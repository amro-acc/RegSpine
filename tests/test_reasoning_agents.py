"""Tests for the mapping, audit, and judge agents.

Unlike test_ingestion_applicability.py's tests (which mocked at the agent's
gateway.call() boundary), these use REAL LLMGateway instances with mocks
only at the network-client boundary (src.llm.gemini_client.call /
src.llm.openai_client.call) — the only way to actually verify the gateway
resolved the correct model string per role.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agents.audit_agent import AuditAgent  # noqa: E402
from src.agents.judge_agent import JudgeAgent  # noqa: E402
from src.agents.mapping_agent import MappingAgent  # noqa: E402
from src.core.schemas import (  # noqa: E402
    ControlType,
    CoverageLevel,
    InternalControl,
    Modality,
    RegulatoryObligation,
    ReviewState,
    RiskSeverity,
)
from src.llm.gateway import LLMGateway  # noqa: E402

PROV = {"source_file": "test.txt", "page_number": 1, "snippet_hash": "h"}


def _make_obligation(text: str = "must retain records for five years") -> RegulatoryObligation:
    return RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text=text,
        verbatim_quote="retain records",
        modality=Modality.MUST,
        obligation_type="recordkeeping",
        confidence=0.9,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


def _make_control(title: str = "Records retention control") -> InternalControl:
    return InternalControl(
        bank_id=uuid.uuid4(),
        control_ref="C-TEST",
        title=title,
        control_type=ControlType.PREVENTIVE,
        confidence=0.85,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


# ============ Mapping agent ============


def test_mapping_agent_routes_to_gpt51_and_produces_valid_mapping():
    obligation = _make_obligation()
    control = _make_control()

    fake_response = {"content": json.dumps({
        "control_id": str(control.id),
        "coverage_level": "full",
        "rationale": "Control retains records matching the obligation's five-year requirement",
        "cited_control_span": None,
        "confidence": 0.9,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=fake_response) as mock_openai:
        gateway = LLMGateway()
        agent = MappingAgent(gateway=gateway)
        mapping = agent.map(obligation, [control], run_id=uuid.uuid4())

    assert mock_openai.called
    called_model = mock_openai.call_args.args[0] if mock_openai.call_args.args else mock_openai.call_args.kwargs.get("model")
    assert called_model == "gpt-5.1"

    assert mapping is not None
    assert mapping.coverage_level == CoverageLevel.FULL
    assert mapping.review_state == ReviewState.PROPOSED
    assert mapping.control_id == control.id


def test_mapping_agent_downgrades_full_coverage_below_confidence_floor():
    obligation = _make_obligation()
    control = _make_control()

    # confidence 0.5 is below config/pipeline.yaml's full_coverage_confidence_floor (0.75)
    fake_response = {"content": json.dumps({
        "control_id": str(control.id),
        "coverage_level": "full",
        "rationale": "Plausible but uncertain match",
        "cited_control_span": None,
        "confidence": 0.5,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=fake_response):
        agent = MappingAgent(gateway=LLMGateway())
        mapping = agent.map(obligation, [control], run_id=uuid.uuid4())

    assert mapping.coverage_level == CoverageLevel.PARTIAL, "low-confidence 'full' must be downgraded"
    assert mapping.review_state == ReviewState.NEEDS_REVIEW


def test_mapping_agent_returns_none_with_no_candidates():
    agent = MappingAgent(gateway=LLMGateway())
    assert agent.map(_make_obligation(), [], run_id=uuid.uuid4()) is None


# ============ Audit agent ============


def test_audit_agent_routes_to_gpt51_and_produces_valid_gap_finding():
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id,
        control_id=control.id,
        coverage_level=CoverageLevel.NONE,
        rationale="No matching control found",
        confidence=0.8,
        created_by_agent="mapping_agent",
        model_id="gpt-5.1",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )

    fake_response = {"content": json.dumps({
        "has_gap": True,
        "gap_class": "control_no_evidence",  # deliberately wrong - coverage_level=none must force "no_control"
        "narrative": "No control addresses this obligation",
        "risk_factors": {
            "regulatory_severity": 4, "enforcement_likelihood": 3,
            "business_exposure": 3, "control_weakness": 5, "remediation_urgency": 3,
        },
    })}

    with mock.patch("src.llm.openai_client.call", return_value=fake_response) as mock_openai:
        agent = AuditAgent(gateway=LLMGateway())
        gap = agent.audit(obligation, control, mapping, run_id=uuid.uuid4())

    assert mock_openai.called
    called_model = mock_openai.call_args.args[0] if mock_openai.call_args.args else mock_openai.call_args.kwargs.get("model")
    assert called_model == "gpt-5.1"

    assert gap is not None
    assert gap.gap_class == "no_control", "coverage_level=none must force the deterministic class, not the model's suggestion"
    assert gap.risk_score > 0
    assert isinstance(gap.risk_band, RiskSeverity)
    assert gap.model_id == "gpt-5.1"


def test_audit_agent_returns_none_when_full_coverage_and_no_gap_found():
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id,
        control_id=control.id,
        coverage_level=CoverageLevel.FULL,
        rationale="Well covered",
        confidence=0.95,
        created_by_agent="mapping_agent",
        model_id="gpt-5.1",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )
    fake_response = {"content": json.dumps({"has_gap": False, "narrative": "", "risk_factors": None})}

    with mock.patch("src.llm.openai_client.call", return_value=fake_response):
        agent = AuditAgent(gateway=LLMGateway())
        gap = agent.audit(obligation, control, mapping, run_id=uuid.uuid4())

    assert gap is None, "full coverage + no model-found deficiency must not fabricate a gap"


# ============ Judge agent ============


def test_judge_agent_processes_gap_finding_and_enforces_model_family_independence():
    obligation = _make_obligation()
    control = _make_control()

    audit_response = {"content": json.dumps({
        "has_gap": True,
        "gap_class": "control_ineffective",
        "narrative": "Control frequency is insufficient for the obligation's requirement",
        "risk_factors": {
            "regulatory_severity": 4, "enforcement_likelihood": 4,
            "business_exposure": 3, "control_weakness": 4, "remediation_urgency": 3,
        },
    })}
    judge_response = {"content": json.dumps({
        "verdict": "adjust",
        "adjusted_severity": "CRITICAL",
        "adjusted_status": None,
        "reasoning": "Regulatory severity and enforcement likelihood justify escalating to CRITICAL",
    })}

    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id, control_id=control.id, coverage_level=CoverageLevel.FULL,
        rationale="r", confidence=0.9, created_by_agent="mapping_agent", model_id="gpt-5.1",
        prompt_version="v1", run_id=uuid.uuid4(), provenance=PROV,
    )

    with mock.patch("src.llm.openai_client.call", return_value=audit_response) as mock_openai:
        audit_agent = AuditAgent(gateway=LLMGateway())
        gap = audit_agent.audit(obligation, control, mapping, run_id=uuid.uuid4())
    assert gap.model_id == "gpt-5.1"  # confirms the producer model judge must differ from

    with mock.patch("src.llm.gemini_client.call", return_value=judge_response) as mock_gemini, \
         mock.patch("src.llm.openai_client.call") as mock_openai_judge, \
         mock.patch("src.database.supabase_client.insert_review_action") as mock_insert_review:
        judge_agent = JudgeAgent(gateway=LLMGateway())
        reviewed = judge_agent.review(gap, obligation, control)

    assert mock_gemini.called, "judge must resolve to Gemini when the producer was gpt-5.1 (different family)"
    assert not mock_openai_judge.called, "judge must NOT call the same family as the producer"

    assert reviewed.risk_band == RiskSeverity.CRITICAL
    assert reviewed.gap_class == gap.gap_class, "judge can adjust severity/status, not gap_class"
    assert reviewed.risk_factors == gap.risk_factors, "judge cannot change the underlying risk factors"

    assert mock_insert_review.called, "every verdict must write a review_actions audit row"
    review_payload = mock_insert_review.call_args.args[0]
    assert review_payload["entity_table"] == "gaps"
    assert review_payload["entity_id"] == str(gap.id)
    assert review_payload["reviewer"] == "gemini-3.8-flash", "reviewer must be the model that actually judged, not the producer"
    assert review_payload["action"] == "amend", "verdict='adjust' maps to the review_actions vocabulary (accept|reject|amend)"
    assert review_payload["corrected_output"]["raw_verdict"] == "adjust", "raw model verdict preserved even though 'action' is normalized"
    assert review_payload["original_output"]["risk_band"] == gap.risk_band.value, "snapshot must be the PRE-judge state, not the post-mutation one"


def test_judge_agent_ratify_leaves_finding_unchanged():
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id, control_id=control.id, coverage_level=CoverageLevel.NONE,
        rationale="r", confidence=0.8, created_by_agent="mapping_agent", model_id="gpt-5.1",
        prompt_version="v1", run_id=uuid.uuid4(), provenance=PROV,
    )
    audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "no_control", "narrative": "n",
        "risk_factors": {"regulatory_severity": 3, "enforcement_likelihood": 3, "business_exposure": 3, "control_weakness": 3, "remediation_urgency": 3},
    })}
    with mock.patch("src.llm.openai_client.call", return_value=audit_response):
        gap = AuditAgent(gateway=LLMGateway()).audit(obligation, control, mapping, run_id=uuid.uuid4())

    ratify_response = {"content": json.dumps({
        "verdict": "ratify", "adjusted_severity": None, "adjusted_status": None,
        "reasoning": "Finding is well-supported as-is",
    })}
    with mock.patch("src.llm.gemini_client.call", return_value=ratify_response), \
         mock.patch("src.database.supabase_client.insert_review_action"):
        reviewed = JudgeAgent(gateway=LLMGateway()).review(gap, obligation, control)

    assert reviewed.risk_band == gap.risk_band
    assert reviewed.status == gap.status


def test_judge_agent_tolerates_stringified_null_instead_of_crashing():
    """Reproduced live against a real judge_pool (Gemini) call: the model
    returned the JSON string "null" for adjusted_severity instead of the JSON
    literal null, and RiskSeverity("null") crashed the entire audit run with
    ValueError: 'null' is not a valid RiskSeverity. A stringified null must be
    treated the same as a real null (no change), not propagate as a crash."""
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id, control_id=control.id, coverage_level=CoverageLevel.NONE,
        rationale="r", confidence=0.8, created_by_agent="mapping_agent", model_id="gpt-5.1",
        prompt_version="v1", run_id=uuid.uuid4(), provenance=PROV,
    )
    audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "no_control", "narrative": "n",
        "risk_factors": {"regulatory_severity": 3, "enforcement_likelihood": 3, "business_exposure": 3, "control_weakness": 3, "remediation_urgency": 3},
    })}
    with mock.patch("src.llm.openai_client.call", return_value=audit_response):
        gap = AuditAgent(gateway=LLMGateway()).audit(obligation, control, mapping, run_id=uuid.uuid4())

    stringified_null_response = {"content": json.dumps({
        "verdict": "ratify", "adjusted_severity": "null", "adjusted_status": "null",
        "reasoning": "Finding is well-supported as-is",
    })}
    with mock.patch("src.llm.gemini_client.call", return_value=stringified_null_response), \
         mock.patch("src.database.supabase_client.insert_review_action"):
        reviewed = JudgeAgent(gateway=LLMGateway()).review(gap, obligation, control)

    assert reviewed.risk_band == gap.risk_band, "stringified null must not change severity"
    assert reviewed.status == gap.status, "stringified null must not change status"


def test_judge_agent_tolerates_invented_severity_string_instead_of_crashing():
    """A model-invented severity string that isn't a real RiskSeverity member
    (e.g. a typo or a value outside CRITICAL/HIGH/MEDIUM/LOW) must fall back
    to leaving severity unchanged, same defensive pattern as audit_agent.py's
    gap_class validation — not crash the run."""
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id, control_id=control.id, coverage_level=CoverageLevel.NONE,
        rationale="r", confidence=0.8, created_by_agent="mapping_agent", model_id="gpt-5.1",
        prompt_version="v1", run_id=uuid.uuid4(), provenance=PROV,
    )
    audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "no_control", "narrative": "n",
        "risk_factors": {"regulatory_severity": 3, "enforcement_likelihood": 3, "business_exposure": 3, "control_weakness": 3, "remediation_urgency": 3},
    })}
    with mock.patch("src.llm.openai_client.call", return_value=audit_response):
        gap = AuditAgent(gateway=LLMGateway()).audit(obligation, control, mapping, run_id=uuid.uuid4())

    invented_severity_response = {"content": json.dumps({
        "verdict": "adjust", "adjusted_severity": "SEVERE", "adjusted_status": None,
        "reasoning": "Escalating severity",
    })}
    with mock.patch("src.llm.gemini_client.call", return_value=invented_severity_response), \
         mock.patch("src.database.supabase_client.insert_review_action"):
        reviewed = JudgeAgent(gateway=LLMGateway()).review(gap, obligation, control)

    assert reviewed.risk_band == gap.risk_band, "invented severity string must not change severity"


def test_judge_agent_review_action_write_failure_does_not_break_review():
    """review_actions is an audit log, not the primary artifact — a failure
    persisting it must be logged and swallowed, never propagate and take
    down graph execution over a finding the judge already correctly reviewed."""
    obligation = _make_obligation()
    control = _make_control()
    from src.core.schemas import ControlMapping

    mapping = ControlMapping(
        obligation_id=obligation.id, control_id=control.id, coverage_level=CoverageLevel.NONE,
        rationale="r", confidence=0.8, created_by_agent="mapping_agent", model_id="gpt-5.1",
        prompt_version="v1", run_id=uuid.uuid4(), provenance=PROV,
    )
    audit_response = {"content": json.dumps({
        "has_gap": True, "gap_class": "no_control", "narrative": "n",
        "risk_factors": {"regulatory_severity": 3, "enforcement_likelihood": 3, "business_exposure": 3, "control_weakness": 3, "remediation_urgency": 3},
    })}
    with mock.patch("src.llm.openai_client.call", return_value=audit_response):
        gap = AuditAgent(gateway=LLMGateway()).audit(obligation, control, mapping, run_id=uuid.uuid4())

    ratify_response = {"content": json.dumps({
        "verdict": "ratify", "adjusted_severity": None, "adjusted_status": None,
        "reasoning": "Finding is well-supported as-is",
    })}
    with mock.patch("src.llm.gemini_client.call", return_value=ratify_response), \
         mock.patch(
             "src.database.supabase_client.insert_review_action",
             side_effect=RuntimeError("simulated review_actions outage"),
         ):
        reviewed = JudgeAgent(gateway=LLMGateway()).review(gap, obligation, control)

    assert reviewed.risk_band == gap.risk_band, "the GapFinding verdict is unaffected by the audit-log failure"
