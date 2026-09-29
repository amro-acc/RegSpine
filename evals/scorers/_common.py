"""Shared helpers for evals/scorers/*.py: golden-data loading and the
RegulatoryObligation/InternalControl construction boilerplate every
mapping/audit-facing scorer needs. Not one of the four named scorer
modules -- internal to evals/ only.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from src.core.schemas import InternalControl, Modality, RegulatoryObligation

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GOLDEN_DIR = REPO_ROOT / "evals" / "golden"

# Execution-provenance filler for synthetic golden-set objects -- these are
# eval fixtures, not real agent output, so created_by_agent/model_id are
# fixed sentinel values rather than anything a real run would produce.
_EVAL_SOURCE_PROVENANCE = {"source_file": "evals/golden", "page_number": 1, "snippet_hash": "golden"}


def load_golden(filename: str) -> list[dict]:
    with open(GOLDEN_DIR / filename, encoding="utf-8") as f:
        return json.load(f)["cases"]


def control_from_golden(entry: dict, bank_id: uuid.UUID | None = None) -> InternalControl:
    """Builds a real InternalControl from a golden_mappings.json
    candidate_controls[] entry. Deterministic id (uuid5 of the golden
    string id) so a case's expected_control_id string can be compared
    against a real returned control_id without a separate lookup table."""
    return InternalControl(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, entry["id"]),
        bank_id=bank_id or uuid.uuid4(),
        control_ref=entry["id"],
        title=entry["title"],
        design_description=entry.get("design_description"),
        control_type=entry.get("control_type"),
        automation=entry.get("automation"),
        frequency=entry.get("frequency"),
        confidence=1.0,
        created_by_agent="golden_eval",
        model_id="golden_eval",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=_EVAL_SOURCE_PROVENANCE,
    )


def obligation_from_text(text: str) -> RegulatoryObligation:
    """Builds a real RegulatoryObligation from a bare obligation string --
    the common case shared by any golden set that only needs obligation_text
    (no candidate controls), e.g. golden_relations.json."""
    return RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text=text,
        verbatim_quote=text,
        modality=Modality.MUST,
        obligation_type="governance",
        confidence=1.0,
        created_by_agent="golden_eval",
        model_id="golden_eval",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=_EVAL_SOURCE_PROVENANCE,
    )


def obligation_from_golden(case: dict) -> RegulatoryObligation:
    """Builds a real RegulatoryObligation from a golden_mappings.json case
    (obligation_text only, no separate source clause in that golden set --
    golden_obligations.json is the one with real source_text/verbatim_quote
    pairs, used directly via IngestionAgent instead of this helper)."""
    return obligation_from_text(case["obligation_text"])
