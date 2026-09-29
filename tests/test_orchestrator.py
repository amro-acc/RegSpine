"""Integration test for Step 6: the full LangGraph orchestration
(ingest -> applicability -> mapping -> audit -> judge -> remediation).

Mocked at the network boundary only (src.llm.gemini_client.call /
src.llm.openai_client.call — same convention as test_reasoning_agents.py)
and at src.database.supabase_client's insert/update functions (kept
hermetic per this step's explicit allowance: "Mock the Supabase network
calls if necessary to keep the test hermetic, but ensure the graph
executes"). ChromaDB, the reranker, span verification, risk scoring, and
LangGraph's own state machine/checkpointer all run for real — this is the
one thing this test actually exists to prove: the wiring between the
DB-agnostic agents and the persistence layer, driven by real graph
execution, not by asserting each piece in isolation again.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agents.state_graph import compiled_graph  # noqa: E402

# applicability_node now fetches the real bank_entities row instead of
# always defaulting to the static config/bank_profile.yaml (per-entity
# profile fix) -- must be mocked or get_bank_entity would hit real Supabase.
_FAKE_BANK_ENTITY_ROW = {
    "name": "Meridian Bank USA",
    "jurisdiction": "US",
    "licences": ["national_bank_charter"],
    "product_lines": ["retail_banking", "consumer_lending"],
}
from src.database import vector_store  # noqa: E402

RAW_REGULATORY_TEXT = (
    "The bank must retain all transaction logs for a minimum of five years "
    "from the date of transaction."
)
RAW_POLICY_TEXT = (
    "Control C-1: the bank operates a log archival system that archives "
    "transaction logs for three years with manual review."
)

OBLIGATIONS_RESPONSE = {
    "obligations": [
        {
            "obligation_text": "Retain transaction logs for at least five years",
            "verbatim_quote": "retain all transaction logs for a minimum of five years",
            "modality": "must",
            "actor": "bank",
            "trigger_condition": None,
            "deadline_spec": "5 years",
            "obligation_type": "recordkeeping",
            "confidence": 0.9,
        }
    ]
}

CONTROLS_RESPONSE = {
    "controls": [
        {
            "control_ref": "C-1",
            "title": "Transaction log retention control",
            "design_description": "Archives transaction logs for three years with manual review",
            "owner": "Ops",
            "control_type": "detective",
            "automation": "semi",
            "frequency": "continuous",
            "verbatim_quote": "archives transaction logs for three years with manual review",
            "confidence": 0.85,
        }
    ]
}

APPLICABILITY_RESPONSE = {
    "applies": True,
    "driver": "product",
    "rationale": "Bank's retail transaction products fall under this obligation's scope",
    "cited_profile_attribute": "business_lines: retail_banking",
    "confidence": 0.9,
}

MAPPING_RESPONSE_TEMPLATE = {
    "coverage_level": "partial",
    "rationale": "Control retains logs for three years; the obligation requires five",
    "cited_control_span": "archives transaction logs for three years",
    "confidence": 0.8,
}

AUDIT_RESPONSE = {
    "has_gap": True,
    "gap_class": "partial_coverage",
    "narrative": "Control only retains logs for three years, short of the obligation's five-year requirement",
    "risk_factors": {
        "regulatory_severity": 4,
        "enforcement_likelihood": 3,
        "business_exposure": 3,
        "control_weakness": 4,
        "remediation_urgency": 3,
    },
}

JUDGE_RESPONSE = {
    "verdict": "ratify",
    "adjusted_severity": None,
    "adjusted_status": None,
    "reasoning": "Finding is well-supported as-is",
}

REMEDIATION_RESPONSE = {
    "action_type": "CONTROL_ENHANCEMENT",
    "action": "Extend log retention from three years to five years to match the obligation",
    "control_design_delta": "Increase retention window in the archival system configuration",
    "owner_role": "Ops",
    "effort_estimate": "1 sprint",
    "test_plan": "Confirm logs older than three years but within five remain retrievable",
    "monitoring_metric": "percentage of transaction logs retrievable at the five-year mark",
    "confidence": 0.85,
}

_CONTROL_ID_RE = re.compile(r"id: ([0-9a-fA-F-]{36})")
# Matches only the rendered *input* line ("chunk_type: regulation          #
# ..."), not the prompt template's own static documentation, which
# describes both shapes ("For `chunk_type: regulation` -- a ... For
# `chunk_type: policy` -- a ...") and would make a plain substring check
# match regardless of which chunk_type was actually rendered.
_CHUNK_TYPE_RE = re.compile(r"chunk_type:\s*(regulation|policy)\s*#")


def _fake_gemini_call(model, prompt, **kwargs):
    """Gemini is now reserved for JUDGE only (judge_pool in config/models.yaml)
    — EXTRACTOR/SUMMARIZER/REASONER all resolve to Foundry GPT-5.1."""
    if "# Judge prompt" in prompt:
        return {"content": json.dumps(JUDGE_RESPONSE)}
    raise AssertionError(f"unexpected gemini prompt (no matching fake response): {prompt[:200]!r}")


def _fake_openai_call(model, prompt, **kwargs):
    chunk_type_match = _CHUNK_TYPE_RE.search(prompt)
    if chunk_type_match:
        if chunk_type_match.group(1) == "regulation":
            return {"content": json.dumps(OBLIGATIONS_RESPONSE)}
        return {"content": json.dumps(CONTROLS_RESPONSE)}
    if "# Applicability prompt" in prompt:
        return {"content": json.dumps(APPLICABILITY_RESPONSE)}
    if "# Mapping prompt" in prompt:
        match = _CONTROL_ID_RE.search(prompt)
        control_id = match.group(1) if match else str(uuid.uuid4())
        return {"content": json.dumps({**MAPPING_RESPONSE_TEMPLATE, "control_id": control_id})}
    if "# Audit prompt" in prompt:
        return {"content": json.dumps(AUDIT_RESPONSE)}
    if "# Remediation prompt" in prompt:
        return {"content": json.dumps(REMEDIATION_RESPONSE)}
    raise AssertionError(f"unexpected openai prompt (no matching fake response): {prompt[:200]!r}")


@pytest.fixture
def isolated_controls_collection(monkeypatch):
    """Disposable Chroma collection so this test never touches the real
    `internal_controls` collection (same convention as test_retrieval.py)."""
    name = f"test_controls_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(vector_store, "COLLECTION_CONTROLS", name)
    yield name
    try:
        vector_store.get_chroma_client().delete_collection(name)
    except Exception:
        pass


def test_full_pipeline_produces_obligations_mappings_gaps_and_remediations(
    tmp_path, isolated_controls_collection
):
    initial_state = {
        "raw_regulatory_text": RAW_REGULATORY_TEXT,
        "raw_policy_text": RAW_POLICY_TEXT,
        "source_file_regulatory": "test_regulation.txt",
        "source_file_policy": "test_policy.txt",
        "clause_id": str(uuid.uuid4()),
        "bank_id": str(uuid.uuid4()),
        "entity_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "obligations": [],
        "controls": [],
        "applicable_obligations": [],
        "applicability_records": [],
        "mappings": [],
        "gaps": [],
        "reviewed_gaps": [],
        "remediations": [],
    }

    checkpoint_path = str(tmp_path / "test_checkpoints.db")

    with (
        mock.patch("src.llm.gemini_client.call", side_effect=_fake_gemini_call),
        mock.patch("src.llm.openai_client.call", side_effect=_fake_openai_call),
        mock.patch("src.database.supabase_client.get_bank_entity", return_value=_FAKE_BANK_ENTITY_ROW),
        mock.patch("src.database.supabase_client.insert_obligation", return_value={}),
        mock.patch("src.database.supabase_client.insert_control", return_value={}),
        mock.patch("src.database.supabase_client.insert_obligation_applicability", return_value={}),
        mock.patch("src.database.supabase_client.insert_control_mapping", return_value={}),
        mock.patch("src.database.supabase_client.insert_gap", return_value={}),
        mock.patch("src.database.supabase_client.update_gap", return_value={}),
        mock.patch("src.database.supabase_client.insert_remediation", return_value={}),
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}),
    ):
        with compiled_graph(checkpoint_path) as app:
            final_state = app.invoke(
                initial_state, config={"configurable": {"thread_id": "test-run-1"}}
            )

    assert len(final_state["obligations"]) == 1
    assert len(final_state["controls"]) == 1
    assert len(final_state["applicable_obligations"]) == 1
    assert len(final_state["applicability_records"]) == 1
    assert final_state["applicability_records"][0].applies is True

    assert len(final_state["mappings"]) == 1
    assert final_state["mappings"][0].control_id == final_state["controls"][0].id
    assert final_state["mappings"][0].coverage_level.value == "partial"

    assert len(final_state["gaps"]) == 1
    assert final_state["gaps"][0].gap_class == "partial_coverage"

    assert len(final_state["reviewed_gaps"]) == 1
    assert final_state["reviewed_gaps"][0].status != "disputed"

    assert len(final_state["remediations"]) == 1
    remediation = final_state["remediations"][0]
    assert remediation.gap_id == final_state["reviewed_gaps"][0].id
    assert remediation.action_type is not None
    assert remediation.monitoring_metric


def test_graph_skips_remediation_for_judge_disputed_gap(tmp_path, isolated_controls_collection):
    """A judge verdict of 'reject' must demote the gap to status='disputed'
    (judge_agent.py) and remediation_node must not draft an action for it —
    a disputed finding is not a ratified one."""
    initial_state = {
        "raw_regulatory_text": RAW_REGULATORY_TEXT,
        "raw_policy_text": RAW_POLICY_TEXT,
        "source_file_regulatory": "test_regulation.txt",
        "source_file_policy": "test_policy.txt",
        "clause_id": str(uuid.uuid4()),
        "bank_id": str(uuid.uuid4()),
        "entity_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "obligations": [],
        "controls": [],
        "applicable_obligations": [],
        "applicability_records": [],
        "mappings": [],
        "gaps": [],
        "reviewed_gaps": [],
        "remediations": [],
    }

    checkpoint_path = str(tmp_path / "test_checkpoints_reject.db")

    def _fake_gemini_call_reject(model, prompt, **kwargs):
        if "# Judge prompt" in prompt:
            return {"content": json.dumps({
                "verdict": "reject",
                "adjusted_severity": None,
                "adjusted_status": None,
                "reasoning": "Insufficient basis for this finding",
            })}
        return _fake_gemini_call(model, prompt, **kwargs)

    with (
        mock.patch("src.llm.gemini_client.call", side_effect=_fake_gemini_call_reject),
        mock.patch("src.llm.openai_client.call", side_effect=_fake_openai_call),
        mock.patch("src.database.supabase_client.get_bank_entity", return_value=_FAKE_BANK_ENTITY_ROW),
        mock.patch("src.database.supabase_client.insert_obligation", return_value={}),
        mock.patch("src.database.supabase_client.insert_control", return_value={}),
        mock.patch("src.database.supabase_client.insert_obligation_applicability", return_value={}),
        mock.patch("src.database.supabase_client.insert_control_mapping", return_value={}),
        mock.patch("src.database.supabase_client.insert_gap", return_value={}),
        mock.patch("src.database.supabase_client.update_gap", return_value={}),
        mock.patch("src.database.supabase_client.insert_remediation", return_value={}),
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}),
    ):
        with compiled_graph(checkpoint_path) as app:
            final_state = app.invoke(
                initial_state, config={"configurable": {"thread_id": "test-run-2"}}
            )

    assert len(final_state["reviewed_gaps"]) == 1
    assert final_state["reviewed_gaps"][0].status == "disputed"
    assert final_state["remediations"] == []
