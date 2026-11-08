"""Project task visibility in PostgreSQL without transferring image metadata."""

import json

from sqlalchemy import and_, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH


def layer_visibility_expression(shown, layers, content):
    """Equivalent to the Python layer fallback, with sparse scalar values.

    Layer task JSON can contain full images for every task. Analytics only
    reads visibility. Resolve the legacy fallback in the same query, remove
    image fields, and omit zero values before sending rows to Python.
    """
    def json_value(value):
        return cast(literal(json.dumps(value)), JSONB)

    empty = json_value({})
    blank = [json_value(value) for value in (None, {}, [], False, 0, "")]

    def nonblank(value):
        return and_(value.is_not(None), ~value.in_(blank))

    source = case((nonblank(shown), shown), (nonblank(layers), layers), else_=content)
    source_object = case((func.jsonb_typeof(source) == "object", source), else_=empty)
    entries = func.jsonb_each(source_object).table_valued("key", "value").alias("layer_entry")
    value = cast(entries.c.value, JSONB)
    visible = value.op("->")("visible")
    # Dict values use bool(visible), or bool(dict) when visible is absent/null.
    # Keep scalar values intact for the existing Python int/truthiness rule.
    projected = case(
        (func.jsonb_typeof(value) == "object", func.to_jsonb(case(
            (visible.is_(None) | (visible == json_value(None)), value != empty),
            else_=nonblank(visible),
        ))),
        else_=value,
    )
    legacy = (
        select(func.coalesce(func.jsonb_object_agg(entries.c.key, projected), empty))
        .select_from(entries)
        .where(nonblank(projected))
        .correlate_except(entries)
        .scalar_subquery()
    )
    # Current tasks store {visible: 0|1|false|true, z_index: ...}. JSONPath
    # evaluates those flags in native code and returns just the shown keys.
    # Preserve the general Python rules above for older/malformed payloads.
    unusual = '$.* ? (@.type() != "object" || !exists(@.visible) || (@.visible.type() != "number" && @.visible.type() != "boolean") || (@.visible.type() == "number" && @.visible != 0 && @.visible != 1))'
    shown_keys = '$.keyvalue() ? (@.value.visible == 1 || @.value.visible == true).key'
    return case(
        (~func.jsonb_path_exists(source_object, cast(literal(unusual), JSONPATH)),
         func.jsonb_path_query_array(source_object, cast(literal(shown_keys), JSONPATH))),
        else_=legacy,
    )
