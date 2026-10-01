"""Shared pytest fixtures for the whole tests/ directory.

isolated_llm_cache is autouse: every test gets its own throwaway SQLite
cache file. Without this, cache.py's persistent cache.db means a response
cached by one test can silently short-circuit a *different* test's mock in
a later run — cached_llm_call() returns the hit and never calls call_fn() at
all, so the mock's assertions on "was it called" fail even though the
agent's real logic is fine.

We hit this for real: two tests in tests/test_reasoning_agents.py passed when
that file was run alone, then failed when the full suite ran afterward,
because a stale cache entry from the earlier standalone run (same model/
prompt/schema_version -> same cache key) served instead of hitting the
fresh mock.

Same fixture also pins REGSPINE_CACHE to "off". test_api.py imports
src.api.main, which calls load_dotenv() at import time — if the developer's
own .env has REGSPINE_CACHE=on (demo-replay mode), that leaks into every
test that runs afterward in the same pytest process, and gateway.py then
refuses to call the mocked LLM clients because each test's fresh, empty
cache has nothing to replay.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture(autouse=True)
def isolated_llm_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / f"test_cache_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("SQLITE_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("REGSPINE_CACHE", "off")
    yield
    # tmp_path is pytest's own per-test temp dir and gets cleaned up by
    # pytest itself eventually, but remove explicitly rather than rely on that.
    if cache_path.exists():
        cache_path.unlink()
