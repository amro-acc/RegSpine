"""One-off verification script: confirms the compliance-audit pipeline
persisted rows across the full obligation -> control -> mapping -> gap ->
remediation chain for a multi-clause run, and dumps the most recent gap/
remediation row in full to check provenance/metadata and JUDGE-related
fields survived intact.

`gaps`/`remediations` have no `created_at` column of their own (confirmed
via scripts/inspect_db_schema.py) -- "most recent" is resolved by joining to
`runs.started_at` via `run_id`, not by row `id` (UUIDs aren't chronologically
sortable) or insertion order (not guaranteed by Postgres without an ORDER BY).

Read-only. Reads DATABASE_URL from .env, same connection scripts/
inspect_db_schema.py uses.

Run: python scripts/verify_persistence.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

TABLES = ["obligations", "controls", "obligation_control_map", "gaps", "remediations"]

# The "3-2-3-3-3" breakdown the multi-clause test run is expected to have
# produced, scoped to that one run (not lifetime totals across every run
# this database has ever seen).
EXPECTED_COUNTS_FOR_RUN = {
    "obligations": 3,
    "controls": 2,
    "obligation_control_map": 3,
    "gaps": 3,
    "remediations": 3,
}


def fetch_row_counts(cur) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - TABLES is a fixed internal list, not user input
        counts[table] = cur.fetchone()[0]
    return counts


def fetch_latest_run_id(cur) -> str | None:
    cur.execute("SELECT id FROM runs ORDER BY started_at DESC NULLS LAST LIMIT 1")
    row = cur.fetchone()
    return str(row[0]) if row else None


def fetch_counts_for_run(cur, run_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id = %s", (run_id,))  # noqa: S608
        counts[table] = cur.fetchone()[0]
    return counts


def fetch_latest_row(cur, table: str) -> dict | None:
    """Most recent row in `table`, defined as belonging to the most recently
    started run — table has no timestamp of its own to order by directly."""
    cur.execute(
        f"""
        SELECT t.* FROM {table} t
        JOIN runs r ON r.id = t.run_id
        ORDER BY r.started_at DESC NULLS LAST
        LIMIT 1
        """  # noqa: S608
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _json_default(value):
    return str(value)


def render(
    total_counts: dict[str, int],
    run_id: str | None,
    per_run_counts: dict[str, int] | None,
    latest_gap: dict | None,
    latest_remediation: dict | None,
) -> str:
    lines: list[str] = []
    lines.append("# Persistence verification\n")

    lines.append("## 1. Total row counts (all runs, lifetime)\n")
    lines.append("| Table | Row count |")
    lines.append("| --- | --- |")
    for table in TABLES:
        lines.append(f"| `{table}` | {total_counts[table]} |")
    lines.append("")

    lines.append("## 2. Row counts for the most recent run\n")
    if run_id and per_run_counts is not None:
        lines.append(f"Most recent run: `{run_id}`\n")
        lines.append("| Table | Expected | Actual | Match? |")
        lines.append("| --- | --- | --- | --- |")
        for table in TABLES:
            expected = EXPECTED_COUNTS_FOR_RUN[table]
            actual = per_run_counts[table]
            match = "PASS" if actual == expected else "FAIL"
            lines.append(f"| `{table}` | {expected} | {actual} | {match} |")
    else:
        lines.append("_No rows in `runs` -- cannot scope to a specific run._")
    lines.append("")

    lines.append("## 3. Most recent `gaps` row (full contents)\n")
    if latest_gap:
        lines.append("```json")
        lines.append(json.dumps(latest_gap, indent=2, default=_json_default))
        lines.append("```\n")
        lines.append("**Provenance/metadata check:**")
        lines.append(f"- `provenance`: {'present' if latest_gap.get('provenance') else 'MISSING'}")
        lines.append(f"- `model_id` (producer, e.g. gpt-5.1): {latest_gap.get('model_id') or 'MISSING'}")
        lines.append(f"- `prompt_version`: {latest_gap.get('prompt_version') or 'MISSING'}")
        lines.append(
            f"- `status` (judge-outcome signal -- 'disputed' means the judge rejected "
            f"the finding): {latest_gap.get('status')}"
        )
        lines.append(f"- `risk_band` (judge can adjust this from the producer's original): {latest_gap.get('risk_band')}")
        lines.append(
            "- No dedicated 'judge model' / 'cross-family verdict' column exists on `gaps` "
            "(confirmed against the live schema) -- `judge_agent.py` only ever adjusts "
            "`risk_band`/`status` in place, it doesn't persist which model reviewed it or "
            "its raw verdict. `status`/`risk_band` above are the only observable trace of "
            "judge activity in this table today."
        )
    else:
        lines.append("_No rows in `gaps`._")
    lines.append("")

    lines.append("## 4. Most recent `remediations` row (full contents)\n")
    if latest_remediation:
        lines.append("```json")
        lines.append(json.dumps(latest_remediation, indent=2, default=_json_default))
        lines.append("```\n")
        lines.append("**Provenance/metadata check:**")
        lines.append(f"- `provenance`: {'present' if latest_remediation.get('provenance') else 'MISSING'}")
        lines.append(f"- `model_id`: {latest_remediation.get('model_id') or 'MISSING'}")
        lines.append(f"- `prompt_version`: {latest_remediation.get('prompt_version') or 'MISSING'}")
    else:
        lines.append("_No rows in `remediations`._")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as plain_cur:
            total_counts = fetch_row_counts(plain_cur)
            run_id = fetch_latest_run_id(plain_cur)
            per_run_counts = fetch_counts_for_run(plain_cur, run_id) if run_id else None

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as dict_cur:
            latest_gap = fetch_latest_row(dict_cur, "gaps")
            latest_remediation = fetch_latest_row(dict_cur, "remediations")
    finally:
        conn.close()

    print(render(total_counts, run_id, per_run_counts, latest_gap, latest_remediation))


if __name__ == "__main__":
    main()
