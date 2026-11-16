"""Temporary caller for the Rust golden matrix.

The study rules stay in ``golden_task_generator`` and are applied once before
this call: 2× respondents with the 1500 cap, one-person previews stay one
person, the shared seed, and layer design constraints as column pairs.
Grid, text, and video use the grid config. Layer studies pass constraint
pairs. Hybrid studies call the grid config once per phase.

Set ``expand_respondents`` only when ``n_respondents`` is still the requested
panel size. This app passes the already expanded count, so the flag stays off.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .config import DesignConfig

logger = logging.getLogger("RDE_Generator")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BINARY_NAME = "golden-matrix-rust.exe" if os.name == "nt" else "golden-matrix-rust"
_BINARY = _REPO_ROOT / "golden_matrix_rust" / "target" / "release" / _BINARY_NAME


def _openblas_library() -> Path:
    """Find the OpenBLAS library shipped with the pinned NumPy installation.

    Windows wheels contain a DLL under ``numpy.libs``; Linux wheels contain a
    shared object in the same logical directory. Resolve it from the active
    Python interpreter instead of assuming a developer's Windows venv path.
    """
    import numpy

    site_packages = Path(numpy.__file__).resolve().parent.parent
    libs = site_packages / "numpy.libs"
    patterns = (
        ("*.dll",) if os.name == "nt" else ("*.so", "*.so.*")
    )
    matches = []
    for pattern in patterns:
        matches.extend(libs.glob(pattern))
    matches = sorted(
        path for path in matches
        if "openblas" in path.name.lower()
    )
    if not matches:
        raise RuntimeError(
            "NumPy OpenBLAS library was not found. The Rust golden matrix needs "
            "the OpenBLAS library shipped with NumPy 2.3.2."
        )
    return matches[0]


def _payload(config: DesignConfig, rng_seed: Optional[int]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "n_respondents": int(config.n_respondents),
        "tasks_per_respondent": int(config.tasks_per_respondent),
        "elements_per_category": [int(count) for count in config.elements_per_category],
        "min_actives_per_row": int(config.min_actives_per_row),
        "max_actives_per_row": int(config.max_actives_per_row),
        "incompatible_pairs": [[int(a), int(b)] for a, b in (config.incompatible_pairs or [])],
        "w_global": float(config.w_global),
        "w_local": float(config.w_local),
        "polish": bool(config.polish),
        "max_attempts": int(config.max_attempts),
        "max_block_retries": int(config.max_block_retries),
        "max_vif": float(config.max_vif),
        "seed": 0 if rng_seed is None else int(rng_seed),
        "expand_respondents": False,
    }
    if config.element_priors is not None:
        payload["element_priors"] = [float(value) for value in config.element_priors]
    return payload


def _read_progress(stream, progress_callback: Optional[Callable[[int, int], None]], lines: List[str]) -> None:
    """Forward Rust progress lines to the worker log and the job progress callback."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        lines.append(line)
        logger.info(line)
        if progress_callback is None or not line.startswith("Golden Matrix: respondent "):
            continue
        try:
            done_text, rest = line.split(" respondent ", 1)[1].split("/", 1)
            total_text = rest.split(" ", 1)[0]
            progress_callback(int(done_text), int(total_text))
        except (IndexError, ValueError):
            continue


def generate_golden_matrix_rust(
    config: DesignConfig,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    rng_seed: Optional[int] = None,
) -> Tuple[None, Dict[str, Any]]:
    """Return ``(None, report)`` with ``report["X"]`` shaped like the Python engine."""
    if not _BINARY.is_file():
        raise RuntimeError(
            f"Rust golden matrix binary is missing at {_BINARY}. "
            "Build golden_matrix_rust in release mode before generating tasks."
        )

    if progress_callback is not None:
        progress_callback(0, config.n_respondents)

    openblas_library = _openblas_library()
    env = os.environ.copy()
    env["NUMPY_OPENBLAS_LIB"] = str(openblas_library)
    # Keep the legacy variable for already-built local binaries.
    env["NUMPY_OPENBLAS_DLL"] = str(openblas_library)
    if os.name == "nt":
        env["PATH"] = str(openblas_library.parent) + os.pathsep + env.get("PATH", "")
    else:
        env["LD_LIBRARY_PATH"] = str(openblas_library.parent) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    env.pop("GOLDEN_TRACE", None)

    process = subprocess.Popen(
        [str(_BINARY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(json.dumps(_payload(config, rng_seed)).encode("utf-8"))
    process.stdin.close()
    progress_lines: List[str] = []
    progress_reader = threading.Thread(
        target=_read_progress,
        args=(process.stderr, progress_callback, progress_lines),
        daemon=True,
    )
    progress_reader.start()
    stdout = process.stdout.read()
    return_code = process.wait()
    progress_reader.join()
    if return_code != 0:
        detail = "\n".join(progress_lines).strip()
        if not detail:
            detail = stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "Rust golden matrix failed")

    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Rust golden matrix returned invalid JSON") from exc

    matrix = np.asarray(data["matrix"], dtype=np.int8)
    structural = data.get("structural_vif")
    try:
        structural_vif = float(structural) if structural is not None else None
    except (TypeError, ValueError):
        structural_vif = None

    if progress_callback is not None:
        progress_callback(config.n_respondents, config.n_respondents)

    return None, {
        "X": matrix,
        "status": data.get("status"),
        "attempt": data.get("attempt"),
        "global_max_vif": data.get("global_vif"),
        "global_vif": data.get("global_vif"),
        "skip_vif": data.get("skip_vif"),
        "engine": "rust",
        "preflight": {
            "feasible": True,
            "row_variety": None,
            "structural_vif": structural_vif,
            "tp_ratio": None,
            "tp_class": None,
        },
    }
