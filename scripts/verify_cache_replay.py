"""One-off verification script: inspects the SQLite LLM response cache
(src/core/cache.py's `llm_cache` table) and proves the cache actually
short-circuits a repeat call — no outbound provider call, near-zero latency.

Cache rows only store a hash of the prompt (`cache_key`/`prompt_hash`), never
the raw prompt text (see src/core/cache.py's compute_cache_key) — so there is
no way to "replay" one of the existing historical rows verbatim; the original
prompt that produced it isn't recoverable from the row alone. Requirement 4
is therefore verified with a controlled experiment instead: call
LLMGateway.call() twice with the identical (role, prompt, schema_version),
with the underlying provider client mocked so a "did it actually call the
network" answer is a fact, not an inference from timing alone. A distinct
schema_version ("verify_cache_replay_test") keeps this experiment's cache
row clearly identifiable and never collides with real ingestion/mapping/
judge cache entries in the same cache.db.

Uses the real cache.db this project actually runs against (SQLITE_CACHE_PATH
from .env, default "cache.db") for steps 1-3 (read-only inspection) and for
step 4's experiment (one extra, clearly-labelled row gets added there).

Run: python scripts/verify_cache_replay.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest import mock

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

CACHE_PATH = os.environ.get("SQLITE_CACHE_PATH", "cache.db")


def inspect_schema(conn: sqlite3.Connection) -> list[tuple]:
    """PRAGMA table_info(llm_cache): (cid, name, type, notnull, dflt_value, pk)."""
    return conn.execute("PRAGMA table_info(llm_cache)").fetchall()


def count_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]


def fetch_recent_entries(conn: sqlite3.Connection, limit: int = 5) -> list[tuple]:
    return conn.execute(
        """
        SELECT created_at, model_name, prompt_hash, response_payload
        FROM llm_cache
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def run_cache_hit_experiment() -> dict:
    """Calls LLMGateway.call() twice with an identical (role, prompt,
    schema_version) — a mocked provider client makes "was the network
    actually hit" an assertion, not a timing guess."""
    from src.llm.gateway import LLMGateway

    gateway = LLMGateway()
    test_prompt = f"verify_cache_replay probe — {uuid.uuid4()}"
    fake_response = {"content": "verify_cache_replay: deterministic test response"}

    with mock.patch("src.llm.openai_client.call", return_value=fake_response) as mock_client:
        start_first = time.perf_counter()
        first_result = gateway.call(role="reasoner", prompt=test_prompt, schema_version="verify_cache_replay_test")
        first_elapsed_ms = (time.perf_counter() - start_first) * 1000

        start_second = time.perf_counter()
        second_result = gateway.call(role="reasoner", prompt=test_prompt, schema_version="verify_cache_replay_test")
        second_elapsed_ms = (time.perf_counter() - start_second) * 1000

        outbound_call_count = mock_client.call_count

    return {
        "first_elapsed_ms": first_elapsed_ms,
        "second_elapsed_ms": second_elapsed_ms,
        "outbound_call_count": outbound_call_count,
        "results_match": first_result.get("content") == second_result.get("content"),
    }


def render(
    cache_path: str,
    schema: list[tuple],
    row_count: int,
    recent_entries: list[tuple],
    experiment: dict,
) -> str:
    lines: list[str] = []
    lines.append("# Cache replay verification\n")
    lines.append(f"Cache file: `{cache_path}`\n")

    lines.append("## 1-2. Schema and row count\n")
    if schema:
        lines.append("| cid | name | type | notnull | default | pk |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for cid, name, col_type, notnull, default, pk in schema:
            lines.append(f"| {cid} | {name} | {col_type} | {notnull} | {default} | {pk} |")
    else:
        lines.append("_`llm_cache` table not found — cache.db may not be initialized yet._")
    lines.append(f"\nTotal rows in `llm_cache`: **{row_count}**\n")

    lines.append("## 3. Most recent 5 cached entries\n")
    if recent_entries:
        lines.append("| created_at | model_name | prompt_hash | response_payload (first 100 chars) |")
        lines.append("| --- | --- | --- | --- |")
        for created_at, model_name, prompt_hash, response_payload in recent_entries:
            snippet = (response_payload or "")[:100].replace("\n", " ").replace("|", "\\|")
            short_hash = (prompt_hash or "")[:16] + "..."
            lines.append(f"| {created_at} | {model_name} | `{short_hash}` | {snippet} |")
    else:
        lines.append("_No cached entries found._")
    lines.append("")

    lines.append("## 4. Cache-hit experiment (LLMGateway.call, mocked provider client)\n")
    lines.append(f"- First call (cache miss, populates cache): {experiment['first_elapsed_ms']:.2f} ms")
    lines.append(f"- Second call (identical role/prompt/schema_version): {experiment['second_elapsed_ms']:.2f} ms")
    lines.append(f"- Outbound provider-client calls across both gateway calls: {experiment['outbound_call_count']}")
    lines.append(f"- Both calls returned identical content: {experiment['results_match']}")
    lines.append("")

    verdict_pass = experiment["outbound_call_count"] == 1 and experiment["results_match"]
    lines.append(f"**Verdict: {'PASS' if verdict_pass else 'FAIL'}**")
    if verdict_pass:
        lines.append(
            "- The second call never reached the provider client at all "
            f"(1 outbound call total, not 2) and took {experiment['second_elapsed_ms']:.2f} ms - "
            "the cache short-circuits repeat calls exactly as designed."
        )
    else:
        lines.append(
            "- Expected exactly 1 outbound call across both gateway.call() invocations "
            f"and matching content; got {experiment['outbound_call_count']} outbound call(s), "
            f"results_match={experiment['results_match']}."
        )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    conn = sqlite3.connect(CACHE_PATH)
    try:
        schema = inspect_schema(conn)
        row_count = count_rows(conn) if schema else 0
        recent_entries = fetch_recent_entries(conn) if schema else []
    finally:
        conn.close()

    experiment = run_cache_hit_experiment()

    print(render(CACHE_PATH, schema, row_count, recent_entries, experiment))


if __name__ == "__main__":
    main()
