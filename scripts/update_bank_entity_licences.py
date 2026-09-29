"""One-off data fix: ApplicabilityAgent dropped the DORA obligation for
"Meridian Bank Europe SE" because its seeded `licences` had nothing an EU
regulatory-scope check could match against ("credit_institution" alone
doesn't signal DORA in-scope status). Updates that row's `licences` to
`["ecb_credit_institution", "dora_financial_entity"]`.

Not folded into scripts/seed_supabase.py's SEED_ENTITIES: that script's
idempotency check is "skip if a row with this name already exists" (by
design, so re-running `make setup` never multiplies rows) — it would never
apply a licences correction to an already-seeded row. A targeted update is
the right tool for correcting one existing row, not a second seed pass.

Usage:
    python scripts/update_bank_entity_licences.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from src.database import supabase_client  # noqa: E402

BANK_NAME = "Meridian Bank Europe SE"
NEW_LICENCES = ["ecb_credit_institution", "dora_financial_entity"]


def main() -> None:
    matches = [row for row in supabase_client.list_bank_entities() if row["name"] == BANK_NAME]
    if not matches:
        print(f"ERROR: no bank_entities row found with name '{BANK_NAME}'", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"ERROR: {len(matches)} rows named '{BANK_NAME}' — refusing to guess which one", file=sys.stderr)
        sys.exit(1)

    entity_id = matches[0]["id"]
    print(f"Before: licences={matches[0]['licences']}")

    response = (
        supabase_client.get_client()
        .table(supabase_client.TABLE_BANK_ENTITIES)
        .update({"licences": NEW_LICENCES})
        .eq("id", entity_id)
        .execute()
    )
    updated = response.data[0]
    print(f"After:  licences={updated['licences']}")
    print(f"Updated bank_entities row '{BANK_NAME}' ({entity_id})")


if __name__ == "__main__":
    main()
