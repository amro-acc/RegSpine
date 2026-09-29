"""Concrete verification script for Feature 2 (Regulatory Change
Intelligence): runs a real DORA v1 -> v2 snippet through the live audit +
change-diff pipeline and confirms the system flags a control that was
compliant under v1 but is invalidated by v2's tightened reporting window.

Scenario: v1 requires reporting within 24 hours; the existing control
escalates within 12 hours (comfortably covers v1). v2 tightens the window to
4 hours -- the same 12-hour control can no longer satisfy it. This script
asserts the diff endpoint's AMENDED change carries a real gap_id (produced by
re-running AuditAgent against the amended obligation and the existing mapped
control -- src/agents/change_watcher_agent.py, not just a qualitative LLM
flag), and prints the flagged gap's narrative/risk_band.

Unlike tests/test_change_watcher_agent.py (mocked network boundary, no real
DB), this hits the REAL Supabase database and REAL LLM providers -- small
real API cost, writes real rows (a baseline run + a diff run) to whichever
Supabase project DATABASE_URL/SUPABASE_URL point at. Requires
`python scripts/seed_supabase.py` to have been run at least once (needs an
existing bank_entities row).

Run: python scripts/test_regulatory_change.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Windows consoles default to cp1252, which can't encode every character a
# live LLM response might contain (e.g. U+2011 non-breaking hyphen) --
# reconfigure stdout to UTF-8 so a narrative string never crashes printing.
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.database import supabase_client  # noqa: E402

client = TestClient(app)

DORA_V1_TEXT = (
    "Financial entities shall report major ICT-related incidents to the "
    "competent authority within 24 hours of detection."
)
DORA_V2_TEXT = (
    "Financial entities shall report major ICT-related incidents to the "
    "competent authority within 4 hours of detection."
)
POLICY_TEXT = (
    "Control INC-07: the bank's incident response team escalates major "
    "incidents to senior management and the competent authority within 12 "
    "hours of detection, with full documentation."
)


def _get_eu_bank_entity_id() -> str:
    """DORA is an EU ICT-risk regulation -- ApplicabilityAgent correctly
    filters it out (applies=False, jurisdiction mismatch) for a US-jurisdiction
    bank before mapping/audit ever run, which would make this scenario look
    like a retrieval failure rather than a jurisdiction one. Prefer the seeded
    EU entity ("Meridian Bank Europe SE") so the obligation is actually
    applicable and reaches mapping/audit at all; fall back to the first
    entity (with a warning) if no EU-named one exists yet."""
    entities = supabase_client.list_bank_entities()
    if not entities:
        raise RuntimeError("No bank_entities found -- run `python scripts/seed_supabase.py` first.")
    eu_entity = next((e for e in entities if "europe" in e["name"].lower()), None)
    if eu_entity is None:
        print(
            f"WARNING: no EU-named bank_entity found; falling back to '{entities[0]['name']}' "
            "-- DORA may be filtered out as not-applicable for a non-EU entity."
        )
        return entities[0]["id"]
    return eu_entity["id"]


def main() -> None:
    bank_profile_id = _get_eu_bank_entity_id()
    print(f"Using bank_profile_id={bank_profile_id}\n")

    print("== Step 1: baseline audit (DORA v1, 24-hour window) ==")
    audit_response = client.post(
        "/api/v1/audit",
        json={
            "regulation_text": DORA_V1_TEXT,
            "policy_text": POLICY_TEXT,
            "bank_profile_id": bank_profile_id,
        },
    )
    assert audit_response.status_code == 200, f"baseline audit failed: {audit_response.text}"
    audit_body = audit_response.json()
    run_id = audit_body["run_id"]
    print(f"run_id={run_id}")
    print(
        f"obligations={audit_body['obligations_count']} controls={audit_body['controls_count']} "
        f"mappings={audit_body['mappings_count']} gaps={audit_body['gaps_count']}"
    )
    assert audit_body["gaps_count"] == 0, (
        "expected the 12-hour control to fully satisfy the 24-hour v1 requirement with no gap; "
        f"got gaps_count={audit_body['gaps_count']} -- check POLICY_TEXT/DORA_V1_TEXT wording"
    )

    print("\n== Step 2: diff against DORA v2 (4-hour window) ==")
    diff_response = client.post(
        "/api/v1/changes/diff",
        json={"run_id": run_id, "new_regulation_text": DORA_V2_TEXT},
    )
    assert diff_response.status_code == 200, f"diff failed: {diff_response.text}"
    diff_body = diff_response.json()

    print(f"diff_run_id={diff_body['diff_run_id']}")
    print(f"changes_count={len(diff_body['changes'])} gaps_count={diff_body['gaps_count']}")

    amended_changes = [c for c in diff_body["changes"] if c["change_type"] == "amended"]
    assert amended_changes, f"expected an AMENDED change classifying the tightened deadline; got {diff_body['changes']}"
    amended = amended_changes[0]
    print(
        f"\nAMENDED change: materiality={amended['materiality']} "
        f"breaks_control={amended['text_diff'].get('breaks_control')}"
    )
    print(f"  obligation_delta: {amended['text_diff'].get('obligation_delta')}")
    print(f"  rationale: {amended['rationale']}")

    gap_id = amended["text_diff"].get("gap_id")
    assert gap_id is not None, "expected the amended obligation's re-check to attach a real gap_id"

    matching_gaps = [g for g in diff_body["gaps"] if g["id"] == gap_id]
    assert matching_gaps, f"gap_id {gap_id} referenced by the change but missing from the response's gaps list"
    gap = matching_gaps[0]

    print(f"\nFLAGGED GAP: gap_class={gap['gap_class']} risk_band={gap['risk_band']} risk_score={gap['risk_score']}")
    print(f"  narrative: {gap['narrative']}")

    print(
        "\nPASS: the 12-hour control, compliant under the 24-hour v1 window, "
        "is correctly flagged as a gap under the tightened 4-hour v2 window."
    )


if __name__ == "__main__":
    main()
