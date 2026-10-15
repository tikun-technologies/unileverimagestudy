"""Fast task-generation precheck. No database and no matrix build."""
import time

from app.services.task_generation_precheck import (
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    generated_respondent_count,
    precheck_task_generation,
)


def _layers(sizes):
    layers = []
    for index, (name, count) in enumerate(sizes):
        layer_id = f"layer-{index}"
        layers.append({
            "layer_id": layer_id,
            "name": name,
            "images": [
                {"image_id": f"{layer_id}-{image}", "name": f"img-{image}"}
                for image in range(count)
            ],
        })
    return layers


def _payload(study_type, respondents, **extra):
    body = {
        "study_type": study_type,
        "audience_segmentation": {"number_of_respondents": respondents},
    }
    body.update(extra)
    return body


def _balanced_layers(n_layers=10, each=4):
    return _layers([(f"Layer {i}", each) for i in range(n_layers)])


def test_generated_count_matches_worker_cap():
    assert generated_respondent_count(0) == 0
    assert generated_respondent_count(1) == 1
    assert generated_respondent_count(800) == 1500
    assert generated_respondent_count(400) == 800


def test_balanced_layer_study_stays_at_one_point_five():
    result = precheck_task_generation(_payload(
        "layer",
        800,
        study_layers=_balanced_layers(),
    ))
    assert result["can_generate"] is True
    assert result["multiplier"] == MIN_MULTIPLIER
    assert result["generated_respondents"] == 1500
    assert result["total_elements"] == 40
    assert result["tasks_per_respondent"] == 60


def test_balanced_grid_stays_at_one_point_five():
    categories = []
    elements = []
    for index in range(6):
        cat_id = f"cat-{index}"
        categories.append({"category_id": cat_id, "name": f"Cat {index}", "phase_type": "grid"})
        for element in range(4):
            elements.append({"category_id": cat_id, "element_id": f"{cat_id}-{element}", "name": "e"})
    result = precheck_task_generation(_payload(
        "grid",
        100,
        categories=categories,
        elements=elements,
    ))
    assert result["can_generate"] is True
    assert result["multiplier"] == MIN_MULTIPLIER
    assert result["generated_respondents"] == 200


def test_dove_copy_lozenge_cannot_fit_within_two():
    layers = _layers([
        ("02_Dove Bottle Deboss", 3),
        ("03_Imagery", 9),
        ("04_Variant", 6),
        ("05_Derm Seal", 8),
        ("06_Lozenge", 14),
        ("07_Advanced care", 4),
        ("08_Wordmark", 4),
        ("09_Bird Treatment", 4),
        ("10_Gold Foil Band", 3),
        ("11_Cap Color", 3),
    ])
    lozenge = layers[4]
    imagery = layers[1]
    constraints = []
    for image in lozenge["images"]:
        constraints.append({
            "name": "block",
            "anchors": [{"layer_id": imagery["layer_id"], "image_id": imagery["images"][0]["image_id"]}],
            "blocked": [{"layer_id": lozenge["layer_id"], "image_id": image["image_id"]}],
        })
    result = precheck_task_generation(_payload(
        "layer",
        800,
        study_layers=layers,
        design_constraints=constraints,
    ))
    assert result["can_generate"] is False
    assert result["multiplier"] == MAX_MULTIPLIER
    assert result["multiplier"] <= 2.0
    assert result["scarce_layer"] == "06_Lozenge"
    assert "06_Lozenge" in result["reason"]
    assert "2.0" in result["reason"]
    assert result["generated_respondents"] == 1500


def test_same_lozenge_shape_fails_without_constraints_too():
    layers = _layers([
        ("Bottle", 3),
        ("Imagery", 9),
        ("Variant", 6),
        ("Derm", 8),
        ("06_Lozenge", 14),
        ("Care", 4),
        ("Wordmark", 4),
        ("Bird", 4),
        ("Gold", 3),
        ("Cap", 3),
    ])
    result = precheck_task_generation(_payload("layer", 800, study_layers=layers))
    assert result["can_generate"] is False
    assert result["scarce_layer"] == "06_Lozenge"


def test_one_respondent_can_still_cover_a_large_layer():
    layers = _layers([
        ("Bottle", 3),
        ("Imagery", 9),
        ("Variant", 6),
        ("Derm", 8),
        ("06_Lozenge", 14),
        ("Care", 4),
        ("Wordmark", 4),
        ("Bird", 4),
        ("Gold", 3),
        ("Cap", 3),
    ])
    result = precheck_task_generation(_payload("layer", 1, study_layers=layers))
    assert result["generated_respondents"] == 1
    assert result["can_generate"] is True
    assert result["multiplier"] <= MAX_MULTIPLIER


def test_mildly_scarce_layer_raises_multiplier_but_stays_at_or_below_two():
    sizes = [(f"Layer {i}", 3) for i in range(9)]
    sizes.append(("Wide", 8))
    result = precheck_task_generation(_payload(
        "layer",
        10,
        study_layers=_layers(sizes),
    ))
    assert result["can_generate"] is True
    assert MIN_MULTIPLIER < result["multiplier"] <= MAX_MULTIPLIER
    assert result["tasks_per_respondent"] >= result["total_elements"]


def test_missing_elements_and_respondents():
    empty = precheck_task_generation(_payload("layer", 50, study_layers=[]))
    assert empty["can_generate"] is False
    no_people = precheck_task_generation(_payload("grid", 0, categories=[], elements=[]))
    assert no_people["can_generate"] is False
    assert "respondents" in no_people["reason"]


def test_hybrid_uses_the_stricter_phase():
    categories = []
    elements = []
    for phase, count in (("grid", 4), ("text", 4)):
        for index in range(4):
            cat_id = f"{phase}-{index}"
            categories.append({"category_id": cat_id, "name": f"{phase} {index}", "phase_type": phase})
            for element in range(count):
                elements.append({"category_id": cat_id, "element_id": f"{cat_id}-{element}"})
    result = precheck_task_generation(_payload(
        "hybrid",
        40,
        categories=categories,
        elements=elements,
        phase_order=["grid", "text"],
    ))
    assert result["can_generate"] is True
    assert result["multiplier"] == MIN_MULTIPLIER
    assert result["total_elements"] == 32


def test_precheck_stays_fast_for_a_large_constraint_list():
    layers = _layers([(f"Layer {i}", 6) for i in range(10)])
    constraints = []
    for image in layers[4]["images"]:
        for anchor_layer in layers[:4]:
            constraints.append({
                "anchors": [{"layer_id": anchor_layer["layer_id"], "image_id": anchor_layer["images"][0]["image_id"]}],
                "blocked": [{"layer_id": layers[4]["layer_id"], "image_id": image["image_id"]}],
            })
    payload = _payload("layer", 500, study_layers=layers, design_constraints=constraints)
    started = time.perf_counter()
    for _ in range(50):
        precheck_task_generation(payload)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
