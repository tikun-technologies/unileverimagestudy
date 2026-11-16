from app.services.task_generation_summary import compact_task_generation_summary


def test_summary_records_rust_without_persisting_tasks():
    result = {
        "tasks": {"1": [{"task_id": "1_0"}]},
        "metadata": {"matrix_engine": "rust", "algorithm": "golden_matrix_rust"},
    }
    summary = compact_task_generation_summary(
        result,
        {"seed": 42, "audience_segmentation": {"number_of_respondents": 500}},
    )

    assert summary == {
        "job_type": "task_generation",
        "matrix_engine": "rust",
        "algorithm": "golden_matrix_rust",
        "seed": 42,
        "requested_respondents": 500,
    }
    assert "tasks" not in summary


def test_summary_handles_hybrid_phase_metadata():
    result = {
        "metadata": {
            "phases": [
                {"phase_type": "grid", "metadata": {"matrix_engine": "rust", "algorithm": "golden_matrix_rust"}},
                {"phase_type": "text", "metadata": {"matrix_engine": "rust", "algorithm": "golden_matrix_rust"}},
            ]
        }
    }

    summary = compact_task_generation_summary(result, {})

    assert summary["matrix_engine"] == "rust"
    assert summary["algorithm"] == "golden_matrix_rust"

