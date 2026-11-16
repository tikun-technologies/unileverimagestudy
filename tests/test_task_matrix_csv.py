import csv
import io
from types import SimpleNamespace

from app.services.task_matrix_csv import discover_matrix_columns, stream_matrix_csv


def _row(respondent, task, shown, content=None, phase=None):
    return SimpleNamespace(
        respondent_id=respondent,
        task_index=task,
        elements_shown=shown,
        elements_shown_content=content or {},
        phase_type=phase,
    )


def _parse(chunks):
    return list(csv.reader(io.StringIO("".join(chunks).lstrip("\ufeff"))))


def test_streamed_grid_csv_uses_human_headers_and_all_rows():
    rows = [
        _row(1, 0, {"Concept_1": 0, "Concept_2": 1}, {
            "Concept_2": {"category_name": "Concept", "name": "Blue"},
        }),
        _row(1, 1, {"Concept_1": 1, "Concept_2": 0}, {
            "Concept_1": {"category_name": "Concept", "name": "Red"},
        }),
        _row(2, 0, {"Concept_1": 1, "Concept_2": 0}, {
            "Concept_1": {"category_name": "Concept", "name": "Red"},
        }),
    ]

    columns = discover_matrix_columns(
        rows,
        study_type="grid",
        group_order={(None, "Concept"): 0},
    )
    parsed = _parse(stream_matrix_csv(iter(rows), columns))

    assert parsed[0] == ["Responder", "Task", "Concept-Red", "Concept-Blue"]
    assert parsed[1:] == [
        ["1", "1", "0", "1"],
        ["1", "2", "1", "0"],
        ["2", "1", "1", "0"],
    ]


def test_hybrid_csv_keeps_same_api_key_separate_by_phase():
    rows = [
        _row(1, 0, {"Message_1": 1}, {"Message_1": {"category_name": "Message", "name": "Pack"}}, "grid"),
        _row(1, 1, {"Message_1": 1}, {"Message_1": {"category_name": "Message", "name": "Claim"}}, "text"),
    ]

    columns = discover_matrix_columns(
        rows,
        study_type="hybrid",
        group_order={("grid", "Message"): 0, ("text", "Message"): 0},
        phase_order=["grid", "text"],
    )
    parsed = _parse(stream_matrix_csv(iter(rows), columns))

    assert parsed[0] == ["Responder", "Task", "Grid: Message-Pack", "Text: Message-Claim"]
    assert parsed[1] == ["1", "1", "1", "0"]
    assert parsed[2] == ["1", "2", "0", "1"]


def test_csv_headers_are_safe_for_spreadsheet_formula_injection():
    rows = [_row(1, 0, {"Danger_1": 1}, {"Danger_1": {"name": "=cmd|test"}})]
    columns = discover_matrix_columns(rows, study_type="grid")
    parsed = _parse(stream_matrix_csv(iter(rows), columns))

    assert parsed[0][2] == "'=cmd|test"

