import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.response import StudyResponseService


class MockExecuteResult:
    def __init__(self, *, first_value=None, scalar_items=None, scalar_value=None):
        self._first_value = first_value
        self._scalar_items = scalar_items
        self._scalar_value = scalar_value

    def first(self):
        return self._first_value

    def all(self):
        return self._scalar_items or []

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalar_items or [])

    def scalar(self):
        return self._scalar_value


class MockDB:
    def __init__(self, execute_results):
        self.execute_results = list(execute_results)
        self.commit = MagicMock()

    def execute(self, *args, **kwargs):
        if not self.execute_results:
            raise AssertionError("Unexpected execute() call")
        return self.execute_results.pop(0)


def _build_study(target=300, pool=600):
    return SimpleNamespace(
        id=uuid4(),
        creator_id=uuid4(),
        status='active',
        audience_segmentation={"number_of_respondents": target},
        tasks={str(i): [{"task_id": f"{i}_0"}] for i in range(1, pool + 1)},
    )


def test_study_does_not_complete_when_only_total_reaches_target():
    study = _build_study(target=300, pool=600)
    db = MockDB([
        MockExecuteResult(scalar_items=[study]),
        MockExecuteResult(first_value=SimpleNamespace(total=300, completed=200, abandoned=0)),
        MockExecuteResult(scalar_value=600),
    ])
    service = StudyResponseService(db)

    with patch('app.services.study.change_status_fast') as mock_change_status:
        service._check_and_complete_studies(study_id=study.id)

    assert not mock_change_status.called


def test_study_completes_when_completed_reaches_target():
    study = _build_study(target=300, pool=600)
    db = MockDB([
        MockExecuteResult(scalar_items=[study]),
        MockExecuteResult(first_value=SimpleNamespace(total=300, completed=300, abandoned=0)),
        MockExecuteResult(scalar_value=600),
        MockExecuteResult(scalar_items=[]),
    ])
    service = StudyResponseService(db)

    with patch('app.services.study.change_status_fast') as mock_change_status:
        service._check_and_complete_studies(study_id=study.id)

    mock_change_status.assert_called_once_with(db, study.id, study.creator_id, 'completed')


def test_study_completes_when_generated_pool_is_exhausted():
    study = _build_study(target=300, pool=600)
    in_progress = SimpleNamespace(
        study_id=study.id,
        status='in_progress',
        is_completed=False,
    )

    db = MockDB([
        MockExecuteResult(scalar_items=[study]),
        MockExecuteResult(first_value=SimpleNamespace(total=600, completed=250, abandoned=100)),
        MockExecuteResult(scalar_value=600),
        MockExecuteResult(scalar_items=[in_progress]),
    ])
    service = StudyResponseService(db)

    with patch('app.services.study.change_status_fast') as mock_change_status:
        service._check_and_complete_studies(study_id=study.id)

    assert in_progress.is_abandoned is True
    assert in_progress.status == 'abandoned'
    assert in_progress.abandonment_reason == 'Study completed - response was in progress'
    assert db.commit.called
    mock_change_status.assert_called_once_with(db, study.id, study.creator_id, 'completed')


def test_legacy_matrix_still_closes_the_study_when_task_table_is_empty():
    study = _build_study(target=2, pool=2)
    db = MockDB([
        MockExecuteResult(scalar_items=[study]),
        MockExecuteResult(first_value=SimpleNamespace(total=2, completed=2, abandoned=0)),
        MockExecuteResult(scalar_value=0),
        MockExecuteResult(scalar_value={"1": [{"task_id": "1_0"}], "2": [{"task_id": "2_0"}]}),
        MockExecuteResult(scalar_items=[]),
    ])
    service = StudyResponseService(db)

    with patch('app.services.study.change_status_fast') as mock_change_status:
        service._check_and_complete_studies(study_id=study.id)

    mock_change_status.assert_called_once_with(db, study.id, study.creator_id, 'completed')
