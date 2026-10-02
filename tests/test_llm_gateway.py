"""Tests for src/llm/gateway.py's retry ladder -- specifically that Gemini
(google provider) gets the widened ~60s/6-attempt profile while every other
provider keeps the original ~7s/3-attempt one. time.sleep is mocked
throughout so this runs instantly regardless of which profile is exercised.
"""

from __future__ import annotations

import json
from unittest import mock

from src.llm.gateway import LLMGateway

_CONFIG = {
    "extractor": {"provider": "openai", "model": "gpt-5.1"},
    "judge_pool": {"provider": "google", "model": "gemini-3.8-flash"},
}


class _FakeUnavailableError(Exception):
    """Mirrors google.genai.errors.ServerError's shape enough for
    _is_transient_provider_error() to recognize it as a 503."""

    code = 503


def _gateway() -> LLMGateway:
    gateway = LLMGateway.__new__(LLMGateway)
    gateway.config = _CONFIG
    return gateway


def test_gemini_retries_up_to_six_times_before_succeeding():
    gateway = _gateway()
    call_count = {"n": 0}

    def flaky_call(model, prompt, temperature=None):
        call_count["n"] += 1
        if call_count["n"] < 6:
            raise _FakeUnavailableError("high demand")
        return {"content": json.dumps({"ok": True})}

    with (
        mock.patch("src.llm.gateway.gemini_client.call", side_effect=flaky_call),
        mock.patch("src.llm.gateway.time.sleep"),  # don't actually wait ~60s in tests
    ):
        result, _ = gateway._invoke(_CONFIG["judge_pool"], "prompt", "schema_v1")

    assert call_count["n"] == 6  # succeeded on the 6th attempt, not fewer
    assert json.loads(result["content"]) == {"ok": True}


def test_gemini_gives_up_after_six_attempts():
    gateway = _gateway()
    call_count = {"n": 0}

    def always_fails(model, prompt, temperature=None):
        call_count["n"] += 1
        raise _FakeUnavailableError("high demand")

    with (
        mock.patch("src.llm.gateway.gemini_client.call", side_effect=always_fails),
        mock.patch("src.llm.gateway.time.sleep"),
    ):
        try:
            gateway._invoke(_CONFIG["judge_pool"], "prompt", "schema_v1")
            raise AssertionError("expected _FakeUnavailableError to propagate")
        except _FakeUnavailableError:
            pass

    assert call_count["n"] == 6  # exactly 6 attempts, no more, no fewer


def test_openai_still_gives_up_after_three_attempts_not_six():
    """Confirms the widened ladder is scoped to google, not applied globally."""
    gateway = _gateway()
    call_count = {"n": 0}

    def always_fails(model, prompt, reasoning_effort=None):
        call_count["n"] += 1
        raise _FakeUnavailableError("high demand")

    with (
        mock.patch("src.llm.gateway.openai_client.call", side_effect=always_fails),
        mock.patch("src.llm.gateway.time.sleep"),
    ):
        try:
            gateway._invoke(_CONFIG["extractor"], "prompt", "schema_v1")
            raise AssertionError("expected _FakeUnavailableError to propagate")
        except _FakeUnavailableError:
            pass

    assert call_count["n"] == 3  # unchanged default ladder


def test_non_transient_error_never_retries_regardless_of_provider():
    gateway = _gateway()
    call_count = {"n": 0}

    def auth_failure(model, prompt, temperature=None):
        call_count["n"] += 1
        raise ValueError("invalid API key")  # no .code -- not transient

    with (
        mock.patch("src.llm.gateway.gemini_client.call", side_effect=auth_failure),
        mock.patch("src.llm.gateway.time.sleep"),
    ):
        try:
            gateway._invoke(_CONFIG["judge_pool"], "prompt", "schema_v1")
            raise AssertionError("expected ValueError to propagate")
        except ValueError:
            pass

    assert call_count["n"] == 1  # fails fast, no retries wasted on a non-transient error
