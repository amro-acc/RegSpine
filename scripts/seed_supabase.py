"""Seed baseline `bank_entities` rows so the UI's bank-profile dropdown and
POST /api/v1/audit's bank_profile_id have real rows to point at.
Deliberately touches ONLY bank_entities — every output/spine table
(obligations, controls, obligation_control_map, gaps, remediations, ...)
must stay empty so the agents populate them autonomously, not this script.

"Meridian Bank USA" values match config/bank_profile.yaml exactly
(jurisdiction, licences, business_lines) — ApplicabilityAgent still reads
its bank-profile *content* from that static file regardless of which
bank_entities row is selected (src/api/main.py's module docstring already
flags this limitation); this script just gives that same synthetic entity
a real DB row so bank_profile_id has something valid to reference. Every
other seeded entity here (e.g. "Meridian Bank Europe SE") is real DB data
only — selecting it changes which entity the output is tagged against, not
which profile attributes ApplicabilityAgent actually reasons over, until
that per-entity limitation is fixed.

BankEntity/bank_entities has no entity_type column (db/migrations/
001_init.sql) — "Credit Institution"-style regulatory classifications are
represented via `licences` instead, the same field "national_bank_charter"
already uses below for this exact purpose.

Idempotent per name: re-running this does not create duplicate rows for a
name already seeded — it prints the existing one's id instead. Without
this check, running `make setup`-adjacent scripts more than once would
silently multiply indistinguishable rows in the UI's dropdown.

Usage:
    python scripts/seed_supabase.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from src.core.schemas import BankEntity  # noqa: E402
from src.database import supabase_client  # noqa: E402

SEED_ENTITIES: list[dict] = [
    {
        "name": "Meridian Bank USA",
        "jurisdiction": "US",
        "licences": ["national_bank_charter"],
        "product_lines": ["retail_banking", "consumer_lending"],
    },
    {
        "name": "Meridian Bank Europe SE",
        "jurisdiction": "EU",
        "licences": ["credit_institution"],
        "product_lines": [],
    },
]


def main() -> None:
    existing_by_name = {row["name"]: row for row in supabase_client.list_bank_entities()}

    for seed in SEED_ENTITIES:
        name = seed["name"]
        existing = existing_by_name.get(name)
        if existing:
            print(f"Already seeded — bank_entities row for '{name}': {existing['id']}")
            continue

        entity = BankEntity(bank_id=uuid.uuid4(), **seed)
        supabase_client.insert_bank_entity(entity)
        print(f"Seeded bank_entities row for '{name}': {entity.id}")


if __name__ == "__main__":
    main()
