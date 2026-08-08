from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock, patch

import pytest

# Import all models so SQLAlchemy relationship names are registered during tests.
import app.models.user_model  # noqa: F401
import app.models.project_model  # noqa: F401
import app.models.study_model  # noqa: F401
import app.models.response_model  # noqa: F401

from app.services.task_service import TaskService
from app.services.response import StudyResponseService


def _assignment(
    respondent_id: int,
    task_index: int,
    *,
    phase_type: str | None = None,
    content: dict | None = None,
):
    return SimpleNamespace(
        respondent_id=respondent_id,
        task_index=task_index,
        task_id=f"{respondent_id}_{task_index}",
        elements_shown={"A_1": 1},
        elements_shown_content=content or {"A_1": {"name": "A", "content": "url"}},
        phase_type=phase_type,
    )


def test_task_service_reads_new_rows_before_legacy_json():
    db = MagicMock()
    study_id = uuid4()
    db.execute.return_value.scalars.return_value.all.return_value = [
        _assignment(1, 0, phase_type="grid"),
        _assignment(1, 1, phase_type="text"),
    ]
    db.get.return_value = SimpleNamespace(
        tasks={"1": [{"task_id": "legacy_0", "task_index": 0}]}
    )

    tasks = TaskService(db).get_all_tasks_as_dict(study_id)

    assert list(tasks.keys()) == ["1"]
    assert [t["task_id"] for t in tasks["1"]] == ["1_0", "1_1"]
    assert [t["phase_type"] for t in tasks["1"]] == ["grid", "text"]
    db.get.assert_not_called()


def test_task_service_falls_back_to_legacy_json_when_no_rows_exist():
    db = MagicMock()
    study_id = uuid4()
    legacy_tasks = {
        "1": [{"task_id": "legacy_0", "task_index": 0, "elements_shown": {"A_1": 1}}],
        "2": [{"task_id": "legacy_1", "task_index": 0, "elements_shown": {"A_1": 0}}],
    }
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.get.return_value = SimpleNamespace(tasks=legacy_tasks)

    tasks = TaskService(db).get_all_tasks_as_dict(study_id)

    assert tasks == legacy_tasks


def test_task_service_supports_any_number_of_respondent_rows():
    db = MagicMock()
    study_id = uuid4()
    assignments = [
        _assignment(respondent_id, task_index)
        for respondent_id in range(1, 101)
        for task_index in range(3)
    ]
    db.execute.return_value.scalars.return_value.all.return_value = assignments

    tasks = TaskService(db).get_all_tasks_as_dict(study_id)

    assert len(tasks) == 100
    assert all(len(task_list) == 3 for task_list in tasks.values())
    assert tasks["100"][2]["task_id"] == "100_2"


def test_preview_tasks_prefers_respondent_one_without_loading_all():
    db = MagicMock()
    study_id = uuid4()
    db.execute.return_value.scalars.return_value.all.return_value = [
        _assignment(1, 0),
        _assignment(1, 1),
    ]

    preview = TaskService(db).get_preview_tasks_as_dict(study_id)

    assert preview == {
        "1": [
            {
                "task_id": "1_0",
                "task_index": 0,
                "elements_shown": {"A_1": 1},
                "elements_shown_content": {"A_1": {"name": "A", "content": "url"}},
            },
            {
                "task_id": "1_1",
                "task_index": 1,
                "elements_shown": {"A_1": 1},
                "elements_shown_content": {"A_1": {"name": "A", "content": "url"}},
            },
        ]
    }
    # Preview must not pull every respondent row.
    assert db.execute.call_count == 1


def test_preview_tasks_falls_back_to_first_numeric_respondent():
    db = MagicMock()
    study_id = uuid4()
    service = TaskService(db)

    first_id_result = MagicMock()
    first_id_result.scalar_one_or_none.return_value = 2
    db.execute.return_value = first_id_result

    fallback_tasks = [
        {
            "task_id": "2_0",
            "task_index": 0,
            "elements_shown": {"A_1": 1},
            "elements_shown_content": {"A_1": {"name": "A", "content": "url"}},
        }
    ]

    with patch.object(
        service,
        "get_respondent_tasks",
        side_effect=[[], fallback_tasks],
    ) as get_respondent_tasks:
        preview = service.get_preview_tasks_as_dict(study_id)

    assert preview == {"2": fallback_tasks}
    assert get_respondent_tasks.call_args_list[0].args == (study_id, 1)
    assert get_respondent_tasks.call_args_list[1].args == (study_id, 2)


def test_preview_tasks_returns_none_when_no_tasks_exist():
    db = MagicMock()
    study_id = uuid4()
    db.execute.return_value.scalars.return_value.all.return_value = []
    db.execute.return_value.scalar_one_or_none.return_value = None
    db.get.return_value = SimpleNamespace(tasks={})

    preview = TaskService(db).get_preview_tasks_as_dict(study_id)

    assert preview is None


def test_task_service_save_failure_rolls_back_and_raises():
    db = MagicMock()
    study_id = uuid4()
    db.execute.side_effect = Exception("missing table")

    with pytest.raises(RuntimeError):
        TaskService(db).save_tasks(study_id, {"1": [{"task_id": "1_0", "task_index": 0}]})

    db.rollback.assert_called_once()


def test_completion_capacity_prefers_normalized_task_rows_over_legacy_json():
    db = MagicMock()
    service = StudyResponseService(db)
    study_id = uuid4()
    legacy_tasks = {"1": [{"task_id": "legacy_0"}]}

    with patch("app.services.task_service.TaskService") as task_service_cls:
        task_service_cls.return_value.get_respondent_count.return_value = 250

        capacity = service._get_generated_respondent_capacity_for_study(
            study_id,
            legacy_tasks,
        )

    assert capacity == 250


def test_completion_capacity_falls_back_to_legacy_json_when_no_rows_exist():
    db = MagicMock()
    service = StudyResponseService(db)
    study_id = uuid4()
    legacy_tasks = {
        "1": [{"task_id": "legacy_0"}],
        "2": [{"task_id": "legacy_1"}],
        "design_matrix": [],
    }

    with patch("app.services.task_service.TaskService") as task_service_cls:
        task_service_cls.return_value.get_respondent_count.return_value = 0

        capacity = service._get_generated_respondent_capacity_for_study(
            study_id,
            legacy_tasks,
        )

    assert capacity == 2
