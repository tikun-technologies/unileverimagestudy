"""Small, safe metadata persisted with completed task-generation jobs."""
from __future__ import annotations

from typing import Any, Dict, Iterable


def _metadata_nodes(result: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return []
    nodes = [metadata]
    for phase in metadata.get("phases") or []:
        if isinstance(phase, dict) and isinstance(phase.get("metadata"), dict):
            nodes.append(phase["metadata"])
    return nodes


def compact_task_generation_summary(
    result: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Return engine metadata only; never persist generated tasks in the job row."""
    engines = {
        str(node.get("matrix_engine"))
        for node in _metadata_nodes(result)
        if node.get("matrix_engine")
    }
    algorithms = {
        str(node.get("algorithm"))
        for node in _metadata_nodes(result)
        if node.get("algorithm")
    }
    matrix_engine = next(iter(engines)) if len(engines) == 1 else ("mixed" if engines else "unknown")
    algorithm = next(iter(algorithms)) if len(algorithms) == 1 else ("mixed" if algorithms else "unknown")
    audience = payload.get("audience_segmentation") or {}
    return {
        "job_type": "task_generation",
        "matrix_engine": matrix_engine,
        "algorithm": algorithm,
        "seed": payload.get("seed"),
        "requested_respondents": audience.get("number_of_respondents"),
    }

