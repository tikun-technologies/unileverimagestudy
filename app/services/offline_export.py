"""Compact inputs for the offline HTML engine, without running any analysis."""

from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import json
import logging
import math
import time
from threading import RLock
import zlib

import numpy as np
import pandas as pd


_ROWS_TTL = 1800
_ROWS_MAX = 4
_rows_cache = OrderedDict()
_rows_lock = RLock()
_redis_rows_client = None

def _rows_redis():
    global _redis_rows_client
    if _redis_rows_client is None:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        from app.core.config import settings
        # Bulk response data needs a different socket budget from tiny app
        # cache entries. The normal client's 2s write timeout drops these
        # payloads mid-transfer, even though Redis is healthy.
        _redis_rows_client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=2.0, socket_timeout=10.0,
            retry=Retry(NoBackoff(), 0), retry_on_timeout=False,
            health_check_interval=30, socket_keepalive=True, max_connections=4,
        )
    return _redis_rows_client


def _redis_enabled():
    from app.core.config import settings
    return bool(settings.REDIS_URL)


def _rows_key(study, unilever_format, study_data=None):
    # Response counters and updated_at change when completions are added/reset.
    # Settings/filters deliberately do not enter this key: these rows are raw.
    revision = "|".join(str(getattr(study, name, None)) for name in (
        "updated_at", "total_responses", "completed_responses", "study_type",
    ))
    # Include definition metadata too: renaming a layer/image/question or
    # changing answer labels must invalidate rows even without new responses.
    definitions = json.dumps(study_data, sort_keys=True, default=str, separators=(",", ":"))
    version = hashlib.sha256((revision + definitions).encode()).hexdigest()[:20]
    return f"offline_export_rows:{study.id}:v2:{int(unilever_format)}:{version}"


def _local_rows(key, stamp=None):
    with _rows_lock:
        cached = _rows_cache.get(key)
        if cached and cached[0] > time.monotonic() and cached[2] == stamp:
            _rows_cache.move_to_end(key)
            return cached[1]
        _rows_cache.pop(key, None)
    return None


def _remember_local_rows(key, packed, stamp=None):
    with _rows_lock:
        _rows_cache[key] = (time.monotonic() + _ROWS_TTL, packed, stamp)
        _rows_cache.move_to_end(key)
        while len(_rows_cache) > _ROWS_MAX:
            _rows_cache.popitem(last=False)


def get_cached_offline_rows(study, unilever_format, study_data=None):
    key = _rows_key(study, unilever_format, study_data)
    if _redis_enabled():
        # Check a tiny shared stamp before using local rows, so invalidation
        # works across workers without repeatedly transferring megabytes.
        try:
            stamp = _rows_redis().get(f"{key}:stamp")
            if not stamp:
                return None
            local = _local_rows(key, stamp)
            if local is not None:
                return local
            raw = _rows_redis().get(key)
            cached = json.loads(raw) if raw else None
            if not cached:
                return None
            if cached.get("format") == "columns-v1-zlib" and cached.get("stamp") == stamp:
                packed = json.loads(zlib.decompress(base64.b64decode(cached["data"])))
                _remember_local_rows(key, packed, stamp)
                return packed
        except Exception:
            logging.getLogger(__name__).debug("HTML export row cache read failed", exc_info=True)
            return None
        return None
    return _local_rows(key)


def has_cached_offline_rows(study, unilever_format, study_data=None):
    key = _rows_key(study, unilever_format, study_data)
    if _redis_enabled():
        try:
            return bool(_rows_redis().get(f"{key}:stamp"))
        except Exception:
            return False
    return _local_rows(key) is not None


def remember_offline_rows(study, unilever_format, df, study_data=None):
    packed = pack_dataframe(df)
    key = _rows_key(study, unilever_format, study_data)
    if _redis_enabled():
        # Compress network traffic/storage as well as row representation.
        raw = json.dumps(packed, separators=(",", ":")).encode()
        stamp = hashlib.sha256(raw).hexdigest()
        compressed = base64.b64encode(zlib.compress(raw, 1)).decode("ascii")
        try:
            _rows_redis().setex(key, _ROWS_TTL, json.dumps({"format": "columns-v1-zlib", "data": compressed, "stamp": stamp}))
            _rows_redis().setex(f"{key}:stamp", _ROWS_TTL, stamp)
            _remember_local_rows(key, packed, stamp)
        except Exception:
            logging.getLogger(__name__).warning("HTML export rows could not be cached; export will load responses again")
    else:
        # Only compact columns are retained, never the large task JSON/frame.
        _remember_local_rows(key, packed)
    return packed


def invalidate_offline_rows(study_id):
    prefix = f"offline_export_rows:{study_id}:"
    with _rows_lock:
        for key in list(_rows_cache):
            if key.startswith(prefix):
                _rows_cache.pop(key, None)
    if _redis_enabled():
        from app.core.cache import RedisCache
        RedisCache.delete_pattern(f"{prefix}*")


def _cell(value):
    # Match slimAnalysisRows in the frontend. Images belong in the image table,
    # never in every task row. Preserve numeric/string/boolean distinctions.
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        return value if value and len(value) <= 4000 and not value.startswith("data:") else None
    return None


def pack_dataframe(df: pd.DataFrame) -> dict:
    """Write the columns-v1 format consumed by the existing TypeScript unpacker.

    Factorize one column at a time, rather than allocating a dict for every row
    or sending repeated field names/values over the network. No filters or
    constant-rater exclusions are applied: the file must be able to undo them.
    """
    columns = []
    duplicates = df.columns.duplicated()
    for position, name in enumerate(df.columns):
        if duplicates[position]:
            continue
        series = df.iloc[:, position]
        if pd.api.types.is_numeric_dtype(series.dtype):
            codes, distinct = pd.factorize(series, sort=False)
            values = [None] + [_cell(value) for value in distinct]
        else:
            # A tuple prevents pandas from merging True with 1 or "1" with 1.
            def cell_key(raw):
                value = _cell(raw)
                return None if value is None else (type(value).__name__, value)

            keys = series.map(cell_key)
            codes, distinct = pd.factorize(keys, sort=False)
            values = [None] + [item[1] for item in distinct]

        if all(value is None for value in values):
            continue
        indexes = (codes + 1).astype(np.uint32)
        bits = next((size for size in (1, 2, 4, 8, 16, 32) if len(values) <= 2 ** size), 32)
        if bits >= 8:
            data = indexes.astype(f"<u{bits // 8}").tobytes()
        else:
            per_byte = 8 // bits
            padded = np.pad(indexes, (0, (-len(indexes)) % per_byte))
            shifts = np.arange(per_byte, dtype=np.uint32) * bits
            data = (padded.reshape(-1, per_byte) << shifts).sum(axis=1).astype(np.uint8).tobytes()
        columns.append({
            "name": str(name), "values": values, "bits": bits,
            "data": base64.b64encode(data).decode("ascii"),
        })
    return {"format": "columns-v1", "count": len(df), "columns": columns}
