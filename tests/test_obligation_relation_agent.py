"""Verification tests for src/agents/obligation_relation_agent.py (roadmap
features 13/14, reopened 2026-09-29). Mocks only at the network-client
boundary (src.llm.openai_client.call), same convention as
tests/test_reasoning_agents.py.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agents.obligation_relation_agent import ObligationRelationAgent  # noqa: E402
from src.core.schemas import Modality, RegulatoryObligation, RelationType  # noqa: E402
from src.llm.gateway import LLMGateway  # noqa: E402

PROV = {"source_file": "test.txt", "page_number": 1, "snippet_hash": "h"}


def _make_obligation(text: str) -> RegulatoryObligation:
    return RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text=text,
        verbatim_quote=text,
        modality=Modality.MUST,
        obligation_type="reporting",
        confidence=0.9,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance=PROV,
    )


def test_compare_returns_none_for_unrelated_verdict():
    obligation_a = _make_obligation("Maintain a minimum CET1 capital ratio of 4.5%")
    obligation_b = _make_obligation("Report major ICT incidents within 24 hours")
    unrelated_response = {"content": json.dumps({
        "relation_type": "unrelated",
        "dimension": None, "rationale": "Different subject matter entirely.",
        "severity": None, "resolution_hint": None, "confidence": 0.95,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=unrelated_response) as mock_openai:
        agent = ObligationRelationAgent(gateway=LLMGateway())
        relation = agent.compare(obligation_a, obligation_b, "Basel III", "DORA")

    assert mock_openai.called, "still resolves to Foundry GPT-5.1 via the EXTRACTOR role"
    assert relation is None


def test_compare_returns_overlaps_relation():
    obligation_a = _make_obligation("Report major ICT-related incidents to the competent authority within 24 hours")
    obligation_b = _make_obligation("Submit an early warning within 24 hours of a significant incident")
    overlaps_response = {"content": json.dumps({
        "relation_type": "overlaps",
        "dimension": "incident reporting deadline",
        "rationale": "Both require reporting a major incident within the same 24-hour window to a competent authority.",
        "severity": None,
        "resolution_hint": "Design one escalation control that satisfies both reporting deadlines.",
        "confidence": 0.82,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=overlaps_response):
        agent = ObligationRelationAgent(gateway=LLMGateway())
        relation = agent.compare(obligation_a, obligation_b, "DORA", "NIS2")

    assert relation is not None
    assert relation.relation_type == RelationType.OVERLAPS
    assert relation.obligation_a == obligation_a.id
    assert relation.obligation_b == obligation_b.id
    assert relation.dimension == "incident reporting deadline"
    assert relation.severity is None


def test_compare_returns_conflicts_with_relation():
    obligation_a = _make_obligation("Retain transaction records for a minimum of ten years")
    obligation_b = _make_obligation("Delete personal data no later than five years after collection")
    conflict_response = {"content": json.dumps({
        "relation_type": "conflicts_with",
        "dimension": "data retention period",
        "rationale": "A 10-year retention requirement directly conflicts with a 5-year deletion mandate for the same data.",
        "severity": "high",
        "resolution_hint": "Escalate to legal/compliance to determine which retention period takes precedence.",
        "confidence": 0.88,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=conflict_response):
        agent = ObligationRelationAgent(gateway=LLMGateway())
        relation = agent.compare(obligation_a, obligation_b, "Basel III", "GDPR")

    assert relation is not None
    assert relation.relation_type == RelationType.CONFLICTS_WITH
    assert relation.severity == "high"


def test_compare_treats_invented_relation_type_as_unrelated_instead_of_crashing():
    obligation_a = _make_obligation("Obligation A text")
    obligation_b = _make_obligation("Obligation B text")
    invented_response = {"content": json.dumps({
        "relation_type": "somewhat_related",  # not a real RelationType value
        "dimension": "vague", "rationale": "r", "severity": None,
        "resolution_hint": None, "confidence": 0.5,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=invented_response):
        agent = ObligationRelationAgent(gateway=LLMGateway())
        relation = agent.compare(obligation_a, obligation_b)

    assert relation is None


def test_compare_across_groups_only_compares_cross_group_pairs_not_within_group():
    group_a = [_make_obligation("A1"), _make_obligation("A2")]
    group_b = [_make_obligation("B1")]

    overlaps_response = {"content": json.dumps({
        "relation_type": "overlaps", "dimension": "d", "rationale": "r",
        "severity": None, "resolution_hint": None, "confidence": 0.7,
    })}

    with mock.patch("src.llm.openai_client.call", return_value=overlaps_response) as mock_openai:
        agent = ObligationRelationAgent(gateway=LLMGateway())
        relations = agent.compare_across_groups([group_a, group_b], ["RegA", "RegB"])

    # 2 obligations in group_a x 1 in group_b = 2 cross-group comparisons; never A1-vs-A2.
    assert mock_openai.call_count == 2
    assert len(relations) == 2
    compared_pairs = {(r.obligation_a, r.obligation_b) for r in relations}
    assert (group_a[0].id, group_a[1].id) not in compared_pairs
    assert (group_a[1].id, group_a[0].id) not in compared_pairs
