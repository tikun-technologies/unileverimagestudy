from app.services.golden_task_generator import _requested_to_generated_n


def test_one_respondent_preview_does_not_overgenerate() -> None:
    assert _requested_to_generated_n(1) == 1


def test_collection_studies_keep_spare_respondent_pool() -> None:
    assert _requested_to_generated_n(100) == 200
