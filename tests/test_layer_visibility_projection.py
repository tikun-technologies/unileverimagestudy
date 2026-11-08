"""Opt-in read-only PostgreSQL check of the visibility projection's semantics."""

import os
import unittest

from sqlalchemy import Integer, column, select, text, values
from sqlalchemy.dialects.postgresql import JSONB

from app.services.layer_visibility import layer_visibility_expression
from app.services.response import _first_layer_payload, _layer_value_visible


@unittest.skipUnless(os.getenv("RUN_POSTGRES_INTEGRATION") == "1", "Requires a PostgreSQL connection")
class LayerVisibilityProjectionTests(unittest.TestCase):
    def test_matches_python_visibility_and_fallback_rules(self):
        from app.db.session import engine

        cells = [None, False, True, 0, 1, -1, 0.5, -0.5, 2.5, "0", " 0 ", "1", "true", "", "0.0", [], [0], {}, {"visible": None}, {"url": "image"}]
        cells += [{"visible": value, "url": "image-metadata"} for value in cells[:18]]
        cases = []
        for cell in cells:
            for shown, layers, content in [
                ({"Layer_1": cell}, None, None),
                ({}, {"Layer_1": cell}, {"Layer_2": 1}),
                (None, {}, {"Layer_1": cell}),
            ]:
                cases.append((len(cases), shown, layers, content))
        for source in [[], [1], "junk", False, 0, "", None, {}]:
            cases.append((len(cases), source, {"Layer_1": 1}, None))
        cases.append((len(cases), {"Layer_1": {"visible": True}, "Layer_2": "0"}, None, None))
        rows = values(
            column("idx", Integer), column("shown", JSONB), column("layers", JSONB), column("content", JSONB),
        ).data(cases).alias("cases")
        query = select(rows.c.idx, layer_visibility_expression(rows.c.shown, rows.c.layers, rows.c.content)).select_from(rows)
        with engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                result = connection.execute(query).all()
        for index, actual in result:
            _, shown, layers, content = cases[index]
            source = _first_layer_payload(shown, layers, content) or {}
            for key in ["Layer_1", "Layer_2"]:
                expected = _layer_value_visible(source[key]) if key in source else 0
                observed = int(key in actual) if isinstance(actual, list) else _layer_value_visible(actual[key]) if key in actual else 0
                self.assertEqual(observed, expected, (index, key, source, actual))
