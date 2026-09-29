"""LangGraph orchestration wiring the DB-agnostic agents to Supabase
persistence via the graph state: ingest -> applicability -> mapping ->
audit -> judge -> remediation.

Persistence pattern: every node calls its DB-agnostic agent first, then
immediately persists the result via src/database/supabase_client.py, then
appends the typed object(s) to state. Agents themselves never call
supabase_client — only these node functions do.

Two accumulator fields exist for gaps, not one — `gaps` (audit_node's raw
output) and `reviewed_gaps` (judge_node's output). Reusing one field for both
would double the list under LangGraph's `operator.add` reducer (concatenation,
not replacement): judge_node's per-gap adjustment would land as *additional*
entries alongside audit_node's originals rather than superseding them. Each
node's output gets its own field because each stage produces a new artifact
rather than mutating the last one in place.

Obligation -> control resolution for judge_node/remediation_node goes through
`ControlMapping.obligation_id`, not a `GapFinding.control_id` field — no such
field exists (see schemas.py's GapFinding docstring; the live `gaps` table
has no control_id column either).

Scope boundary (matches ingestion_agent.py's own documented boundary):
clause_id/bank_id/entity_id/run_id are caller-supplied, real, pre-existing
row ids. This graph does not create the upstream Run/Clause/BankEntity
scaffolding those FKs point at — clause_segmenter and run-tracking are out
of scope here, same as ingestion_agent.py's clause_id/bank_id handling.
"""

from __future__ import annotations

import operator
import os
import uuid
from contextlib import contextmanager
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.agents.applicability_agent import ApplicabilityAgent, bank_profile_from_entity_row
from src.agents.audit_agent import AuditAgent
from src.agents.ingestion_agent import IngestionAgent
from src.agents.judge_agent import JudgeAgent
from src.agents.mapping_agent import MappingAgent
from src.agents.remediation_agent import RemediationAgent
from src.core.cache import DEFAULT_CACHE_PATH
from src.core.schemas import (
    ControlMapping,
    GapFinding,
    InternalControl,
    ObligationApplicability,
    RegulatoryObligation,
    RemediationAction,
)
from src.database import supabase_client
from src.database import vector_store


class OverallState(TypedDict):
    # Inputs — caller-supplied, see module docstring's scope boundary note.
    raw_regulatory_text: str
    raw_policy_text: str
    source_file_regulatory: str
    source_file_policy: str
    clause_id: str
    bank_id: str
    entity_id: str
    run_id: str

    # Accumulating outputs, one field per stage (see module docstring).
    obligations: Annotated[list[RegulatoryObligation], operator.add]
    controls: Annotated[list[InternalControl], operator.add]
    applicable_obligations: Annotated[list[RegulatoryObligation], operator.add]
    applicability_records: Annotated[list[ObligationApplicability], operator.add]
    mappings: Annotated[list[ControlMapping], operator.add]
    gaps: Annotated[list[GapFinding], operator.add]
    reviewed_gaps: Annotated[list[GapFinding], operator.add]
    remediations: Annotated[list[RemediationAction], operator.add]


def ingest_node(state: OverallState) -> dict:
    agent = IngestionAgent()
    run_id = uuid.UUID(state["run_id"])
    clause_id = uuid.UUID(state["clause_id"])
    bank_id = uuid.UUID(state["bank_id"])

    obligations = agent.extract_obligations(
        source_text=state["raw_regulatory_text"],
        clause_id=clause_id,
        source_file=state["source_file_regulatory"],
        page_number=1,
        run_id=run_id,
    )
    controls = agent.extract_controls(
        source_text=state["raw_policy_text"],
        bank_id=bank_id,
        source_file=state["source_file_policy"],
        page_number=1,
        run_id=run_id,
    )

    for obligation in obligations:
        supabase_client.insert_obligation(obligation)
    for control in controls:
        supabase_client.insert_control(control)

    if controls:
        # Upsert into Chroma with the control's real id so mapping_node can
        # resolve query_with_rerank's returned ids back to full
        # InternalControl objects via an in-memory lookup, with no redundant
        # DB round-trip.
        vector_store.upsert_documents(
            vector_store.COLLECTION_CONTROLS,
            [
                {
                    "id": str(control.id),
                    "text": f"{control.title}. {control.design_description or ''}".strip(),
                    "metadata": {"control_ref": control.control_ref, "bank_id": str(control.bank_id)},
                }
                for control in controls
            ],
        )

    return {"obligations": obligations, "controls": controls}


def applicability_node(state: OverallState) -> dict:
    entity_id = uuid.UUID(state["entity_id"])

    # Uses the actual entity's profile (not the static
    # config/bank_profile.yaml) so results reflect whichever bank_profile_id
    # was selected. Falls back to the static file only if the entity row is
    # somehow missing (defensive -- main.py already validates the entity
    # exists before invoking this graph).
    entity_row = supabase_client.get_bank_entity(entity_id)
    bank_profile = bank_profile_from_entity_row(entity_row) if entity_row else None
    agent = ApplicabilityAgent(bank_profile=bank_profile)

    applicable, records = agent.evaluate(state["obligations"], entity_id=entity_id)

    for record in records:
        supabase_client.insert_obligation_applicability(record)

    return {"applicable_obligations": applicable, "applicability_records": records}


def mapping_node(state: OverallState) -> dict:
    agent = MappingAgent()
    run_id = uuid.UUID(state["run_id"])
    controls_by_id = {control.id: control for control in state["controls"]}
    mappings: list[ControlMapping] = []

    for obligation in state["applicable_obligations"]:
        results = vector_store.query_with_rerank(vector_store.COLLECTION_CONTROLS, obligation.obligation_text)

        candidates: list[InternalControl] = []
        for result in results:
            try:
                control_id = uuid.UUID(result["id"])
            except (ValueError, KeyError):
                continue
            control = controls_by_id.get(control_id)
            if control is not None:
                candidates.append(control)

        mapping = agent.map(obligation, candidates, run_id=run_id)
        if mapping is not None:
            supabase_client.insert_control_mapping(mapping)
            mappings.append(mapping)

    return {"mappings": mappings}


def audit_node(state: OverallState) -> dict:
    agent = AuditAgent()
    run_id = uuid.UUID(state["run_id"])
    controls_by_id = {control.id: control for control in state["controls"]}
    mapping_by_obligation = {mapping.obligation_id: mapping for mapping in state["mappings"]}
    gaps: list[GapFinding] = []

    for obligation in state["applicable_obligations"]:
        # Every applicable obligation is audited, not just ones that got a
        # mapping: MappingAgent.map() returns None when zero candidates
        # existed at all — the most severe no_control case there is, and
        # skipping it here would silently drop the worst gaps. See
        # audit_agent.py's mapping/control Optional handling.
        mapping = mapping_by_obligation.get(obligation.id)
        control = controls_by_id.get(mapping.control_id) if mapping else None

        gap = agent.audit(obligation, control, mapping, run_id=run_id)
        if gap is not None:
            supabase_client.insert_gap(gap)
            gaps.append(gap)

    return {"gaps": gaps}


def judge_node(state: OverallState) -> dict:
    agent = JudgeAgent()
    obligations_by_id = {obligation.id: obligation for obligation in state["obligations"]}
    controls_by_id = {control.id: control for control in state["controls"]}
    mapping_by_obligation = {mapping.obligation_id: mapping for mapping in state["mappings"]}
    reviewed_gaps: list[GapFinding] = []

    for gap in state["gaps"]:
        obligation = obligations_by_id[gap.obligation_id]
        mapping = mapping_by_obligation.get(gap.obligation_id)
        control = controls_by_id.get(mapping.control_id) if mapping else None

        reviewed = agent.review(gap, obligation, control)
        # judge_agent.review() returns a GapFinding with the SAME id
        # audit_node already inserted — update_gap does an UPDATE-by-id,
        # not a second insert (which would hit a primary-key collision).
        supabase_client.update_gap(reviewed)
        reviewed_gaps.append(reviewed)

    return {"reviewed_gaps": reviewed_gaps}


def remediation_node(state: OverallState) -> dict:
    agent = RemediationAgent()
    run_id = uuid.UUID(state["run_id"])
    obligations_by_id = {obligation.id: obligation for obligation in state["obligations"]}
    controls_by_id = {control.id: control for control in state["controls"]}
    mapping_by_obligation = {mapping.obligation_id: mapping for mapping in state["mappings"]}
    remediations: list[RemediationAction] = []

    for gap in state["reviewed_gaps"]:
        if gap.status == "disputed":
            # Rejected by the judge — not a ratified finding, no
            # remediation is drafted for something under dispute.
            continue

        obligation = obligations_by_id[gap.obligation_id]
        mapping = mapping_by_obligation.get(gap.obligation_id)
        control = controls_by_id.get(mapping.control_id) if mapping else None

        remediation = agent.remediate(gap, obligation, control, run_id=run_id)
        supabase_client.insert_remediation(remediation)
        remediations.append(remediation)

    return {"remediations": remediations}


def _build_graph() -> StateGraph:
    graph = StateGraph(OverallState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("applicability", applicability_node)
    graph.add_node("mapping", mapping_node)
    graph.add_node("audit", audit_node)
    graph.add_node("judge", judge_node)
    graph.add_node("remediation", remediation_node)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "applicability")
    graph.add_edge("applicability", "mapping")
    graph.add_edge("mapping", "audit")
    graph.add_edge("audit", "judge")
    graph.add_edge("judge", "remediation")
    graph.add_edge("remediation", END)
    return graph


@contextmanager
def compiled_graph(checkpointer_path: str | None = None):
    """SqliteSaver.from_conn_string is a context manager
    (`(conn_string: str) -> Iterator[SqliteSaver]`), not a plain factory.
    Compilation and every invocation must happen inside its `with` block, so
    this wraps that instead of leaking the requirement to every caller:
    `with compiled_graph(path) as app: app.invoke(...)`.

    Reuses the same cache.db path convention as src/core/cache.py
    (SQLITE_CACHE_PATH env var) — checkpoints and the LLM cache share one
    SQLite file, in different tables.
    """
    path = checkpointer_path or os.environ.get("SQLITE_CACHE_PATH", DEFAULT_CACHE_PATH)
    with SqliteSaver.from_conn_string(path) as checkpointer:
        yield _build_graph().compile(checkpointer=checkpointer)
