"""One-off live verification script: confirms GOOGLE_API_KEY's comma-separated
multi-key rotation (src/llm/gemini_client.py) actually works against the real
Gemini API -- not mocked. Never prints a raw key value; only masked previews
(first 6 + last 4 chars) and length, safe to paste into a terminal/log.

Bypasses src/llm/gateway.py's cache entirely (calls gemini_client.call()
directly) so every call in this script is a guaranteed real network hit,
never a cache replay -- otherwise "it worked" could just mean "it was
cached," which would prove nothing about rotation.

Run: python scripts/verify_gemini_key_rotation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

import src.llm.gemini_client as gemini_client  # noqa: E402

MODEL = "gemini-3.8-flash"


def _masked(key: str) -> str:
    return f"{key[:6]}...{key[-4:]} (length={len(key)})" if len(key) > 12 else "(too short to mask safely)"


def _make_call(label: str) -> None:
    print(f"\n--- {label} ---")
    index_before = gemini_client._current_key_index
    keys = gemini_client._api_keys()
    print(f"_current_key_index before call: {index_before} (of {len(keys)} key(s))")
    try:
        result = gemini_client.call(MODEL, "Reply with exactly the words: rotation test succeeded")
        index_after = gemini_client._current_key_index
        used_key_index = index_after if index_after != index_before else index_before
        print(f"SUCCESS on key[{used_key_index % len(keys)}] ({_masked(keys[used_key_index % len(keys)])})")
        print(f"_current_key_index after call: {index_after}")
        print(f"Response content: {result['content']!r}")
        if index_after != index_before:
            print(f"==> ROTATION OCCURRED: key[{index_before % len(keys)}] failed, key[{index_after % len(keys)}] succeeded.")
        else:
            print(f"==> No rotation needed: key[{index_before % len(keys)}] succeeded directly.")
    except Exception as exc:  # noqa: BLE001 - this script's job is to report, not to handle
        index_after = gemini_client._current_key_index
        print(f"FAILED. code={getattr(exc, 'code', None)!r} status={getattr(exc, 'status', None)!r}")
        print(f"_current_key_index: {index_after} (before this call: {index_before})")
        if index_after != index_before:
            print(
                f"==> Rotation happened DURING this call too: key[{index_before % len(keys)}] was quota-exhausted "
                f"(429), then key[{index_after % len(keys)}] was tried and ALSO failed (shown above) -- this "
                "single exception can hide an internal rotation attempt; check the index change, not just the "
                "final error code, to know what actually happened."
            )
        print(f"Exception: {exc}")


def main() -> None:
    raw = os.environ.get("GOOGLE_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    print(f"Found {len(keys)} key(s) in GOOGLE_API_KEY:")
    for i, k in enumerate(keys):
        print(f"  key[{i}]: {_masked(k)}")
    if len(keys) < 2:
        print("\nOnly one key configured -- add a second comma-separated key to GOOGLE_API_KEY to test rotation.")
        return

    gemini_client._current_key_index = 0  # deterministic starting point for this verification run

    _make_call("Call #1")
    _make_call("Call #2 (confirms stickiness -- should stay on whichever key call #1 landed on)")


if __name__ == "__main__":
    main()
