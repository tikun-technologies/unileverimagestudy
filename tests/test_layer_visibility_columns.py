import math
import unittest

import pandas as pd

from app.services.response import (
    _fill_layer_visibility_columns,
    _layer_row_needs_fallback,
    _layer_value_visible,
)


def _legacy_cell(shown, layers, content, key):
    """The previous per-row rule, kept here so the fast path cannot drift."""
    layer_data = shown or layers or content or {}
    if not isinstance(layer_data, dict):
        return 0
    if key not in layer_data:
        return 0
    value = layer_data[key]
    if isinstance(value, dict):
        vis = value.get("visible")
        if vis is None:
            vis = bool(value)
        return 1 if bool(vis) else 0
    try:
        return 1 if int(value) != 0 else 0
    except (TypeError, ValueError):
        return 1 if bool(value) else 0


class LayerVisibilityColumnsTest(unittest.TestCase):
    def test_matches_previous_rule_on_mixed_rows(self):
        rows = [
            ({"Bottle_1": 1, "Bottle_2": 0}, {}, {}),
            ({}, {"Bottle_1": 1}, {"Bottle_1": 0}),
            (None, None, {"Bottle_2": {"visible": True, "name": "On"}}),
            ({"Bottle_1": {"visible": False}}, {"Bottle_1": 1}, {}),
            ({"Bottle_1": {"visible": None, "name": "On"}}, {}, {}),
            ({"Bottle_1": {"visible": None}}, {}, {}),
            ({"Bottle_1": {}}, {}, {}),
            ({"Bottle_1": "1"}, {}, {}),
            ({"Bottle_1": "0"}, {}, {}),
            ({"Bottle_1": "true"}, {}, {}),
            ({"Bottle_1": ""}, {}, {}),
            ({"Bottle_1": True}, {}, {}),
            ({"Bottle_1": False}, {}, {}),
            ({"other": 1}, {"Bottle_1": 1}, {}),
            (float("nan"), {"Bottle_1": 1}, {}),
            ("junk", {"Bottle_1": 1}, {}),
            (0, None, {"Bottle_1": 1}),
            (None, None, None),
            ({"Bottle_1": 1, "Bottle_2": {"visible": 0, "url": "x"}}, {}, {}),
        ]
        frame = pd.DataFrame(
            {
                "elements_shown_in_task": [row[0] for row in rows],
                "layers_shown_in_task": [row[1] for row in rows],
                "elements_shown_content": [row[2] for row in rows],
            }
        )
        keys = ["Bottle_1", "Bottle_2"]
        headers = {"Bottle_1": "Bottle-Off", "Bottle_2": "Bottle-On"}
        _fill_layer_visibility_columns(frame, keys, headers)

        for index, (shown, layers, content) in enumerate(rows):
            for key, header in headers.items():
                expected = _legacy_cell(shown, layers, content, key)
                actual = int(frame.iloc[index][header])
                self.assertEqual(actual, expected, f"row {index} {key}")

    def test_later_image_replaces_shared_column(self):
        frame = pd.DataFrame(
            {
                "elements_shown_in_task": [
                    {"Layer_1": 1},
                    {"Layer_1": 1, "Layer_2": 0},
                    {"Layer_2": 1},
                ],
                "layers_shown_in_task": [{}, {}, {}],
                "elements_shown_content": [{}, {}, {}],
            }
        )
        _fill_layer_visibility_columns(
            frame,
            ["Layer_1", "Layer_2"],
            {"Layer_1": "Same", "Layer_2": "Same"},
        )
        self.assertEqual(frame["Same"].tolist(), [0, 0, 1])

    def test_empty_frame_still_has_columns(self):
        frame = pd.DataFrame(
            columns=["elements_shown_in_task", "layers_shown_in_task", "elements_shown_content"]
        )
        _fill_layer_visibility_columns(frame, ["Layer_1"], {"Layer_1": "Layer-A"})
        self.assertIn("Layer-A", frame.columns)
        self.assertEqual(len(frame), 0)

    def test_missing_source_columns_are_zero(self):
        frame = pd.DataFrame({"other": [1, 2]})
        _fill_layer_visibility_columns(frame, ["Layer_1"], {"Layer_1": "Layer-A"})
        self.assertEqual(frame["Layer-A"].tolist(), [0, 0])

    def test_fallback_only_when_visibility_json_is_empty(self):
        self.assertFalse(_layer_row_needs_fallback({"Bottle_1": 1}))
        self.assertFalse(_layer_row_needs_fallback(float("nan")))
        self.assertTrue(_layer_row_needs_fallback(None))
        self.assertTrue(_layer_row_needs_fallback({}))

    def test_cell_values(self):
        self.assertEqual(_layer_value_visible({"name": "On"}), 1)
        self.assertEqual(_layer_value_visible({"visible": False, "name": "On"}), 0)
        self.assertEqual(_layer_value_visible({"visible": 0}), 0)
        self.assertEqual(_layer_value_visible(math.nan), 1)
        self.assertEqual(_layer_value_visible([]), 0)
        self.assertEqual(_layer_value_visible([1]), 1)


if __name__ == "__main__":
    unittest.main()
