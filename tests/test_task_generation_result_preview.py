from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.api.v1.study import get_task_generation_result
from app.models.job_model import JobStatus


def test_completed_job_preview_loads_only_one_respondent():
    study_id = uuid4()
    user_id = uuid4()
    db = MagicMock()
    user = SimpleNamespace(id=user_id)
    job = SimpleNamespace(
        job_id="job-1",
        study_id=str(study_id),
        status=JobStatus.COMPLETED,
        completed_at=None,
        result={"matrix_engine": "rust", "algorithm": "golden_matrix_rust"},
    )
    study = SimpleNamespace(
        jobid="job-1",
        layers=[],
        audience_segmentation={"number_of_respondents": 500},
    )
    preview_tasks = {
        "1": [
            {
                "task_id": "1_0",
                "task_index": 0,
                "elements_shown": {"A_1": 1},
                "elements_shown_content": {},
            }
        ]
    }

    with (
        patch("app.services.background_task_service.background_task_service.get_job_status", return_value=job),
        patch("app.api.v1.study._user_has_study_access", return_value=True),
        patch("app.api.v1.study.study_service.get_study", return_value=study),
        patch(
            "app.api.v1.study.study_service.build_task_generation_result_config",
            return_value={"study_type": "grid", "layers": [], "categories": []},
        ),
        patch("app.services.task_service.TaskService") as task_service_cls,
    ):
        task_service = task_service_cls.return_value
        task_service.get_preview_tasks_as_dict.return_value = preview_tasks
        task_service.get_respondent_count.return_value = 1_000
        task_service.get_task_count.return_value = 16_000

        result = get_task_generation_result(
            job_id="job-1",
            preview_only=True,
            db=db,
            current_user=user,
        )

    assert result["tasks"] == preview_tasks
    assert result["matrix_engine"] == "rust"
    assert result["metadata"] == {
        "number_of_respondents": 500,
        "total_respondents": 1_000,
        "generated_respondents": 1_000,
        "total_tasks": 16_000,
        "tasks_per_consumer": 1,
        "preview_only": True,
        "matrix_engine": "rust",
        "algorithm": "golden_matrix_rust",
        "completed_at": None,
        "message": "Task generation completed successfully",
    }
    task_service.get_preview_tasks_as_dict.assert_called_once_with(study_id)
    task_service.get_all_tasks_as_dict.assert_not_called()
    db.commit.assert_called_once()

