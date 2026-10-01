"""SQLite-backed LLM response cache.

Cache key is sha256(model_id|prompt|schema_version). Including
schema_version means a change to a Pydantic output schema naturally
invalidates old cached responses instead of silently returning
stale-shaped data.

The REGSPINE_CACHE on/off toggle lives in src/llm/gateway.py (the
caller), not here — this module just exposes `replay_only` so the
caller can enforce "never touch the live provider" without this
module needing to know why.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_CACHE_PATH = "cache.db"


def _cache_path() -> str:
    return os.environ.get("SQLITE_CACHE_PATH", DEFAULT_CACHE_PATH)


@contextmanager
def _connect():
    conn = sqlite3.connect(_cache_path())
    try:
        yield conn
    finally:
        conn.close()


def init_cache_db() -> None:
    """Create the cache table if it doesn't exist. Safe to call repeatedly."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key TEXT PRIMARY KEY,
                prompt_hash TEXT NOT NULL,
                response_payload TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.commit()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_cache_key(model_name: str, prompt: str, schema_version: str) -> str:
    """sha256(model_id|prompt|schema_version)."""
    return _sha256(f"{model_name}|{prompt}|{schema_version}")


def get_cached_response(cache_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT response_payload FROM llm_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def set_cached_response(
    cache_key: str, prompt_hash: str, response_payload: dict, model_name: str
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache
                (cache_key, prompt_hash, response_payload, model_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cache_key, prompt_hash, json.dumps(response_payload), model_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


class CacheReplayMiss(RuntimeError):
    """Raised when replay_only=True and nothing is cached for this call."""


def cached_llm_call(
    model_name: str,
    prompt: str,
    schema_version: str,
    call_fn: Callable[[], dict],
    force_refresh: bool = False,
    replay_only: bool = False,
) -> dict:
    """Look up (model_name, prompt, schema_version) in the cache; on a miss
    (or force_refresh), call `call_fn()` — which must return a JSON-serializable
    dict — store the result, and return it either way.

    replay_only=True is the demo-replay mode: only ever return what's
    already cached, and never call call_fn. A cache miss raises
    CacheReplayMiss instead of silently falling back to a live call —
    otherwise "offline-safe demo" could quietly turn into a real API
    call mid-demo.

    `call_fn` takes no arguments by design: callers close over whatever they
    need (the actual API client call). This keeps the cache mechanism itself
    provider-agnostic — it has no idea it's caching an LLM call specifically,
    it just caches "the result of calling this function for this key."
    """
    init_cache_db()
    cache_key = compute_cache_key(model_name, prompt, schema_version)

    if replay_only:
        cached = get_cached_response(cache_key)
        if cached is None:
            raise CacheReplayMiss(
                f"REGSPINE_CACHE=on but no cached response for model={model_name!r}, "
                f"schema_version={schema_version!r} — run once with REGSPINE_CACHE=off "
                "first to populate the cache, or turn replay off."
            )
        return cached

    if not force_refresh:
        cached = get_cached_response(cache_key)
        if cached is not None:
            return cached

    response = call_fn()
    set_cached_response(cache_key, _sha256(prompt), response, model_name)
    return response
