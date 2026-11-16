import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.services.golden_matrix.config import DesignConfig
from app.services.golden_matrix.generator import generate_golden_matrix
from app.services.golden_matrix.rust_engine import generate_golden_matrix_rust
from app.services.golden_task_generator import (
    generate_grid_tasks_golden,
    generate_layer_tasks_golden,
)


RUST_BINARY = (
    Path(__file__).resolve().parents[1]
    / "golden_matrix_rust"
    / "target"
    / "release"
    / ("golden-matrix-rust.exe" if os.name == "nt" else "golden-matrix-rust")
)


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="release Rust binary is not built")
@pytest.mark.parametrize(
    ("seed", "kwargs"),
    [
        (
            7,
            {
                "n_respondents": 3,
                "tasks_per_respondent": 12,
                "elements_per_category": [2, 2, 2, 2],
                "min_actives_per_row": 2,
                "max_actives_per_row": 3,
            },
        ),
        (
            123,
            {
                "n_respondents": 2,
                "tasks_per_respondent": 10,
                "elements_per_category": [2, 3, 2],
                "min_actives_per_row": 1,
                "max_actives_per_row": 2,
                "incompatible_pairs": [(0, 2)],
            },
        ),
        (
            0,
            {
                "n_respondents": 2,
                "tasks_per_respondent": 18,
                "elements_per_category": [3, 3, 3, 3],
                "min_actives_per_row": 2,
                "max_actives_per_row": 4,
            },
        ),
    ],
)
def test_rust_matches_python_final_matrix_exactly(seed: int, kwargs: dict):
    config = DesignConfig(n_categories=len(kwargs["elements_per_category"]), **kwargs)

    _, python_report = generate_golden_matrix(config, rng_seed=seed)
    _, rust_report = generate_golden_matrix_rust(config, rng_seed=seed)

    python_matrix = np.asarray(python_report["X"], dtype=np.int8)
    rust_matrix = np.asarray(rust_report["X"], dtype=np.int8)

    assert rust_matrix.shape == python_matrix.shape
    assert np.array_equal(rust_matrix, python_matrix)
    assert rust_report["global_vif"] == pytest.approx(
        python_report["global_max_vif"], rel=0.0, abs=1e-12
    )


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="release Rust binary is not built")
def test_production_grid_task_payload_matches_python_exactly():
    categories = [
        {
            "category_name": f"Category {category_index}",
            "elements": [
                {
                    "element_id": f"{category_index}-{element_index}",
                    "name": f"Element {category_index}-{element_index}",
                    "content": f"content-{category_index}-{element_index}",
                    "element_type": "text",
                }
                for element_index in range(2)
            ],
        }
        for category_index in range(4)
    ]

    with patch(
        "app.services.golden_task_generator._use_rust_golden_matrix",
        return_value=False,
    ):
        python_result = generate_grid_tasks_golden(
            categories,
            number_of_respondents=10,
            tasks_per_respondent=12,
            seed=91,
        )

    with patch(
        "app.services.golden_task_generator._use_rust_golden_matrix",
        return_value=True,
    ):
        rust_result = generate_grid_tasks_golden(
            categories,
            number_of_respondents=10,
            tasks_per_respondent=12,
            seed=91,
        )

    assert rust_result["tasks"] == python_result["tasks"]
    assert rust_result["metadata"]["tasks_per_consumer"] == python_result["metadata"]["tasks_per_consumer"]
    assert rust_result["metadata"]["capacity"] == python_result["metadata"]["capacity"]


@pytest.mark.skipif(not RUST_BINARY.exists(), reason="release Rust binary is not built")
def test_production_layer_task_payload_matches_python_exactly():
    layers = [
        {
            "layer_id": f"layer-{layer_index}",
            "name": f"Layer {layer_index}",
            "z_index": layer_index,
            "images": [
                {
                    "image_id": f"{layer_index}-{image_index}",
                    "name": f"Image {layer_index}-{image_index}",
                    "url": f"https://cdn.example.com/{layer_index}-{image_index}.png",
                    "alt_text": f"Image {layer_index}-{image_index}",
                }
                for image_index in range(2)
            ],
        }
        for layer_index in range(4)
    ]
    constraints = [
        {
            "anchors": [{"layer_id": "layer-0", "image_id": "0-0"}],
            "blocked": [{"layer_id": "layer-1", "image_id": "1-1"}],
        }
    ]

    with patch(
        "app.services.golden_task_generator._use_rust_golden_matrix",
        return_value=False,
    ):
        python_result = generate_layer_tasks_golden(
            layers,
            number_of_respondents=5,
            tasks_per_respondent=12,
            seed=91,
            design_constraints=constraints,
        )

    with patch(
        "app.services.golden_task_generator._use_rust_golden_matrix",
        return_value=True,
    ):
        rust_result = generate_layer_tasks_golden(
            layers,
            number_of_respondents=5,
            tasks_per_respondent=12,
            seed=91,
            design_constraints=constraints,
        )

    assert rust_result["tasks"] == python_result["tasks"]
    assert rust_result["metadata"]["tasks_per_consumer"] == python_result["metadata"]["tasks_per_consumer"]
    assert rust_result["metadata"]["capacity"] == python_result["metadata"]["capacity"]
    assert rust_result["metadata"]["incompatible_pairs_count"] == python_result["metadata"]["incompatible_pairs_count"]


