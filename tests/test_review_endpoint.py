"""Tests for the HITL review endpoint (src/api/main.py's /api/v1/review and
/review). Every src.database.supabase_client call is mocked -- this proves
the endpoint's request/response contract and state-transition logic (does
accept/reject/amend apply the right review_state/status, does it write the
right review_actions audit row?), not the DB layer itself, which has its own
coverage elsewhere.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402

client = TestClient(app)


def _fake_obligation_row(obligation_id: str, run_id: str) -> dict:
    return {
        "id": obligation_id,
        "clause_id": str(uuid.uuid4()),
        "obligation_text": "Report major ICT incidents within 24 hours",
        "verbatim_quote": "shall report major ICT-related incidents within 24 hours",
        "modality": "must",
        "actor": None,
        "trigger_condition": None,
        "deadline_spec": "24 hours",
        "obligation_type": "ict",
        "confidence": 0.9,
        "span_verified": True,
        "span_verify_method": "exact",
        "review_state": "proposed",
        "created_by_agent": "ingestion_agent",
        "model_id": "gpt-5.1",
        "prompt_version": "v1",
        "run_id": run_id,
        "scenario_id": None,
        "provenance": {"source_file": "api_upload", "page_number": 1, "snippet_hash": "h"},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _fake_gap_row(run_id: str, obligation_id: str, review_state: str = "needs_review") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "obligation_id": obligation_id,
        "entity_id": None,
        "gap_class": "partial_coverage",
        "narrative": "Control INC-07 escalates within 48 hours but the obligation requires 24.",
        "risk_factors": {
            "regulatory_severity": 4,
            "enforcement_likelihood": 3,
            "business_exposure": 3,
            "control_weakness": 3,
            "remediation_urgency": 4,
        },
        "risk_score": 68,
        "risk_band": "HIGH",
        "status": "open",
        "confidence": 0.8,
        "review_state": review_state,
        "created_by_agent": "audit_agent",
        "model_id": "gpt-5.6-luna",
        "prompt_version": "v1",
        "run_id": run_id,
        "scenario_id": None,
        "provenance": {"source_file": "api_upload", "page_number": 1, "snippet_hash": "h"},
    }


# ============ GET /api/v1/review ============


def test_review_queue_returns_only_needs_review_gaps_enriched_with_obligation_text():
    run_id = str(uuid.uuid4())
    obligation_id = str(uuid.uuid4())
    gap = _fake_gap_row(run_id, obligation_id)

    with (
        mock.patch("src.database.supabase_client.list_gaps", return_value=[gap]) as mock_list_gaps,
        mock.patch(
            "src.database.supabase_client.get_obligation",
            return_value=_fake_obligation_row(obligation_id, run_id),
        ),
    ):
        response = client.get("/api/v1/review")

    assert response.status_code == 200
    # Confirms the queue is actually filtered server-side, not just labelled as such.
    assert mock_list_gaps.call_args.kwargs == {"review_state": "needs_review"}

    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == gap["id"]
    assert body[0]["obligation_text"] == "Report major ICT incidents within 24 hours"
    assert body[0]["risk_band"] == "HIGH"
    assert body[0]["review_state"] == "needs_review"


def test_review_queue_bare_alias_matches_api_v1_path():
    with mock.patch("src.database.supabase_client.list_gaps", return_value=[]):
        response = client.get("/review")
    assert response.status_code == 200
    assert response.json() == []


def test_review_queue_handles_obligation_not_found():
    run_id = str(uuid.uuid4())
    obligation_id = str(uuid.uuid4())
    gap = _fake_gap_row(run_id, obligation_id)

    with (
        mock.patch("src.database.supabase_client.list_gaps", return_value=[gap]),
        mock.patch("src.database.supabase_client.get_obligation", return_value=None),
    ):
        response = client.get("/api/v1/review")

    assert response.status_code == 200
    assert response.json()[0]["obligation_text"] == "(obligation not found)"


# ============ POST /api/v1/review -- accept/reject/amend transitions ============


def test_review_accept_sets_accepted_and_leaves_status_untouched():
    run_id = str(uuid.uuid4())
    obligation_id = str(uuid.uuid4())
    gap_row = _fake_gap_row(run_id, obligation_id)

    with (
        mock.patch("src.database.supabase_client.get_gap", return_value=gap_row),
        mock.patch("src.database.supabase_client.update_gap", return_value={}) as mock_update_gap,
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}) as mock_insert_action,
    ):
        response = client.post(
            "/api/v1/review",
            json={"gap_id": gap_row["id"], "reviewer": "amit@bank.example", "action": "accept"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["review_state"] == "accepted"
    assert body["status"] == "open"  # untouched, matches gap_row's original status

    updated_gap = mock_update_gap.call_args.args[0]
    assert updated_gap.review_state.value == "accepted"
    assert updated_gap.status == "open"

    action_payload = mock_insert_action.call_args.args[0]
    assert action_payload["entity_table"] == "gaps"
    assert action_payload["entity_id"] == gap_row["id"]
    assert action_payload["reviewer"] == "amit@bank.example"
    assert action_payload["action"] == "accept"
    assert action_payload["original_output"]["review_state"] == "needs_review"
    assert action_payload["corrected_output"]["review_state"] == "accepted"


def test_review_reject_sets_rejected_and_status_disputed():
    run_id = str(uuid.uuid4())
    obligation_id = str(uuid.uuid4())
    gap_row = _fake_gap_row(run_id, obligation_id)

    with (
        mock.patch("src.database.supabase_client.get_gap", return_value=gap_row),
        mock.patch("src.database.supabase_client.update_gap", return_value={}) as mock_update_gap,
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}) as mock_insert_action,
    ):
        response = client.post(
            "/api/v1/review",
            json={"gap_id": gap_row["id"], "reviewer": "amit@bank.example", "action": "reject", "note": "false positive"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["review_state"] == "rejected"
    assert body["status"] == "disputed"  # same signal JudgeAgent's own reject path uses

    updated_gap = mock_update_gap.call_args.args[0]
    assert updated_gap.review_state.value == "rejected"
    assert updated_gap.status == "disputed"

    action_payload = mock_insert_action.call_args.args[0]
    assert action_payload["action"] == "reject"
    assert action_payload["note"] == "false positive"


def test_review_amend_applies_adjusted_fields_and_sets_accepted():
    run_id = str(uuid.uuid4())
    obligation_id = str(uuid.uuid4())
    gap_row = _fake_gap_row(run_id, obligation_id)

    with (
        mock.patch("src.database.supabase_client.get_gap", return_value=gap_row),
        mock.patch("src.database.supabase_client.update_gap", return_value={}) as mock_update_gap,
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}) as mock_insert_action,
    ):
        response = client.post(
            "/api/v1/review",
            json={
                "gap_id": gap_row["id"],
                "reviewer": "amit@bank.example",
                "action": "amend",
                "adjusted_fields": {"risk_band": "CRITICAL", "narrative": "Escalated after legal review."},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["review_state"] == "accepted"
    assert body["risk_band"] == "CRITICAL"
    assert body["narrative"] == "Escalated after legal review."

    updated_gap = mock_update_gap.call_args.args[0]
    assert updated_gap.risk_band.value == "CRITICAL"
    assert updated_gap.review_state.value == "accepted"

    action_payload = mock_insert_action.call_args.args[0]
    assert action_payload["action"] == "amend"
    assert action_payload["original_output"]["risk_band"] == "HIGH"
    assert action_payload["corrected_output"]["risk_band"] == "CRITICAL"


def test_review_amend_rejects_unknown_adjusted_field():
    gap_row = _fake_gap_row(str(uuid.uuid4()), str(uuid.uuid4()))
    with mock.patch("src.database.supabase_client.get_gap", return_value=gap_row):
        response = client.post(
            "/api/v1/review",
            json={
                "gap_id": gap_row["id"],
                "reviewer": "amit@bank.example",
                "action": "amend",
                "adjusted_fields": {"obligation_id": str(uuid.uuid4())},
            },
        )
    assert response.status_code == 422


def test_review_amend_requires_adjusted_fields():
    gap_row = _fake_gap_row(str(uuid.uuid4()), str(uuid.uuid4()))
    with mock.patch("src.database.supabase_client.get_gap", return_value=gap_row):
        response = client.post(
            "/api/v1/review",
            json={"gap_id": gap_row["id"], "reviewer": "amit@bank.example", "action": "amend"},
        )
    assert response.status_code == 422


def test_review_amend_rejects_invalid_risk_band_value():
    gap_row = _fake_gap_row(str(uuid.uuid4()), str(uuid.uuid4()))
    with mock.patch("src.database.supabase_client.get_gap", return_value=gap_row):
        response = client.post(
            "/api/v1/review",
            json={
                "gap_id": gap_row["id"],
                "reviewer": "amit@bank.example",
                "action": "amend",
                "adjusted_fields": {"risk_band": "SUPER_CRITICAL"},
            },
        )
    assert response.status_code == 422


def test_review_rejects_invalid_action():
    gap_row = _fake_gap_row(str(uuid.uuid4()), str(uuid.uuid4()))
    with mock.patch("src.database.supabase_client.get_gap", return_value=gap_row):
        response = client.post(
            "/api/v1/review",
            json={"gap_id": gap_row["id"], "reviewer": "amit@bank.example", "action": "approve"},
        )
    assert response.status_code == 422


def test_review_404s_on_missing_gap():
    with mock.patch("src.database.supabase_client.get_gap", return_value=None):
        response = client.post(
            "/api/v1/review",
            json={"gap_id": str(uuid.uuid4()), "reviewer": "amit@bank.example", "action": "accept"},
        )
    assert response.status_code == 404


def test_review_422s_on_malformed_gap_id():
    response = client.post(
        "/api/v1/review",
        json={"gap_id": "not-a-uuid", "reviewer": "amit@bank.example", "action": "accept"},
    )
    assert response.status_code == 422


def test_review_post_bare_alias_matches_api_v1_path():
    gap_row = _fake_gap_row(str(uuid.uuid4()), str(uuid.uuid4()))
    with (
        mock.patch("src.database.supabase_client.get_gap", return_value=gap_row),
        mock.patch("src.database.supabase_client.update_gap", return_value={}),
        mock.patch("src.database.supabase_client.insert_review_action", return_value={}),
    ):
        response = client.post(
            "/review",
            json={"gap_id": gap_row["id"], "reviewer": "amit@bank.example", "action": "accept"},
        )
    assert response.status_code == 200
    assert response.json()["review_state"] == "accepted"
