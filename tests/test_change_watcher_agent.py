"""Verification tests for the (reopened) feature 2 change_watcher agent
(src/agents/change_watcher_agent.py). Mocks only at the network-client
boundary (src.llm.gemini_client.call / src.llm.openai_client.call), same
convention as tests/test_reasoning_agents.py — this proves the real
LLMGateway resolves both EXTRACTOR (added-region extraction) and REASONER
(amended-region materiality) to Foundry GPT-5.1, not a stubbed role.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agents.change_watcher_agent import ChangeWatcherAgent, find_diff_regions  # noqa: E402
from src.core.schemas import (  # noqa: E402
    ChangeType,
    ControlMapping,
    ControlType,
    CoverageLevel,
    InternalControl,
    Materiality,
    Modality,
    RegulatoryObligation,
)
from src.llm.gateway import LLMGateway  # noqa: E402

PROV = {"source_file": "test.txt", "page_number": 1, "snippet_hash": "h"}

OLD_SENTENCE = (
    "Financial entities shall report major ICT-related incidents to the "
    "competent authority within 24 hours of detection."
)
NEW_SENTENCE_AMENDED = (
    "Financial entities shall report major ICT-related incidents to the "
    "competent authority within 12 hours of detection."
)
UNCHANGED_SENTENCE = "Firms must retain incident logs for five years."
NEW_SENTENCE_INSERTED = "Financial entities shall also notify affected clients within 72 hours of a breach."

OLD_TEXT = f"{OLD_SENTENCE} {UNCHANGED_SENTENCE}"
NEW_TEXT = f"{NEW_SENTENCE_AMENDED} {UNCHANGED_SENTENCE} {NEW_SENTENCE_INSERTED}"


def _make_obligation() -> RegulatoryObligation:
    return RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text="Report major ICT incidents within 24 hours",
        verbatim_quote=OLD_SENTENCE,
        modality=Modality.MUST,
        deadline_spec="24 hours of detection",
        obligation_type="ict",
        confidence=0.9,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


def _make_control() -> InternalControl:
    return InternalControl(
        bank_id=uuid.uuid4(),
        control_ref="INC-07",
        title="Incident escalation control",
        design_description="Escalates major incidents to senior management within 48 hours",
        control_type=ControlType.DETECTIVE,
        confidence=0.85,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


def _make_mapping(obligation_id: uuid.UUID, control_id: uuid.UUID) -> ControlMapping:
    return ControlMapping(
        obligation_id=obligation_id,
        control_id=control_id,
        coverage_level=CoverageLevel.FULL,
        rationale="Control escalates incidents, matching the reporting obligation",
        confidence=0.8,
        created_by_agent="mapping_agent",
        model_id="gpt-5.1",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


def test_find_diff_regions_ignores_pure_whitespace_and_case_reflow():
    old_text = "Firms  MUST retain   logs for five years."
    new_text = "firms must retain logs for five years."
    assert find_diff_regions(old_text, new_text) == []


def test_find_diff_regions_detects_replace_and_insert():
    regions = find_diff_regions(OLD_TEXT, NEW_TEXT)
    kinds = [r.kind for r in regions]
    assert "replace" in kinds
    assert "insert" in kinds

    replace_region = next(r for r in regions if r.kind == "replace")
    assert replace_region.old_text == OLD_SENTENCE
    assert replace_region.new_text == NEW_SENTENCE_AMENDED
    assert replace_region.old_start_char == 0
    assert replace_region.old_end_char == len(OLD_SENTENCE)

    insert_region = next(r for r in regions if r.kind == "insert")
    assert insert_region.old_text == ""
    assert insert_region.new_text == NEW_SENTENCE_INSERTED


def test_diff_classifies_amended_region_and_flags_broken_control():
    obligation = _make_obligation()
    control = _make_control()
    mapping = _make_mapping(obligation.id, control.id)
    run_id = uuid.uuid4()

    reasoner_response = {"content": json.dumps({
        "materiality": "high",
        "breaks_control": True,
        "obligation_delta": "reporting deadline tightened from 24h to 12h after detection",
        "rationale": "The control escalates within 48h, which cannot satisfy a 12h deadline",
    })}
    extractor_response = {"content": json.dumps({"obligations": [{
        "obligation_text": "Notify affected clients within 72 hours of a breach",
        "verbatim_quote": NEW_SENTENCE_INSERTED,
        "modality": "must",
        "obligation_type": "conduct",
        "confidence": 0.88,
    }]})}
    # Deterministic re-check of the AMENDED obligation against its EXISTING
    # mapped control (coverage_level=FULL from _make_mapping, so gap_class is
    # model-driven here, not deterministic — spec.md hard invariant #3 only
    # forces gap_class for NONE/PARTIAL coverage).
    amended_audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "control_ineffective",
        "narrative": "Escalation control's 48h window cannot satisfy the tightened 12h reporting deadline",
        "risk_factors": {"regulatory_severity": 4, "enforcement_likelihood": 4, "business_exposure": 3, "control_weakness": 4, "remediation_urgency": 4},
    })}
    # New obligation (from the inserted region) mapped against existing_controls=[control].
    new_obligation_mapping_response = {"content": json.dumps({
        "control_id": str(control.id),
        "coverage_level": "none",
        "rationale": "No control addresses client notification for breaches",
        "cited_control_span": None,
        "confidence": 0.8,
    })}
    new_obligation_audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "no_control",
        "narrative": "No control exists for the new client-notification obligation",
        "risk_factors": {"regulatory_severity": 3, "enforcement_likelihood": 3, "business_exposure": 3, "control_weakness": 3, "remediation_urgency": 3},
    })}

    def _fake_openai_call(model, prompt, **kwargs):
        if "Change-watcher prompt" in prompt:
            return reasoner_response
        if "Mapping prompt" in prompt:
            return new_obligation_mapping_response
        if "Audit prompt" in prompt:
            return amended_audit_response if "12 hours" in prompt else new_obligation_audit_response
        if "Ingestion prompt" in prompt:
            return extractor_response
        raise AssertionError(f"unexpected prompt (no matching fake response): {prompt[:200]!r}")

    with (
        mock.patch("src.llm.openai_client.call", side_effect=_fake_openai_call) as mock_openai,
        mock.patch("src.llm.gemini_client.call") as mock_gemini,
    ):
        agent = ChangeWatcherAgent(gateway=LLMGateway())
        clause_changes, new_obligations, gaps = agent.diff(
            old_text=OLD_TEXT,
            new_text=NEW_TEXT,
            from_version_id=uuid.uuid4(),
            to_version_id=uuid.uuid4(),
            old_clause_id=uuid.uuid4(),
            new_clause_id=uuid.uuid4(),
            obligations=[obligation],
            controls_by_obligation_id={obligation.id: control},
            mappings_by_obligation_id={obligation.id: mapping},
            existing_controls=[control],
            run_id=run_id,
        )

    assert mock_openai.call_count == 5, (
        "materiality assessment + amended-obligation audit + new-obligation "
        "extraction + mapping + audit — all resolve to Foundry GPT"
    )
    assert not mock_gemini.called, "Gemini is reserved for JUDGE only, not EXTRACTOR/REASONER"

    amended = next(c for c in clause_changes if c.change_type == ChangeType.AMENDED)
    assert amended.materiality == Materiality.HIGH
    assert amended.text_diff["breaks_control"] is True
    assert amended.text_diff["affected_obligation_id"] == str(obligation.id)
    assert amended.text_diff["gap_id"] is not None, "the amended-obligation re-check must produce a real gap"

    added = next(c for c in clause_changes if c.change_type == ChangeType.ADDED)
    assert added.materiality == Materiality.HIGH
    assert len(added.text_diff["new_obligation_ids"]) == 1
    assert len(added.text_diff["new_gap_ids"]) == 1, "the new obligation has no covering control -> a gap"

    assert len(new_obligations) == 1
    assert new_obligations[0].verbatim_quote == NEW_SENTENCE_INSERTED

    assert len(gaps) == 2, "one gap from the amended re-check, one from the new obligation's no_control check"
    gap_classes = {gap.gap_class for gap in gaps}
    assert gap_classes == {"control_ineffective", "no_control"}


def test_diff_removed_region_with_no_affected_obligation_is_editorial_and_no_model_call():
    old_text = f"{UNCHANGED_SENTENCE} This sentence is pure boilerplate with no obligation in it."
    new_text = UNCHANGED_SENTENCE

    with (
        mock.patch("src.llm.openai_client.call") as mock_openai,
        mock.patch("src.llm.gemini_client.call") as mock_gemini,
    ):
        agent = ChangeWatcherAgent(gateway=LLMGateway())
        clause_changes, new_obligations, gaps = agent.diff(
            old_text=old_text,
            new_text=new_text,
            from_version_id=uuid.uuid4(),
            to_version_id=uuid.uuid4(),
            old_clause_id=uuid.uuid4(),
            new_clause_id=uuid.uuid4(),
            obligations=[],
            controls_by_obligation_id={},
            mappings_by_obligation_id={},
            existing_controls=[],
            run_id=uuid.uuid4(),
        )

    assert not mock_openai.called
    assert not mock_gemini.called
    assert new_obligations == []
    assert gaps == []
    assert len(clause_changes) == 1
    assert clause_changes[0].change_type == ChangeType.REMOVED
    assert clause_changes[0].materiality == Materiality.EDITORIAL
