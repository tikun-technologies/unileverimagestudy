"""Unit tests for deterministic design optimizer."""

from __future__ import annotations

import unittest

from types import SimpleNamespace

from app.services.design_optimizer import (
    OptimizerCategory,
    OptimizerElement,
    build_conflict_pair_set,
    canonicalize_design_constraints,
    rank_designs,
    rank_elements,
    verify_design,
)


def _el(cat: str, name: str, value: float, **kwargs) -> OptimizerElement:
    return OptimizerElement(
        element_id=f"{cat}::{name}",
        category_key=cat,
        category_name=cat,
        name=name,
        value=value,
        layer_id=kwargs.get("layer_id"),
        image_id=kwargs.get("image_id"),
        z_index=kwargs.get("z_index", 0),
        code=kwargs.get("code"),
    )


class DesignOptimizerTests(unittest.TestCase):
    def test_rank_elements_highest_and_ties(self):
        categories = [
            OptimizerCategory(
                key="A",
                name="A",
                elements=[_el("A", "a1", 10, code="A1"), _el("A", "a2", 8, code="A2")],
            ),
            OptimizerCategory(
                key="B",
                name="B",
                elements=[_el("B", "b1", 10, code="B1"), _el("B", "b2", 1, code="B2")],
            ),
        ]
        top = rank_elements(categories, direction="highest", limit=2)
        self.assertEqual(top[0].value, 10)
        self.assertEqual(top[1].value, 10)
        # Deterministic tie-break by category/name
        self.assertEqual([el.name for el in top[:2]], ["a1", "b1"])

        bottom = rank_elements(categories, direction="lowest", limit=1)
        self.assertEqual(bottom[0].name, "b2")

    def test_layer_best_designs_require_all_layers_and_constraints(self):
        categories = [
            OptimizerCategory(
                key="L1",
                name="L1",
                z_index=0,
                elements=[
                    _el("L1", "good", 20, layer_id="l1", image_id="i1", z_index=0),
                    _el("L1", "bad", -5, layer_id="l1", image_id="i2", z_index=0),
                ],
            ),
            OptimizerCategory(
                key="L2",
                name="L2",
                z_index=1,
                elements=[
                    _el("L2", "good", 15, layer_id="l2", image_id="j1", z_index=1),
                    _el("L2", "blocked", 100, layer_id="l2", image_id="j2", z_index=1),
                ],
            ),
        ]
        constraints = [
            {
                "anchors": [{"layer_id": "l1", "image_id": "i1"}],
                "blocked": [{"layer_id": "l2", "image_id": "j2"}],
            }
        ]
        designs, meta = rank_designs(
            categories,
            study_type="layer",
            direction="highest",
            limit=3,
            design_constraints=constraints,
            require_all_layers=True,
            timeout_ms=500,
        )
        self.assertGreaterEqual(len(designs), 1)
        self.assertTrue(designs[0].complete_layers)
        selected_ids = {el.image_id for el in designs[0].elements}
        # Best unconstrained would be i1+j2=120, but that pair is blocked.
        # Next-best valid complete mix is i2+j2 = 95.
        self.assertEqual(selected_ids, {"i2", "j2"})
        self.assertEqual(designs[0].score, 95)
        self.assertNotIn({"i1", "j2"}, [{el.image_id for el in d.elements} for d in designs])

        pairs = build_conflict_pair_set(constraints)
        ok, errors = verify_design(
            designs[0],
            pairs,
            require_all_layers=True,
            layer_count=2,
        )
        self.assertTrue(ok)
        self.assertEqual(errors, [])
        self.assertFalse(meta.get("timed_out"))

    def test_grid_designs_respect_max_four_categories(self):
        categories = []
        for idx in range(6):
            cat = f"C{idx}"
            categories.append(
                OptimizerCategory(
                    key=cat,
                    name=cat,
                    order=idx,
                    elements=[_el(cat, f"e{idx}", 10 - idx)],
                )
            )
        designs, _ = rank_designs(
            categories,
            study_type="grid",
            direction="highest",
            limit=5,
            require_all_layers=False,
            timeout_ms=500,
        )
        self.assertGreaterEqual(len(designs), 1)
        self.assertEqual(len(designs[0].elements), 4)

    def test_forced_element_and_require_any_white(self):
        categories = [
            OptimizerCategory(
                key="L1",
                name="Cap",
                z_index=0,
                elements=[
                    _el("L1", "A4-largecap-white-transp", 8, layer_id="l1", image_id="w1", z_index=0),
                    _el("L1", "black-cap", 20, layer_id="l1", image_id="b1", z_index=0),
                ],
            ),
            OptimizerCategory(
                key="L2",
                name="Body",
                z_index=1,
                elements=[
                    _el("L2", "green-body", 15, layer_id="l2", image_id="g1", z_index=1),
                    _el("L2", "blue-body", 5, layer_id="l2", image_id="u1", z_index=1),
                ],
            ),
        ]
        forced, _ = rank_designs(
            categories,
            study_type="layer",
            direction="highest",
            limit=1,
            forced_by_category={"L1": "L1::A4-largecap-white-transp"},
        )
        self.assertEqual(forced[0].selected_by_category["L1"], "L1::A4-largecap-white-transp")
        self.assertEqual(forced[0].score, 23)

        white_ids = ["L1::A4-largecap-white-transp"]
        white_designs, _ = rank_designs(
            categories,
            study_type="layer",
            direction="highest",
            limit=2,
            require_any_element_ids=white_ids,
        )
        self.assertTrue(
            all(
                "L1::A4-largecap-white-transp" in design.selected_by_category.values()
                for design in white_designs
            )
        )

    def test_canonicalize_constraints_resolves_uuid_aliases(self):
        layers = [
            SimpleNamespace(
                id="layer-uuid-1",
                layer_id="L1",
                name="Bottle",
                images=[
                    SimpleNamespace(id="img-uuid-1", image_id="I1", name="cap-a"),
                    SimpleNamespace(id="img-uuid-2", image_id="I2", name="cap-b"),
                ],
            ),
            SimpleNamespace(
                id="layer-uuid-2",
                layer_id="L2",
                name="Label",
                images=[
                    SimpleNamespace(id="img-uuid-3", image_id="J1", name="green"),
                    SimpleNamespace(id="img-uuid-4", image_id="J2", name="blocked"),
                ],
            ),
        ]
        # Constraints may store ORM/primary-key UUIDs from the create-study UI.
        raw = [
            {
                "anchors": [{"layerId": "layer-uuid-1", "imageId": "img-uuid-1"}],
                "blocked": [{"layer_id": "layer-uuid-2", "image_id": "img-uuid-4"}],
            }
        ]
        normalized = canonicalize_design_constraints(raw, layers=layers)
        self.assertEqual(normalized[0]["anchors"][0], {"layer_id": "L1", "image_id": "I1"})
        self.assertEqual(normalized[0]["blocked"][0], {"layer_id": "L2", "image_id": "J2"})

        categories = [
            OptimizerCategory(
                key="L1",
                name="Bottle",
                z_index=0,
                elements=[
                    _el("L1", "cap-a", 20, layer_id="L1", image_id="I1", z_index=0),
                    _el("L1", "cap-b", -5, layer_id="L1", image_id="I2", z_index=0),
                ],
            ),
            OptimizerCategory(
                key="L2",
                name="Label",
                z_index=1,
                elements=[
                    _el("L2", "green", 15, layer_id="L2", image_id="J1", z_index=1),
                    _el("L2", "blocked", 100, layer_id="L2", image_id="J2", z_index=1),
                ],
            ),
        ]
        designs, meta = rank_designs(
            categories,
            study_type="layer",
            direction="highest",
            limit=1,
            design_constraints=normalized,
            require_all_layers=True,
        )
        selected = {el.image_id for el in designs[0].elements}
        self.assertEqual(selected, {"I2", "J2"})
        self.assertTrue(meta["constraints_applied"])

    def test_worst_layer_designs(self):
        categories = [
            OptimizerCategory(
                key="L1",
                name="L1",
                z_index=0,
                elements=[
                    _el("L1", "high", 20, layer_id="l1", image_id="a", z_index=0),
                    _el("L1", "low", -10, layer_id="l1", image_id="b", z_index=0),
                ],
            ),
            OptimizerCategory(
                key="L2",
                name="L2",
                z_index=1,
                elements=[
                    _el("L2", "high", 5, layer_id="l2", image_id="c", z_index=1),
                    _el("L2", "low", -7, layer_id="l2", image_id="d", z_index=1),
                ],
            ),
        ]
        designs, _ = rank_designs(
            categories,
            study_type="layer",
            direction="lowest",
            limit=2,
            design_constraints=[],
            require_all_layers=True,
        )
        self.assertEqual(designs[0].score, -17)

    def _brute_force_best_worst(self, categories):
        """Exhaustively compute the true min/max full-layer-stack totals."""
        import itertools

        option_values = [[el.value for el in cat.elements] for cat in categories]
        totals = [sum(combo) for combo in itertools.product(*option_values)]
        return min(totals), max(totals)

    def test_worst_design_matches_brute_force_on_large_all_positive_study(self):
        # Reproduces the real-world defect: many layers with all-positive
        # coefficients. If the search explores highest-first while minimizing,
        # a tight timeout returns a badly non-optimal "worst" design. The true
        # worst is simply the lowest element in every layer.
        import random

        rng = random.Random(1234)
        categories = []
        for layer_idx in range(9):
            elements = []
            option_count = rng.randint(3, 5)
            for opt_idx in range(option_count):
                elements.append(
                    _el(
                        f"L{layer_idx}",
                        f"e{layer_idx}_{opt_idx}",
                        round(rng.uniform(1.0, 12.0), 1),
                        layer_id=f"l{layer_idx}",
                        image_id=f"i{layer_idx}_{opt_idx}",
                        z_index=layer_idx,
                    )
                )
            categories.append(
                OptimizerCategory(
                    key=f"L{layer_idx}",
                    name=f"L{layer_idx}",
                    z_index=layer_idx,
                    elements=elements,
                )
            )

        expected_min, expected_max = self._brute_force_best_worst(categories)

        worst, worst_meta = rank_designs(
            categories,
            study_type="layer",
            direction="lowest",
            limit=3,
            design_constraints=[],
            require_all_layers=True,
            timeout_ms=200,
        )
        best, best_meta = rank_designs(
            categories,
            study_type="layer",
            direction="highest",
            limit=3,
            design_constraints=[],
            require_all_layers=True,
            timeout_ms=200,
        )

        self.assertFalse(worst_meta.get("timed_out"), "worst search should complete")
        self.assertFalse(best_meta.get("timed_out"), "best search should complete")
        self.assertAlmostEqual(worst[0].score, expected_min, places=4)
        self.assertAlmostEqual(best[0].score, expected_max, places=4)
        # Every returned design must be a complete stack (one element per layer).
        self.assertTrue(all(len(d.selected_by_category) == len(categories) for d in worst))
        self.assertTrue(all(len(d.selected_by_category) == len(categories) for d in best))
        # Worst list is strictly ascending, best list strictly descending.
        self.assertEqual([d.score for d in worst], sorted(d.score for d in worst))
        self.assertEqual(
            [d.score for d in best], sorted((d.score for d in best), reverse=True)
        )


if __name__ == "__main__":
    unittest.main()
