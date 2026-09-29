"""Verification script for src/llm/gateway.py (Step 2, item 6).

Mocks the actual network calls (src.llm.gemini_client.call /
src.llm.openai_client.call) rather than hitting real APIs — this is checking
routing and fallback *logic*, not live model behavior, and doesn't need real
API keys to run. A day-1 task (separate from this script) is one real live
call per model to confirm the model IDs in config/models.yaml actually work.

Run: python scripts/test_llm_gateway.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("SQLITE_CACHE_PATH", str(REPO_ROOT / "cache.db"))

from src.core import cache  # noqa: E402
from src.llm.gateway import LLMGateway  # noqa: E402


def reset_cache() -> None:
    cache_path = os.environ["SQLITE_CACHE_PATH"]
    if os.path.exists(cache_path):
        os.remove(cache_path)


def test_extractor_routes_to_gpt51() -> None:
    """EXTRACTOR moved from Gemini 3.8 Flash to GPT-5.1 on 2026-09-27
    (spec.md §14.1.3) after sustained real 503s on the Gemini free tier."""
    gateway = LLMGateway()
    with mock.patch("src.llm.openai_client.call") as mock_openai:
        mock_openai.return_value = {"content": "extracted obligation text"}
        result = gateway.call(role="EXTRACTOR", prompt="extract obligations from clause X")

    assert mock_openai.called, "EXTRACTOR must route to openai_client.call"
    assert result["content"] == "extracted obligation text"
    print("PASS: EXTRACTOR routes to GPT-5.1")


def test_judge_routes_to_gemini_via_judge_pool() -> None:
    """JUDGE still needs a different model family from whichever role produced
    the artifact (hard invariant #6). Now that EXTRACTOR/REASONER are both
    OpenAI, judge_pool (gemini-3.8-flash) is what makes that satisfiable."""
    gateway = LLMGateway()
    with (
        mock.patch("src.llm.gemini_client.call") as mock_gemini,
        mock.patch("src.llm.openai_client.call") as mock_openai,
    ):
        mock_gemini.return_value = {"content": "verdict: ratify"}
        gateway.call(role="judge", prompt="review this finding", producer_model="gpt-5.1")

    assert mock_gemini.called, "JUDGE must route to gemini_client.call via judge_pool"
    assert not mock_openai.called, "JUDGE must not call the same family as the producer"
    print("PASS: JUDGE routes to Gemini via judge_pool")


def test_reasoner_primary_success_no_fallback() -> None:
    gateway = LLMGateway()
    with (
        mock.patch("src.llm.openai_client.call") as mock_openai,
        mock.patch("src.llm.gemini_client.call") as mock_gemini,
    ):
        mock_openai.return_value = {"coverage_level": "full", "confidence": 0.92}
        result = gateway.call(role="REASONER", prompt="adjudicate mapping X", schema_version="v_unique_1")

    assert mock_openai.called, "REASONER must route to openai_client.call"
    assert not mock_gemini.called, "REASONER success must not touch Gemini at all"
    assert "status" not in result, "no fallback occurred — must not inject status/hitl_required"
    print("PASS: REASONER primary success routes to GPT-5.1, no fallback injected")


def test_reasoner_fallback_on_primary_failure() -> None:
    gateway = LLMGateway()
    call_log: list[str] = []

    def fake_openai_call(model, prompt, reasoning_effort=None):
        call_log.append(model)
        if model == "gpt-5.1":
            raise RuntimeError("simulated primary failure (e.g. rate limit)")
        return {"coverage_level": "partial", "confidence": 0.7}

    with mock.patch("src.llm.openai_client.call", side_effect=fake_openai_call):
        result = gateway.call(role="REASONER", prompt="adjudicate mapping Y", schema_version="v_unique_2")

    assert call_log == ["gpt-5.1", "gpt-5.6-luna"], f"expected primary-then-fallback call order, got {call_log}"
    assert result["status"] == "proposed", "hard invariant #10: fallback output must be forced to proposed"
    assert result["hitl_required"] is True, "hard invariant #10: fallback output must force hitl_required=True"
    print("PASS: REASONER fallback triggers on primary failure, forces proposed+HITL")


def test_cache_actually_persists_a_row() -> None:
    gateway = LLMGateway()
    with mock.patch("src.llm.openai_client.call") as mock_openai:
        mock_openai.return_value = {"content": "cached response"}
        gateway.call(role="EXTRACTOR", prompt="a unique prompt for cache test", schema_version="v_cache_test")

    import sqlite3

    conn = sqlite3.connect(os.environ["SQLITE_CACHE_PATH"])
    rows = conn.execute(
        "SELECT model_name FROM llm_cache WHERE prompt_hash = ?",
        (cache._sha256("a unique prompt for cache test"),),
    ).fetchall()
    conn.close()

    assert len(rows) == 1, f"expected exactly 1 cache row for this prompt, found {len(rows)}"
    assert rows[0][0] == "gpt-5.1"
    print("PASS: cache.db actually creates a row for a real gateway call")


if __name__ == "__main__":
    reset_cache()
    test_extractor_routes_to_gpt51()
    test_judge_routes_to_gemini_via_judge_pool()
    test_reasoner_primary_success_no_fallback()
    test_reasoner_fallback_on_primary_failure()
    test_cache_actually_persists_a_row()
    reset_cache()
    print("\nAll gateway verification checks passed.")
