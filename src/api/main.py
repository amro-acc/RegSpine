"""FastAPI application layer — the HTTP boundary in front of the LangGraph
orchestration (state_graph.py) and the Supabase persistence layer
(supabase_client.py).

CORSMiddleware explicitly allows the Vite dev server origin
(http://localhost:5173): the React SPA is a browser process and must never
hold SUPABASE_SERVICE_KEY, so it talks to this API only, and this is the
one place that needs a CORS allowance for it.

This module is the real "caller" of state_graph.py's compiled graph outside
a test. clause_id/bank_id/entity_id/run_id are treated as pre-existing,
caller-supplied ids (state_graph.py's own module docstring says this graph
does not create Run/Clause/BankEntity scaffolding). POST /api/v1/audit is
that caller: it creates the run row and an ad-hoc RegulatoryDocument ->
DocumentVersion -> Clause chain so API-submitted regulation_text has
somewhere to attach to, since no batch ingestion/clause_segmenter step runs
ahead of an API request.

`bank_profile_id` is resolved to a real `bank_entities` row (to satisfy the
entity_id FK on ObligationApplicability); applicability_node
(state_graph.py) fetches that same row and passes its actual
jurisdiction/licences/product_lines into ApplicabilityAgent, so selecting a
different bank_profile_id also changes what profile content the model
reasons over, not just which entity the output is tagged against.
`config/bank_profile.yaml` remains the fallback only if a bank_entities row
is somehow missing.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from src.agents.change_watcher_agent import ChangeWatcherAgent
from src.agents.obligation_relation_agent import ObligationRelationAgent
from src.agents.state_graph import compiled_graph
from src.core.schemas import (
    Clause,
    ControlMapping,
    DocClass,
    DocumentVersion,
    InternalControl,
    RegulatoryDocument,
    RegulatoryObligation,
    Run,
)
from src.database import supabase_client

# src.database.supabase_client.get_client() reads SUPABASE_URL/
# SUPABASE_SERVICE_KEY from os.environ lazily, at first request time, not at
# import time — this loads .env explicitly rather than relying on those vars
# already being exported as real shell environment variables.
# scripts/apply_schema.py does the same load_dotenv call, for the same
# reason.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Common Postgres SQLSTATE codes (postgrest.exceptions.APIError.code) worth
# a plain-English translation. Not exhaustive by design: the point is to
# make the FREQUENT cases (a re-run colliding with a unique constraint,
# a bad FK) instantly readable, not to translate every possible Postgres
# error -- anything not listed here gets the generic pipeline-failure
# fallback instead, never raw SQLSTATE/dict text.
_POSTGRES_ERROR_SUMMARIES = {
    "23505": "A record with this identifier already exists (duplicate key).",
    "23503": "The write referenced a record that doesn't exist (foreign key violation).",
    "23502": "A required field was missing (not-null violation).",
    "22P02": "A value had the wrong type or format for its column.",
}


def _extract_llm_error_message(exc: Exception) -> str:
    """Both LLM SDKs used here embed the provider's raw JSON error body
    inside their own default message text (confirmed by reading both
    source trees directly, not assumed): google.genai.errors.APIError's
    __init__ does `super().__init__(f'{code} {status}. {details}')` where
    `details` is the whole parsed response dict, and openai's client builds
    `err_msg = f"Error code: {status} - {body}"` the same way — exactly
    the raw-dict-on-screen problem this function exists to avoid. Both
    SDKs also expose the clean, already-human-readable message text
    separately (google.genai's `.message`, extracted from
    response_json['error']['message'] internally; openai's `.body`, the
    parsed JSON itself) — this reads from those instead of the exception's
    own str()/.message-with-dict-baked-in."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])

    message = getattr(exc, "message", None)
    if isinstance(message, str) and message and not message.startswith("Error code:"):
        return message

    return "no further detail was provided by the API"


def _friendly_client_message(exc: Exception) -> str:
    """Exactly 1-2 plain-English sentences for the browser — never JSON or
    a raw Python/Postgrest/provider dict repr, which is what was reaching
    the UI before this (e.g. a bare {'error': {'code': 503, ...}} dict).
    The full exception WITH TRACEBACK is always logged server-side via
    logger.exception() in the caller regardless of what this returns —
    that's the terminal-visibility half of the fix; this function is only
    the browser-visibility half, and deliberately throws away technical
    precision (SQLSTATE codes, HTTP status internals, stack frames) that
    belongs in the terminal, not the UI."""
    try:
        from google.genai.errors import APIError as GeminiAPIError

        if isinstance(exc, GeminiAPIError):
            if exc.code == 503:
                return (
                    "The Gemini judge model is temporarily overloaded due to high demand. "
                    "This is a Google-side capacity issue, not a bug — please try again in a moment."
                )
            return f"The Gemini judge model returned an error: {exc.message or exc.status}"
    except ImportError:
        pass

    try:
        from openai import APIError as OpenAIAPIError

        if isinstance(exc, OpenAIAPIError):
            return f"The GPT model returned an error: {_extract_llm_error_message(exc)}"
    except ImportError:
        pass

    code = getattr(exc, "code", None)
    if code in _POSTGRES_ERROR_SUMMARIES:
        return f"Database write failed: {_POSTGRES_ERROR_SUMMARIES[code]}"

    return "The compliance audit pipeline encountered an unexpected error. See the server terminal for full details."


app = FastAPI(title="RegSpine API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuditRequest(BaseModel):
    regulation_text: str
    policy_text: str
    bank_profile_id: str


class AuditResponse(BaseModel):
    run_id: str
    status: str
    obligations_count: int
    controls_count: int
    applicable_obligations_count: int
    mappings_count: int
    gaps_count: int
    remediations_count: int


def _create_ad_hoc_clause(regulation_text: str, run_id: uuid.UUID) -> uuid.UUID:
    """RegulatoryObligation.clause_id is a required FK; an API request has no
    upstream Document/Clause already ingested (see module docstring). Every
    call creates a fresh chain (never reused) so the
    unique(document_version_id, clause_ref) constraint never collides."""
    document = RegulatoryDocument(
        regulator="API_SUBMITTED",
        jurisdiction="UNSPECIFIED",
        title="API-submitted regulation text",
        doc_class=DocClass.REGULATION,
    )
    supabase_client.insert_regulatory_document(document)

    version = DocumentVersion(
        document_id=document.id,
        version_label=f"api-{run_id}",
        source_sha256=hashlib.sha256(regulation_text.encode("utf-8")).hexdigest(),
    )
    supabase_client.insert_document_version(version)

    clause = Clause(
        document_version_id=version.id,
        clause_ref=f"API-{run_id}",
        text_content=regulation_text,
        page_no=1,
    )
    supabase_client.insert_clause(clause)
    return clause.id


@app.post("/api/v1/audit", response_model=AuditResponse)
def run_audit(payload: AuditRequest) -> AuditResponse:
    try:
        entity_uuid = uuid.UUID(payload.bank_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bank_profile_id must be a UUID") from exc

    entity_row = supabase_client.get_bank_entity(entity_uuid)
    if entity_row is None:
        raise HTTPException(status_code=404, detail=f"bank_profile_id '{payload.bank_profile_id}' not found")

    run_id = uuid.uuid4()
    supabase_client.insert_run(Run(id=run_id, pipeline="api_audit", status="running"))

    try:
        clause_id = _create_ad_hoc_clause(payload.regulation_text, run_id)

        initial_state = {
            "raw_regulatory_text": payload.regulation_text,
            "raw_policy_text": payload.policy_text,
            "source_file_regulatory": "api_upload",
            "source_file_policy": "api_upload",
            "clause_id": str(clause_id),
            "bank_id": entity_row["bank_id"],
            "entity_id": str(entity_uuid),
            "run_id": str(run_id),
            "obligations": [],
            "controls": [],
            "applicable_obligations": [],
            "applicability_records": [],
            "mappings": [],
            "gaps": [],
            "reviewed_gaps": [],
            "remediations": [],
        }

        with compiled_graph() as reg_agent_app:
            final_state = reg_agent_app.invoke(
                initial_state, config={"configurable": {"thread_id": str(run_id)}}
            )
    except Exception as exc:  # noqa: BLE001 - intentionally broad: any pipeline failure must mark the run failed, not leave it "running" forever
        logger.exception("Audit pipeline failed for run_id=%s", run_id)
        supabase_client.update_run_status(run_id, "failed")
        raise HTTPException(status_code=500, detail=_friendly_client_message(exc)) from exc

    supabase_client.update_run_status(run_id, "completed")

    return AuditResponse(
        run_id=str(run_id),
        status="completed",
        obligations_count=len(final_state["obligations"]),
        controls_count=len(final_state["controls"]),
        applicable_obligations_count=len(final_state["applicable_obligations"]),
        mappings_count=len(final_state["mappings"]),
        gaps_count=len(final_state["reviewed_gaps"]),
        remediations_count=len(final_state["remediations"]),
    )


class BankEntitySummary(BaseModel):
    id: str
    name: str


@app.get("/api/v1/bank_entities", response_model=list[BankEntitySummary])
def list_bank_entities() -> list[BankEntitySummary]:
    """Backs the UI's bank-profile dropdown (see module docstring's
    bank_profile_id caveat — this only lists which entity gets tagged, it
    does not change which profile content ApplicabilityAgent reasons over)."""
    rows = supabase_client.list_bank_entities()
    return [BankEntitySummary(id=row["id"], name=row["name"]) for row in rows]


class ChangeDiffRequest(BaseModel):
    run_id: str
    new_regulation_text: str


class ChangeDiffResponse(BaseModel):
    diff_run_id: str
    from_version_id: str
    to_version_id: str
    changes: list[dict]
    new_obligations_count: int
    gaps: list[dict]
    gaps_count: int


def _create_new_version_for_diff(
    document_id: uuid.UUID, new_regulation_text: str, diff_run_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Reuses the ORIGINAL run's document_id (not a fresh RegulatoryDocument
    like _create_ad_hoc_clause) — this genuinely is meant to be read as "a
    new version of that same regulation," which from/to_version_id on
    ClauseChange only make sense as a pair if both sides trace back to one
    document."""
    version = DocumentVersion(
        document_id=document_id,
        version_label=f"diff-{diff_run_id}",
        source_sha256=hashlib.sha256(new_regulation_text.encode("utf-8")).hexdigest(),
    )
    supabase_client.insert_document_version(version)

    clause = Clause(
        document_version_id=version.id,
        clause_ref=f"DIFF-{diff_run_id}",
        text_content=new_regulation_text,
        page_no=1,
    )
    supabase_client.insert_clause(clause)
    return version.id, clause.id


@app.post("/api/v1/changes/diff", response_model=ChangeDiffResponse)
def diff_regulatory_change(payload: ChangeDiffRequest) -> ChangeDiffResponse:
    """Diffs `new_regulation_text` against the regulation text of a prior,
    already-audited run: classifies each changed region as added/removed/
    amended, judges materiality on amended regions that overlap an existing
    tracked obligation, and flags whether the change plausibly breaks the
    control that obligation was mapped to. See
    src/agents/change_watcher_agent.py's module docstring for the scoping
    tradeoff (sentence-level diffing within one ad-hoc Clause blob, not real
    clause_ref-level alignment — no clause_segmenter exists yet)."""
    try:
        old_run_id = uuid.UUID(payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="run_id must be a UUID") from exc

    obligation_rows = supabase_client.list_obligations_by_run(old_run_id)
    if not obligation_rows:
        raise HTTPException(
            status_code=404, detail=f"No obligations found for run_id '{payload.run_id}' to diff against"
        )

    old_clause_id = uuid.UUID(obligation_rows[0]["clause_id"])
    old_clause = supabase_client.get_clause(old_clause_id)
    if old_clause is None:
        raise HTTPException(status_code=404, detail=f"Source clause for run_id '{payload.run_id}' no longer exists")

    old_version = supabase_client.get_document_version(uuid.UUID(old_clause["document_version_id"]))
    document_id = uuid.UUID(old_version["document_id"])

    obligations = [RegulatoryObligation(**row) for row in obligation_rows]

    mapping_rows = supabase_client.list_control_mappings_by_run(old_run_id)
    mappings_by_obligation_id = {
        uuid.UUID(row["obligation_id"]): ControlMapping(**row) for row in mapping_rows
    }

    controls_by_id: dict[uuid.UUID, InternalControl] = {}
    for mapping in mappings_by_obligation_id.values():
        if mapping.control_id in controls_by_id:
            continue
        control_row = supabase_client.get_control(mapping.control_id)
        if control_row is not None:
            controls_by_id[mapping.control_id] = InternalControl(**control_row)

    controls_by_obligation_id = {
        obligation_id: controls_by_id.get(mapping.control_id)
        for obligation_id, mapping in mappings_by_obligation_id.items()
    }

    # "The existing control set in the database" for delta gap detection
    # (change_watcher_agent.py's added/replace-with-no-affected paths) — the
    # bank's full known control set from the baseline run being diffed
    # against, not just the subset already mapped to an obligation above.
    existing_controls = [InternalControl(**row) for row in supabase_client.list_controls_by_run(old_run_id)]

    diff_run_id = uuid.uuid4()
    supabase_client.insert_run(Run(id=diff_run_id, pipeline="change_diff", status="running"))

    try:
        new_version_id, new_clause_id = _create_new_version_for_diff(
            document_id, payload.new_regulation_text, diff_run_id
        )

        agent = ChangeWatcherAgent()
        clause_changes, new_obligations, gaps = agent.diff(
            old_text=old_clause["text_content"],
            new_text=payload.new_regulation_text,
            from_version_id=uuid.UUID(old_clause["document_version_id"]),
            to_version_id=new_version_id,
            old_clause_id=old_clause_id,
            new_clause_id=new_clause_id,
            obligations=obligations,
            controls_by_obligation_id=controls_by_obligation_id,
            mappings_by_obligation_id=mappings_by_obligation_id,
            existing_controls=existing_controls,
            run_id=diff_run_id,
        )

        for change in clause_changes:
            supabase_client.insert_clause_change(change)
        for obligation in new_obligations:
            supabase_client.insert_obligation(obligation)
        for gap in gaps:
            supabase_client.insert_gap(gap)
    except Exception as exc:  # noqa: BLE001 - any diff-pipeline failure must mark the run failed, not leave it "running" forever
        logger.exception("Change diff failed for diff_run_id=%s", diff_run_id)
        supabase_client.update_run_status(diff_run_id, "failed")
        raise HTTPException(status_code=500, detail=_friendly_client_message(exc)) from exc

    supabase_client.update_run_status(diff_run_id, "completed")

    return ChangeDiffResponse(
        diff_run_id=str(diff_run_id),
        from_version_id=str(old_clause["document_version_id"]),
        to_version_id=str(new_version_id),
        changes=[change.model_dump(mode="json") for change in clause_changes],
        new_obligations_count=len(new_obligations),
        gaps=[gap.model_dump(mode="json") for gap in gaps],
        gaps_count=len(gaps),
    )


# ============ Obligation relations — cross-regulation overlap/conflict
# detection (src/agents/obligation_relation_agent.py's module docstring
# explains why both are one feature apart). `regulator` is caller-supplied
# per run rather than resolved via a multi-table join (obligation -> clause
# -> document_version -> regulatory_document) — the caller (a human who just
# ran two audits) already knows what each run was, and this keeps the
# endpoint from needing a new read path just to re-derive something the UI
# already has. ============


class ObligationRelationGroupRequest(BaseModel):
    run_id: str
    regulator: str


class ObligationRelationsRequest(BaseModel):
    groups: list[ObligationRelationGroupRequest]


class ObligationRelationsResponse(BaseModel):
    obligations_compared_count: int
    relations: list[dict]
    relations_count: int


@app.post("/api/v1/obligations/relations", response_model=ObligationRelationsResponse)
def find_obligation_relations(payload: ObligationRelationsRequest) -> ObligationRelationsResponse:
    """Compares every obligation pair drawn from *different* groups (e.g.
    a DORA run vs. a Basel III run) for overlap/conflict/supersession/
    implementation — never within the same group, since same-source
    obligations are already covered by the main mapping/audit pipeline.
    Persists only genuine relations (relation_type != "unrelated", the
    common case) via insert_obligation_relation; "unrelated" pairs are
    never written or returned, matching MappingAgent's "no candidates ->
    no fabricated row" precedent."""
    if len(payload.groups) < 2:
        raise HTTPException(status_code=422, detail="At least 2 groups (run_id + regulator) are required to compare across")

    obligation_groups: list[list[RegulatoryObligation]] = []
    regulators: list[str] = []
    for group in payload.groups:
        try:
            run_id = uuid.UUID(group.run_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"run_id '{group.run_id}' must be a UUID") from exc

        obligation_rows = supabase_client.list_obligations_by_run(run_id)
        if not obligation_rows:
            raise HTTPException(status_code=404, detail=f"No obligations found for run_id '{group.run_id}'")

        obligation_groups.append([RegulatoryObligation(**row) for row in obligation_rows])
        regulators.append(group.regulator)

    try:
        agent = ObligationRelationAgent()
        relations = agent.compare_across_groups(obligation_groups, regulators)
        for relation in relations:
            supabase_client.insert_obligation_relation(relation)
    except Exception as exc:  # noqa: BLE001 - any failure here must surface a friendly message, not a raw traceback
        logger.exception("Obligation relation comparison failed for groups=%s", [g.run_id for g in payload.groups])
        raise HTTPException(status_code=500, detail=_friendly_client_message(exc)) from exc

    obligations_by_id = {
        obligation.id: obligation for group in obligation_groups for obligation in group
    }
    enriched_relations = []
    for relation in relations:
        obligation_a = obligations_by_id[relation.obligation_a]
        obligation_b = obligations_by_id[relation.obligation_b]
        enriched_relations.append(
            {
                **relation.model_dump(mode="json"),
                "obligation_a_text": obligation_a.obligation_text,
                "obligation_b_text": obligation_b.obligation_text,
            }
        )

    return ObligationRelationsResponse(
        obligations_compared_count=sum(len(group) for group in obligation_groups),
        relations=enriched_relations,
        relations_count=len(relations),
    )


@app.get("/api/v1/lineage/{run_id}")
def get_lineage(run_id: str) -> dict:
    """Structured JSON hierarchy for the traceability graph: Obligations ->
    Mappings (with nested Control) / Gaps (with nested Remediations). Keyed
    by obligation_id throughout, not control_id — gaps have no control_id
    field (schemas.py's GapFinding docstring); the link from a gap back to a
    control goes through the ControlMapping for the same obligation,
    matching state_graph.py's own resolution pattern."""
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="run_id must be a UUID") from exc

    obligations = supabase_client.list_obligations_by_run(run_uuid)
    if not obligations:
        raise HTTPException(status_code=404, detail=f"No data found for run_id '{run_id}'")

    controls = supabase_client.list_controls_by_run(run_uuid)
    mappings = supabase_client.list_control_mappings_by_run(run_uuid)
    gaps = supabase_client.list_gaps_by_run(run_uuid)
    remediations = supabase_client.list_remediations_by_run(run_uuid)

    controls_by_id = {control["id"]: control for control in controls}

    remediations_by_gap: dict[str, list[dict]] = defaultdict(list)
    for remediation in remediations:
        remediations_by_gap[remediation["gap_id"]].append(remediation)

    gaps_by_obligation: dict[str, list[dict]] = defaultdict(list)
    for gap in gaps:
        gaps_by_obligation[gap["obligation_id"]].append(
            {**gap, "remediations": remediations_by_gap.get(gap["id"], [])}
        )

    mappings_by_obligation: dict[str, list[dict]] = defaultdict(list)
    for mapping in mappings:
        mappings_by_obligation[mapping["obligation_id"]].append(
            {**mapping, "control": controls_by_id.get(mapping["control_id"])}
        )

    hierarchy = [
        {
            **obligation,
            "mappings": mappings_by_obligation.get(obligation["id"], []),
            "gaps": gaps_by_obligation.get(obligation["id"], []),
        }
        for obligation in obligations
    ]

    return {"run_id": run_id, "obligations": hierarchy}
