import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.synthetic.panelist_generator import expand_panelists_to_count, generate_all_panelist_combinations


def _sample_study(option_count=4):
    return {
        "id": "study-1",
        "title": "Test",
        "classification_questions": [
            {
                "question_id": "q1",
                "question_text": "How often do you buy?",
                "order": 1,
                "answer_options": [{"text": f"Option {i}"} for i in range(1, option_count + 1)],
            }
        ],
        "audience_segmentation": {
            "gender_distribution": {"male": 50.0, "female": 50.0},
            "age_distribution": {"18 - 24": 50.0, "25 - 34": 50.0},
        },
    }


def test_one_question_four_options_makes_four_unique_personas():
    unique = generate_all_panelist_combinations(_sample_study(4))
    assert len(unique) == 4
    answers = [p["answers"]["q1"]["answer"] for p in unique]
    assert answers == ["Option 1", "Option 2", "Option 3", "Option 4"]


def test_cycling_fills_requested_count_and_loops_personas():
    unique = generate_all_panelist_combinations(_sample_study(4))
    expanded = expand_panelists_to_count(unique, 10, task_numbers=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    assert len(expanded) == 10
    answers = [p["answers"]["q1"]["answer"] for p in expanded]
    assert answers == [
        "Option 1", "Option 2", "Option 3", "Option 4",
        "Option 1", "Option 2", "Option 3", "Option 4",
        "Option 1", "Option 2",
    ]
    assert [p["panelist_number"] for p in expanded] == list(range(1, 11))
    assert [p["panelist_id"] for p in expanded] == [f"panelist_{i:06d}" for i in range(1, 11)]
    assert [p["task_lookup_number"] for p in expanded] == list(range(1, 11))


def test_cycling_reuses_task_slots_when_fewer_than_requested():
    unique = generate_all_panelist_combinations(_sample_study(4))
    expanded = expand_panelists_to_count(unique, 6, task_numbers=[1, 2, 3, 4])

    assert [p["task_lookup_number"] for p in expanded] == [1, 2, 3, 4, 1, 2]
    assert [p["answers"]["q1"]["answer"] for p in expanded] == [
        "Option 1", "Option 2", "Option 3", "Option 4", "Option 1", "Option 2",
    ]


def test_requested_count_at_or_below_unique_does_not_loop():
    unique = generate_all_panelist_combinations(_sample_study(4))
    expanded = expand_panelists_to_count(unique, 3, task_numbers=[1, 2, 3, 4])

    assert len(expanded) == 3
    assert [p["answers"]["q1"]["answer"] for p in expanded] == ["Option 1", "Option 2", "Option 3"]
