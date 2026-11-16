"""Streaming CSV helpers for generated task matrices.

The export path intentionally works with assignment iterators. It never builds
the respondent/task matrix in memory and it never serializes the matrix to JSON.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Tuple


@dataclass(frozen=True)
class MatrixColumn:
    phase_type: Optional[str]
    api_key: str
    header: str


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _normalized_phase(value: Any) -> Optional[str]:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text if text in {"grid", "text"} else None


def _key_parts(api_key: str) -> Tuple[str, int]:
    match = re.match(r"^(.*)_([0-9]+)$", api_key)
    if not match:
        return api_key, 2**31 - 1
    return match.group(1), int(match.group(2))


def _human_header(api_key: str, content: Any) -> str:
    if not isinstance(content, Mapping):
        return api_key
    group = str(content.get("category_name") or content.get("layer_name") or "").strip()
    name = str(
        content.get("name")
        or content.get("text_content")
        or content.get("alt_text")
        or ""
    ).strip()
    if group and name:
        return f"{group}-{name}"
    return name or api_key


def discover_matrix_columns(
    rows: Iterable[Any],
    *,
    study_type: str,
    group_order: Optional[Dict[Tuple[Optional[str], str], int]] = None,
    phase_order: Optional[Iterable[str]] = None,
) -> list[MatrixColumn]:
    """Discover stable matrix columns without retaining assignment rows."""
    is_hybrid = str(study_type).lower() == "hybrid"
    order_map = group_order or {}
    configured_phases = [
        phase for phase in (_normalized_phase(item) for item in (phase_order or [])) if phase
    ]
    if not configured_phases:
        configured_phases = ["grid", "text"]
    phase_rank = {phase: index for index, phase in enumerate(configured_phases)}

    discovered: Dict[Tuple[Optional[str], str], MatrixColumn] = {}
    for row in rows:
        phase = _normalized_phase(_field(row, "phase_type")) if is_hybrid else None
        shown = _field(row, "elements_shown", {}) or {}
        content = _field(row, "elements_shown_content", {}) or {}
        if not isinstance(shown, Mapping):
            continue
        for raw_key in shown.keys():
            api_key = str(raw_key)
            identity = (phase, api_key)
            candidate_header = _human_header(
                api_key,
                content.get(raw_key) if isinstance(content, Mapping) else None,
            )
            if is_hybrid:
                candidate_header = f"{(phase or 'unknown').title()}: {candidate_header}"
            existing = discovered.get(identity)
            # Replace a raw-key fallback once an active task supplies human metadata.
            raw_fallback = f"{(phase or 'unknown').title()}: {api_key}" if is_hybrid else api_key
            if existing is None or (existing.header == raw_fallback and candidate_header != raw_fallback):
                discovered[identity] = MatrixColumn(phase, api_key, candidate_header)

    def sort_key(column: MatrixColumn) -> tuple:
        group_name, element_index = _key_parts(column.api_key)
        phase_index = phase_rank.get(column.phase_type, len(phase_rank)) if is_hybrid else 0
        group_index = order_map.get(
            (column.phase_type if is_hybrid else None, group_name),
            2**31 - 1,
        )
        return phase_index, group_index, group_name.casefold(), element_index, column.api_key.casefold()

    return sorted(discovered.values(), key=sort_key)


def _safe_spreadsheet_text(value: Any) -> Any:
    """Prevent names beginning with formula markers from executing in Excel."""
    if not isinstance(value, str):
        return value
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def stream_matrix_csv(rows: Iterable[Any], columns: list[MatrixColumn]) -> Iterator[str]:
    """Yield a UTF-8 CSV incrementally, one assignment row at a time."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(["Responder", "Task", *(_safe_spreadsheet_text(c.header) for c in columns)])
    yield "\ufeff" + buffer.getvalue()

    for row in rows:
        buffer.seek(0)
        buffer.truncate(0)
        phase = _normalized_phase(_field(row, "phase_type"))
        shown = _field(row, "elements_shown", {}) or {}
        if not isinstance(shown, Mapping):
            shown = {}
        values = []
        for column in columns:
            if column.phase_type and phase != column.phase_type:
                values.append(0)
                continue
            value = shown.get(column.api_key, 0)
            values.append(int(value) if value in (0, 1, False, True) else value)
        writer.writerow([
            int(_field(row, "respondent_id", 0) or 0),
            int(_field(row, "task_index", 0) or 0) + 1,
            *values,
        ])
        yield buffer.getvalue()

