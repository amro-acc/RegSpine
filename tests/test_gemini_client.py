"""Verification tests for src/llm/gemini_client.py's multi-key rotation
(2026-09-28): GOOGLE_API_KEY may hold a comma-separated list of keys, each
with its own independent Google free-tier daily quota. Mocks
google.genai.Client itself (the network boundary) so no real API calls
happen; a plain exception with .code/.status attributes stands in for the
real google.genai.errors.ClientError/ServerError shapes, since
_is_quota_exhausted only reads those two attributes -- decoupling the test
from that SDK's exact exception hierarchy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import src.llm.gemini_client as gemini_client  # noqa: E402


class _FakeQuotaExhausted(Exception):
    code = 429
    status = "RESOURCE_EXHAUSTED"


class _FakeHighDemand(Exception):
    code = 503
    status = "UNAVAILABLE"


def _fake_client(response_or_exc):
    """Builds a fake genai.Client(...) whose .models.generate_content(...)
    either returns a fake response object or raises."""
    fake_client = mock.MagicMock()
    if isinstance(response_or_exc, Exception):
        fake_client.models.generate_content.side_effect = response_or_exc
    else:
        fake_response = mock.MagicMock()
        fake_response.text = response_or_exc
        fake_client.models.generate_content.return_value = fake_response
    return fake_client


@pytest.fixture(autouse=True)
def _reset_rotation_index():
    """_current_key_index is module-level, sticky-by-design across calls --
    reset it before/after every test so tests don't leak rotation state into
    each other."""
    gemini_client._current_key_index = 0
    yield
    gemini_client._current_key_index = 0


def test_single_key_success_never_touches_rotation(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a")
    with mock.patch("google.genai.Client", return_value=_fake_client("hello")) as mock_ctor:
        result = gemini_client.call("gemini-3.8-flash", "prompt")

    assert result == {"content": "hello"}
    mock_ctor.assert_called_once_with(api_key="key-a")


def test_single_key_quota_exhausted_raises_immediately_no_rotation_possible(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a")
    with mock.patch("google.genai.Client", return_value=_fake_client(_FakeQuotaExhausted())):
        with pytest.raises(_FakeQuotaExhausted):
            gemini_client.call("gemini-3.8-flash", "prompt")


def test_multi_key_rotates_to_next_key_on_quota_exhaustion(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a,key-b")

    clients = {
        "key-a": _fake_client(_FakeQuotaExhausted()),
        "key-b": _fake_client("succeeded on key-b"),
    }

    with mock.patch("google.genai.Client", side_effect=lambda api_key: clients[api_key]):
        result = gemini_client.call("gemini-3.8-flash", "prompt")

    assert result == {"content": "succeeded on key-b"}


def test_multi_key_stays_on_rotated_key_for_the_next_call(monkeypatch):
    """Sticky rotation: once key-a is exhausted, subsequent calls should
    start directly on key-b, not retry the known-exhausted key-a first."""
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a,key-b")

    clients = {
        "key-a": _fake_client(_FakeQuotaExhausted()),
        "key-b": _fake_client("first call"),
    }
    with mock.patch("google.genai.Client", side_effect=lambda api_key: clients[api_key]):
        gemini_client.call("gemini-3.8-flash", "prompt")

    with mock.patch("google.genai.Client", return_value=_fake_client("second call")) as mock_ctor:
        result = gemini_client.call("gemini-3.8-flash", "prompt")

    assert result == {"content": "second call"}
    mock_ctor.assert_called_once_with(api_key="key-b")


def test_all_keys_exhausted_raises_the_last_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a,key-b")
    clients = {
        "key-a": _fake_client(_FakeQuotaExhausted()),
        "key-b": _fake_client(_FakeQuotaExhausted()),
    }
    with mock.patch("google.genai.Client", side_effect=lambda api_key: clients[api_key]):
        with pytest.raises(_FakeQuotaExhausted):
            gemini_client.call("gemini-3.8-flash", "prompt")


def test_transient_high_demand_error_does_not_trigger_rotation(monkeypatch):
    """A 503 'high demand' is a Google-side capacity issue every key would
    hit identically -- rotating keys wouldn't help, so this must raise
    immediately (leaving backoff-retry to gateway.py) rather than burn
    through the other key for no benefit."""
    monkeypatch.setenv("GOOGLE_API_KEY", "key-a,key-b")
    with mock.patch("google.genai.Client", return_value=_fake_client(_FakeHighDemand())) as mock_ctor:
        with pytest.raises(_FakeHighDemand):
            gemini_client.call("gemini-3.8-flash", "prompt")

    mock_ctor.assert_called_once_with(api_key="key-a")
