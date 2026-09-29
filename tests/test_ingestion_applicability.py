"""Tests for span verification, the ingestion agent, and the applicability
agent.

Ingestion/applicability tests mock LLMGateway.call rather than hit a real
model — these are testing agent logic (parsing, span verification wiring,
filtering), not model quality. Sample text comes from corpus/ (the same
dummy documents scripts/seed_chroma.py indexes) rather than inventing
separate fixture text.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agents.applicability_agent import ApplicabilityAgent  # noqa: E402
from src.agents.ingestion_agent import IngestionAgent  # noqa: E402
from src.core.schemas import Modality, ReviewState, RiskSeverity  # noqa: E402
from src.verify.span import verify_citation_span  # noqa: E402

DUMMY_REGULATION_TEXT = (REPO_ROOT / "corpus" / "regulations" / "dummy_regulation.txt").read_text(
    encoding="utf-8"
)
DUMMY_POLICY_TEXT = (REPO_ROOT / "corpus" / "bank" / "dummy_policy.txt").read_text(encoding="utf-8")


# ============ Span verification ============


def test_span_verification_passes_on_verbatim_quote():
    quote = "A regulated entity must report any material operational incident"
    assert quote in DUMMY_REGULATION_TEXT  # sanity check on the fixture itself
    result = verify_citation_span(DUMMY_REGULATION_TEXT, quote)
    assert result["verified"] is True
    assert result["method"] == "exact"
    assert result["match_ratio"] == 1.0


def test_span_verification_fails_on_hallucinated_quote():
    hallucinated = "must report any incident to the central bank within 6 hours"
    assert hallucinated not in DUMMY_REGULATION_TEXT
    result = verify_citation_span(DUMMY_REGULATION_TEXT, hallucinated)
    assert result["verified"] is False
    assert result["method"] == "none"


# ============ Ingestion agent ============


class _FakeGateway:
    """Stands in for LLMGateway — mocks the network boundary, not the
    parsing/verification logic this test actually exercises."""

    def __init__(self, content_by_role: dict[str, str]):
        self.config = {"extractor": {"model": "gpt-5.1"}}
        self._content_by_role = content_by_role
        self.calls: list[tuple[str, str]] = []

    def call(self, role: str, prompt: str, schema_version: str = "v1") -> dict:
        self.calls.append((role, prompt))
        return {"content": self._content_by_role[role]}


def test_ingestion_produces_valid_models_with_provenance():
    real_quote = "must report any material operational incident to the"
    assert real_quote in DUMMY_REGULATION_TEXT

    fake_response = json.dumps(
        {
            "obligations": [
                {
                    "obligation_text": "must report material incidents promptly",
                    "verbatim_quote": real_quote,
                    "modality": "must",
                    "actor": "regulated entity",
                    "trigger_condition": "material operational incident",
                    "deadline_spec": "24 hours",
                    "obligation_type": "reporting",
                    "confidence": 0.9,
                }
            ]
        }
    )
    gateway = _FakeGateway({"EXTRACTOR": fake_response})
    agent = IngestionAgent(gateway=gateway)

    run_id = uuid.uuid4()
    clause_id = uuid.uuid4()
    obligations = agent.extract_obligations(
        source_text=DUMMY_REGULATION_TEXT,
        clause_id=clause_id,
        source_file="dummy_regulation.txt",
        page_number=1,
        run_id=run_id,
    )

    assert len(obligations) == 1
    obligation = obligations[0]
    assert obligation.clause_id == clause_id
    assert obligation.modality == Modality.MUST
    assert obligation.span_verified is True
    assert obligation.review_state == ReviewState.PROPOSED
    assert obligation.provenance.source_file == "dummy_regulation.txt"
    assert obligation.provenance.page_number == 1
    assert obligation.created_by_agent == "ingestion_agent"
    assert obligation.model_id == "gpt-5.1"  # read from gateway config, not hardcoded


def test_ingestion_flags_hallucinated_citation_as_needs_review():
    fake_response = json.dumps(
        {
            "obligations": [
                {
                    "obligation_text": "must report within 6 hours to the central bank",
                    "verbatim_quote": "must report within 6 hours to the central bank",  # not in source
                    "modality": "must",
                    "obligation_type": "reporting",
                    "confidence": 0.8,
                }
            ]
        }
    )
    gateway = _FakeGateway({"EXTRACTOR": fake_response})
    agent = IngestionAgent(gateway=gateway)

    obligations = agent.extract_obligations(
        source_text=DUMMY_REGULATION_TEXT,
        clause_id=uuid.uuid4(),
        source_file="dummy_regulation.txt",
        page_number=1,
        run_id=uuid.uuid4(),
    )

    assert len(obligations) == 1, "a hallucinated citation must still be returned, not silently dropped"
    obligation = obligations[0]
    assert obligation.span_verified is False
    assert obligation.review_state == ReviewState.NEEDS_REVIEW


def test_ingestion_extracts_controls_from_policy_text():
    assert "operations team escalates any detected system incident" in DUMMY_POLICY_TEXT

    fake_response = json.dumps(
        {
            "controls": [
                {
                    "control_ref": "C-1",
                    "title": "Incident escalation procedure",
                    "design_description": "Escalates incidents to compliance",
                    "verbatim_quote": "operations team escalates any detected system incident",
                    "confidence": 0.85,
                }
            ]
        }
    )
    gateway = _FakeGateway({"EXTRACTOR": fake_response})
    agent = IngestionAgent(gateway=gateway)

    controls = agent.extract_controls(
        source_text=DUMMY_POLICY_TEXT,
        bank_id=uuid.uuid4(),
        source_file="dummy_policy.txt",
        page_number=1,
        run_id=uuid.uuid4(),
    )

    assert len(controls) == 1
    control = controls[0]
    assert control.control_ref == "C-1"
    assert control.span_verified is True
    assert control.provenance.source_file == "dummy_policy.txt"


# ============ Applicability agent ============


def test_applicability_filters_out_of_scope_obligation():
    """An investment-banking obligation tested against a retail-only profile
    (config/bank_profile.yaml's actual Meridian Bank USA profile, which
    deliberately has no investment_banking business line) must be filtered
    out; a retail-banking obligation must survive."""
    from src.core.schemas import RegulatoryObligation

    retail_obligation = RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text="must retain retail deposit account records for five years",
        verbatim_quote="retain retail deposit account records",
        modality=Modality.MUST,
        obligation_type="recordkeeping",
        confidence=0.9,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance={"source_file": "test.txt", "page_number": 1, "snippet_hash": "h1"},
    )
    investment_banking_obligation = RegulatoryObligation(
        clause_id=uuid.uuid4(),
        obligation_text="must report proprietary trading positions to the securities regulator",
        verbatim_quote="report proprietary trading positions",
        modality=Modality.MUST,
        obligation_type="reporting",
        confidence=0.9,
        created_by_agent="test",
        model_id="test-model",
        prompt_version="v1",
        run_id=uuid.uuid4(),
        provenance={"source_file": "test.txt", "page_number": 1, "snippet_hash": "h2"},
    )

    def fake_call(role, prompt, schema_version="v1"):
        if "proprietary trading" in prompt:
            return {
                "content": json.dumps(
                    {
                        "applies": False,
                        "driver": "product",
                        "rationale": "Entity has no investment_banking or trading business line",
                        "cited_profile_attribute": "business_lines: retail_banking, consumer_lending",
                        "confidence": 0.92,
                    }
                )
            }
        return {
            "content": json.dumps(
                {
                    "applies": True,
                    "driver": "product",
                    "rationale": "Entity operates retail deposit accounts",
                    "cited_profile_attribute": "business_lines: retail_banking",
                    "confidence": 0.95,
                }
            )
        }

    gateway = _FakeGateway({})
    gateway.call = fake_call  # override with prompt-inspecting logic instead of a fixed response

    agent = ApplicabilityAgent(gateway=gateway)
    assert "investment_banking" not in agent.bank_profile.get("business_lines", [])

    applicable, records = agent.evaluate(
        obligations=[retail_obligation, investment_banking_obligation],
        entity_id=uuid.uuid4(),
    )

    assert len(records) == 2, "every obligation gets a recorded verdict, applicable or not"
    assert len(applicable) == 1
    assert applicable[0].obligation_text.startswith("must retain retail deposit")

    not_applicable_records = [r for r in records if not r.applies]
    assert len(not_applicable_records) == 1
    assert not_applicable_records[0].obligation_id == investment_banking_obligation.id


def test_bank_profile_from_entity_row_maps_db_row_to_profile_shape():
    """Reproduced live: DORA was dropped for a newly-seeded EU bank because
    ApplicabilityAgent was still reasoning over a different, hardcoded bank's
    profile regardless of which bank_entities row was actually selected —
    this is the fix (state_graph.py's applicability_node now builds the
    profile from the real row instead of always defaulting to
    config/bank_profile.yaml)."""
    from src.agents.applicability_agent import bank_profile_from_entity_row

    row = {
        "id": str(uuid.uuid4()),
        "name": "Meridian Bank Europe SE",
        "jurisdiction": "EU",
        "licences": ["ecb_credit_institution", "dora_financial_entity"],
        "product_lines": [],
    }
    profile = bank_profile_from_entity_row(row)

    assert profile["entity_name"] == "Meridian Bank Europe SE"
    assert profile["jurisdiction"] == "EU"
    assert profile["licences"] == ["ecb_credit_institution", "dora_financial_entity"]
    assert profile["business_lines"] == []
    assert profile["entity_tier"] == "unknown"  # no DB column for this -- not fabricated
