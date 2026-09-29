"""Ad-hoc reproduction script for a POST /api/v1/audit 500 — NOT a test,
not run by `make test`. Sends one real request with mock regulation/policy
text and a real bank_profile_id, then prints the full response body
(main.py's HTTPException detail already stringifies the underlying
exception — see main.py's `except Exception as exc: ... f"Pipeline run
failed: {exc}"` — so this alone usually tells you exactly what failed
without needing to read server logs).

Usage:
    python scripts/debug_audit.py <bank_profile_id>
    python scripts/debug_audit.py           # uses the Meridian Bank USA
                                              # id seeded by scripts/seed_supabase.py
"""

from __future__ import annotations

import sys

import httpx

API_BASE_URL = "http://localhost:8010"
DEFAULT_BANK_PROFILE_ID = "ed6b8dc7-d343-4523-8a15-44bcf765ad26"

MOCK_REGULATION_TEXT = (
    "Financial entities shall report major ICT-related incidents to the competent authority "
    "within 24 hours of detection, in accordance with Article 19 of Regulation (EU) 2022/2554 (DORA)."
)
MOCK_POLICY_TEXT = (
    "Control INC-07: the bank's ICT Incident Response team logs and triages security incidents, "
    "escalating major incidents to senior management within 48 hours of detection."
)


def main() -> None:
    bank_profile_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BANK_PROFILE_ID

    response = httpx.post(
        f"{API_BASE_URL}/api/v1/audit",
        json={
            "regulation_text": MOCK_REGULATION_TEXT,
            "policy_text": MOCK_POLICY_TEXT,
            "bank_profile_id": bank_profile_id,
        },
        timeout=120,
    )

    print(f"status_code: {response.status_code}")
    print(f"body: {response.text}")


if __name__ == "__main__":
    main()
