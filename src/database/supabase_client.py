"""Supabase (Postgres) data-access layer. The only place raw SQL/table writes
happen — agents call these functions, never the Supabase client directly.

Function names follow the Pydantic model names (RegulatoryObligation ->
insert_obligation, InternalControl -> insert_control, etc.). The live tables
are `obligations`, `controls`, `obligation_control_map`, `gaps`,
`remediations`, `review_actions` — every query here targets those real names.

Every insert function requires a fully-populated Pydantic model as input,
which means the mandatory provenance fields (both execution provenance and
the `provenance` source-traceability dict) are enforced by the type system
before this module ever sees the data — there is no code path here that can
write a row without them, because the model constructor already rejected
that possibility.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from supabase import Client, create_client

from src.core.schemas import (
    BankEntity,
    Clause,
    ClauseChange,
    ControlMapping,
    DocumentVersion,
    GapFinding,
    InternalControl,
    ObligationApplicability,
    ObligationRelation,
    RegulatoryDocument,
    RegulatoryObligation,
    RemediationAction,
    Run,
)

TABLE_OBLIGATIONS = "obligations"
TABLE_CONTROLS = "controls"
TABLE_CONTROL_MAPPINGS = "obligation_control_map"
TABLE_GAPS = "gaps"
TABLE_REMEDIATIONS = "remediations"
TABLE_BANK_ENTITIES = "bank_entities"
TABLE_OBLIGATION_APPLICABILITY = "obligation_applicability"
TABLE_RUNS = "runs"
TABLE_REGULATORY_DOCUMENTS = "regulatory_documents"
TABLE_DOCUMENT_VERSIONS = "document_versions"
TABLE_CLAUSES = "clauses"
TABLE_CLAUSE_CHANGES = "clause_changes"
TABLE_REVIEW_ACTIONS = "review_actions"
TABLE_OBLIGATION_RELATIONS = "obligation_relations"


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Lazy singleton — constructed on first use, not at import time, so
    importing this module doesn't require SUPABASE_URL/SUPABASE_SERVICE_KEY
    to already be set (useful for tests that only exercise other functions)."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (see .env.example). "
            "SUPABASE_SERVICE_KEY is server-side only — never load it in the UI process."
        )
    return create_client(url, key)


def _payload(model) -> dict:
    """Serialize a Pydantic model to a JSON-safe dict matching the live
    column names exactly (schemas.py was written to mirror the DDL field for
    field) — UUIDs and dates become strings, enums become their .value."""
    return model.model_dump(mode="json")


# ============ Runs (src/api/main.py calls state_graph.py's compiled graph
# and owns creating the run row; everything downstream just takes run_id as
# a given) ============


def insert_run(run: Run) -> dict:
    response = get_client().table(TABLE_RUNS).insert(_payload(run)).execute()
    return response.data[0]


def update_run_status(run_id: uuid.UUID, status: str, finished_at: datetime | None = None) -> dict:
    payload: dict = {"status": status}
    if finished_at is not None:
        payload["finished_at"] = finished_at.isoformat()
    elif status in ("completed", "failed"):
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    response = get_client().table(TABLE_RUNS).update(payload).eq("id", str(run_id)).execute()
    return response.data[0]


def get_run(run_id: uuid.UUID) -> dict | None:
    response = get_client().table(TABLE_RUNS).select("*").eq("id", str(run_id)).maybe_single().execute()
    return response.data if response else None


# ============ Ad-hoc document scaffolding — RegulatoryObligation.clause_id
# is a required FK, but the API receives raw regulation_text with no
# upstream Document/Clause already ingested. These are thin, direct inserts
# of the existing RegulatoryDocument/DocumentVersion/Clause models, just to
# close the FK chain for API-submitted text so ingest_node's real work has
# somewhere to attach to ============


def insert_regulatory_document(document: RegulatoryDocument) -> dict:
    response = get_client().table(TABLE_REGULATORY_DOCUMENTS).insert(_payload(document)).execute()
    return response.data[0]


def insert_document_version(version: DocumentVersion) -> dict:
    response = get_client().table(TABLE_DOCUMENT_VERSIONS).insert(_payload(version)).execute()
    return response.data[0]


def get_document_version(version_id: uuid.UUID) -> dict | None:
    response = (
        get_client()
        .table(TABLE_DOCUMENT_VERSIONS)
        .select("*")
        .eq("id", str(version_id))
        .maybe_single()
        .execute()
    )
    return response.data if response else None


def insert_clause(clause: Clause) -> dict:
    response = get_client().table(TABLE_CLAUSES).insert(_payload(clause)).execute()
    return response.data[0]


def get_clause(clause_id: uuid.UUID) -> dict | None:
    response = get_client().table(TABLE_CLAUSES).select("*").eq("id", str(clause_id)).maybe_single().execute()
    return response.data if response else None


# ============ Obligations ============


def insert_obligation(obligation: RegulatoryObligation) -> dict:
    response = get_client().table(TABLE_OBLIGATIONS).insert(_payload(obligation)).execute()
    return response.data[0]


def list_obligations_by_run(run_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_OBLIGATIONS).select("*").eq("run_id", str(run_id)).execute()
    return response.data


def get_obligation(obligation_id: uuid.UUID) -> dict | None:
    response = (
        get_client()
        .table(TABLE_OBLIGATIONS)
        .select("*")
        .eq("id", str(obligation_id))
        .maybe_single()
        .execute()
    )
    return response.data if response else None


def list_obligations(review_state: str | None = None, limit: int = 100) -> list[dict]:
    query = get_client().table(TABLE_OBLIGATIONS).select("*").limit(limit)
    if review_state is not None:
        query = query.eq("review_state", review_state)
    return query.execute().data


# ============ Bank entities (applicability_node needs a real row to satisfy
# ObligationApplicability.entity_id's FK; config/bank_profile.yaml is a
# file-based stand-in for the LLM prompt context, not a substitute for the
# actual DB row persistence requires) ============


def insert_bank_entity(entity: BankEntity) -> dict:
    response = get_client().table(TABLE_BANK_ENTITIES).insert(_payload(entity)).execute()
    return response.data[0]


def get_bank_entity(entity_id: uuid.UUID) -> dict | None:
    response = (
        get_client().table(TABLE_BANK_ENTITIES).select("*").eq("id", str(entity_id)).maybe_single().execute()
    )
    return response.data if response else None


def list_bank_entities() -> list[dict]:
    response = get_client().table(TABLE_BANK_ENTITIES).select("*").execute()
    return response.data


# ============ Obligation applicability (ApplicabilityAgent itself stays
# DB-agnostic; the graph orchestration layer is what persists these) ============


def insert_obligation_applicability(record: ObligationApplicability) -> dict:
    response = get_client().table(TABLE_OBLIGATION_APPLICABILITY).insert(_payload(record)).execute()
    return response.data[0]


def get_applicability_for_obligation(obligation_id: uuid.UUID) -> list[dict]:
    response = (
        get_client()
        .table(TABLE_OBLIGATION_APPLICABILITY)
        .select("*")
        .eq("obligation_id", str(obligation_id))
        .execute()
    )
    return response.data


# ============ Controls ============


def insert_control(control: InternalControl) -> dict:
    """Upsert-by-(bank_id, control_ref), not a plain insert: ingest_node
    re-runs against the same policy text and the same bank always re-extract
    the same control_ref (e.g. "INC-07") — the schema's
    unique(bank_id, control_ref) treats that as one canonical control
    identity, not one row per audit run. A plain insert() threw a live 23505
    unique-violation ("controls_bank_id_control_ref_key") the moment a second
    run was made against the same bank+policy — reproduced via the UI, not
    hypothetical.

    Reuses the EXISTING row's id rather than letting the payload's
    freshly-generated uuid4 (InternalControl.id's default_factory) overwrite
    it in place: `id` is a primary key referenced by
    obligation_control_map.control_id (and others) with no `ON UPDATE
    CASCADE` (db/migrations/001_init.sql) — changing it here would either
    hard-fail on any earlier run's existing mapping FK or, if that mapping
    didn't exist yet, silently leave `state["controls"]`'s in-memory id
    unable to resolve back to the real persisted row. Mutates `control.id`
    in place on the update path so the caller's in-memory object (and the
    Chroma upsert that follows it in ingest_node) stays consistent with
    whatever id actually ended up in Postgres."""
    existing = (
        get_client()
        .table(TABLE_CONTROLS)
        .select("id")
        .eq("bank_id", str(control.bank_id))
        .eq("control_ref", control.control_ref)
        .maybe_single()
        .execute()
    )
    if existing and existing.data:
        control.id = uuid.UUID(existing.data["id"])
        response = (
            get_client()
            .table(TABLE_CONTROLS)
            .update(_payload(control))
            .eq("id", str(control.id))
            .execute()
        )
    else:
        response = get_client().table(TABLE_CONTROLS).insert(_payload(control)).execute()
    return response.data[0]


def get_control(control_id: uuid.UUID) -> dict | None:
    response = (
        get_client().table(TABLE_CONTROLS).select("*").eq("id", str(control_id)).maybe_single().execute()
    )
    return response.data if response else None


def list_controls(bank_id: uuid.UUID | None = None, limit: int = 100) -> list[dict]:
    query = get_client().table(TABLE_CONTROLS).select("*").limit(limit)
    if bank_id is not None:
        query = query.eq("bank_id", str(bank_id))
    return query.execute().data


def list_controls_by_run(run_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_CONTROLS).select("*").eq("run_id", str(run_id)).execute()
    return response.data


# ============ Control mappings ============


def insert_control_mapping(mapping: ControlMapping) -> dict:
    response = get_client().table(TABLE_CONTROL_MAPPINGS).insert(_payload(mapping)).execute()
    return response.data[0]


def get_control_mappings_for_obligation(obligation_id: uuid.UUID) -> list[dict]:
    response = (
        get_client()
        .table(TABLE_CONTROL_MAPPINGS)
        .select("*")
        .eq("obligation_id", str(obligation_id))
        .execute()
    )
    return response.data


def list_control_mappings_by_run(run_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_CONTROL_MAPPINGS).select("*").eq("run_id", str(run_id)).execute()
    return response.data


# ============ Gaps ============


def insert_gap(gap: GapFinding) -> dict:
    response = get_client().table(TABLE_GAPS).insert(_payload(gap)).execute()
    return response.data[0]


def update_gap(gap: GapFinding) -> dict:
    """For judge_node: JudgeAgent returns a GapFinding with the same `id` as
    the one audit_node already inserted (ratified or with adjusted
    severity/status) — a second insert() would hit a primary-key collision.
    This updates that row in place rather than duplicating it."""
    payload = _payload(gap)
    payload.pop("id", None)  # id is the match key, not a column to overwrite
    response = get_client().table(TABLE_GAPS).update(payload).eq("id", str(gap.id)).execute()
    return response.data[0]


def get_gap(gap_id: uuid.UUID) -> dict | None:
    response = get_client().table(TABLE_GAPS).select("*").eq("id", str(gap_id)).maybe_single().execute()
    return response.data if response else None


def list_gaps(status: str | None = None, review_state: str | None = None, limit: int = 100) -> list[dict]:
    query = get_client().table(TABLE_GAPS).select("*").limit(limit)
    if status is not None:
        query = query.eq("status", status)
    if review_state is not None:
        query = query.eq("review_state", review_state)
    return query.execute().data


def list_gaps_by_run(run_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_GAPS).select("*").eq("run_id", str(run_id)).execute()
    return response.data


# ============ Remediations ============


def insert_remediation(remediation: RemediationAction) -> dict:
    response = get_client().table(TABLE_REMEDIATIONS).insert(_payload(remediation)).execute()
    return response.data[0]


def get_remediations_for_gap(gap_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_REMEDIATIONS).select("*").eq("gap_id", str(gap_id)).execute()
    return response.data


def list_remediations_by_run(run_id: uuid.UUID) -> list[dict]:
    response = get_client().table(TABLE_REMEDIATIONS).select("*").eq("run_id", str(run_id)).execute()
    return response.data


# ============ Clause changes — src/agents/change_watcher_agent.py is the
# first agent to populate this table. `clause_changes` has no run_id column
# of its own (kept minimal, roadmap-only); the diff run's id is carried
# inside `text_diff` instead ============


def insert_clause_change(change: ClauseChange) -> dict:
    response = get_client().table(TABLE_CLAUSE_CHANGES).insert(_payload(change)).execute()
    return response.data[0]


def list_clause_changes_by_to_version(to_version_id: uuid.UUID) -> list[dict]:
    response = (
        get_client().table(TABLE_CLAUSE_CHANGES).select("*").eq("to_version_id", str(to_version_id)).execute()
    )
    return response.data


# ============ Review actions (audit-trail table for any human or model
# review of an agent output — judge_agent.py is the first writer).
# Deliberately polymorphic (entity_table + entity_id, not a real FK), so
# unlike every insert_* above this takes a plain dict rather than one fixed
# Pydantic model — a single typed ReviewAction schema tied to one entity
# shape would fight that design, not match it. Caller owns populating the
# required columns (entity_table, entity_id, reviewer, action); this
# function does not default or validate them. ============


def insert_review_action(action_payload: dict) -> dict:
    response = get_client().table(TABLE_REVIEW_ACTIONS).insert(action_payload).execute()
    return response.data[0]


# ============ Obligation relations (roadmap — cross-regulation intelligence
# and regulatory contradiction detection; src/agents/obligation_relation_agent.py
# is the first writer). ============


def insert_obligation_relation(relation: ObligationRelation) -> dict:
    response = get_client().table(TABLE_OBLIGATION_RELATIONS).insert(_payload(relation)).execute()
    return response.data[0]
