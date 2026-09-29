"""Tests for the FastAPI application layer (src/api/main.py).

Both the LangGraph invocation and every src.database.supabase_client call
are mocked — this test is about the API's request/response contract and
its wiring (does it call the graph with a state the graph can accept? does
it flatten Supabase rows into the right hierarchy?), not about re-proving
the graph or the DB layer, which already have their own tests
(test_orchestrator.py, test_retrieval.py).
"""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402

client = TestClient(app)

FAKE_FINAL_STATE = {
    "obligations": [object()],
    "controls": [object()],
    "applicable_obligations": [object()],
    "applicability_records": [object()],
    "mappings": [object()],
    "gaps": [object()],
    "reviewed_gaps": [object(), object()],
    "remediations": [object()],
}


class _FakeCompiledGraph:
    def invoke(self, initial_state, config=None):
        # Sanity-check the endpoint actually built a state the graph could
        # use, not just that it calls .invoke() at all.
        assert initial_state["raw_regulatory_text"]
        assert initial_state["raw_policy_text"]
        assert initial_state["obligations"] == []
        return FAKE_FINAL_STATE


@contextmanager
def _fake_compiled_graph(checkpointer_path=None):
    yield _FakeCompiledGraph()


def _fake_bank_entity(entity_id: uuid.UUID) -> dict:
    return {"id": str(entity_id), "bank_id": str(uuid.uuid4()), "name": "Meridian NV"}


def test_audit_endpoint_returns_200_with_final_state_summary():
    entity_id = str(uuid.uuid4())

    with (
        mock.patch("src.api.main.compiled_graph", _fake_compiled_graph),
        mock.patch("src.database.supabase_client.get_bank_entity", side_effect=lambda eid: _fake_bank_entity(eid)),
        mock.patch("src.database.supabase_client.insert_run", return_value={}),
        mock.patch("src.database.supabase_client.update_run_status", return_value={}),
        mock.patch("src.database.supabase_client.insert_regulatory_document", return_value={}),
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
    ):
        response = client.post(
            "/api/v1/audit",
            json={
                "regulation_text": "The bank must retain transaction logs for five years.",
                "policy_text": "Control C-1 retains logs for three years.",
                "bank_profile_id": entity_id,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["run_id"])  # a real UUID was generated
    assert body["status"] == "completed"
    assert body["obligations_count"] == 1
    assert body["controls_count"] == 1
    assert body["applicable_obligations_count"] == 1
    assert body["mappings_count"] == 1
    assert body["gaps_count"] == 2
    assert body["remediations_count"] == 1


def test_audit_endpoint_404s_on_unknown_bank_profile_id():
    with mock.patch("src.database.supabase_client.get_bank_entity", return_value=None):
        response = client.post(
            "/api/v1/audit",
            json={
                "regulation_text": "text",
                "policy_text": "text",
                "bank_profile_id": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 404


def test_audit_endpoint_422s_on_malformed_bank_profile_id():
    response = client.post(
        "/api/v1/audit",
        json={"regulation_text": "text", "policy_text": "text", "bank_profile_id": "not-a-uuid"},
    )
    assert response.status_code == 422


def test_audit_endpoint_marks_run_failed_on_pipeline_exception():
    entity_id = str(uuid.uuid4())

    @contextmanager
    def _raising_compiled_graph(checkpointer_path=None):
        raise RuntimeError("simulated pipeline failure")
        yield  # pragma: no cover - unreachable, satisfies generator shape

    with (
        mock.patch("src.api.main.compiled_graph", _raising_compiled_graph),
        mock.patch("src.database.supabase_client.get_bank_entity", side_effect=lambda eid: _fake_bank_entity(eid)),
        mock.patch("src.database.supabase_client.insert_run", return_value={}) as mock_insert_run,
        mock.patch("src.database.supabase_client.update_run_status", return_value={}) as mock_update_status,
        mock.patch("src.database.supabase_client.insert_regulatory_document", return_value={}),
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
    ):
        response = client.post(
            "/api/v1/audit",
            json={"regulation_text": "text", "policy_text": "text", "bank_profile_id": entity_id},
        )

    assert response.status_code == 500
    assert mock_insert_run.called
    # update_run_status must be called with "failed", not left "running" forever
    assert mock_update_status.call_args.args[1] == "failed"
    body = response.json()
    assert "unexpected error" in body["detail"]  # generic fallback (no .code attribute)
    assert "simulated pipeline failure" not in body["detail"]  # raw exception text stays out of the browser now
    assert "{" not in body["detail"]  # never a raw dict/JSON repr on screen


def test_audit_endpoint_logs_full_exception_and_gives_friendly_detail_for_postgres_errors():
    """A postgrest.exceptions.APIError-shaped exception (has .code, .message)
    must get its known SQLSTATE translated into a short plain-English
    sentence instead of reaching the browser as a raw {'message': ...,
    'code': ...} dict — and the full exception must be logged server-side
    via logger.exception() so pipeline failures actually show up in the
    terminal."""
    entity_id = str(uuid.uuid4())

    class _FakeDuplicateKeyError(Exception):
        code = "23505"
        message = 'duplicate key value violates unique constraint "controls_bank_id_control_ref_key"'

        def __init__(self) -> None:
            # Mirrors real postgrest.exceptions.APIError.__init__, which
            # calls Exception.__init__(self, str(self)) via its own
            # __repr__ -- a bare `Exception()` call here would leave
            # str(exc) empty and not actually exercise what this test
            # claims to check (raw detail stays server-side only).
            super().__init__(self.message)

    @contextmanager
    def _raising_compiled_graph(checkpointer_path=None):
        raise _FakeDuplicateKeyError()
        yield  # pragma: no cover - unreachable, satisfies generator shape

    with (
        mock.patch("src.api.main.compiled_graph", _raising_compiled_graph),
        mock.patch("src.database.supabase_client.get_bank_entity", side_effect=lambda eid: _fake_bank_entity(eid)),
        mock.patch("src.database.supabase_client.insert_run", return_value={}),
        mock.patch("src.database.supabase_client.update_run_status", return_value={}),
        mock.patch("src.database.supabase_client.insert_regulatory_document", return_value={}),
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
        mock.patch("src.api.main.logger") as mock_logger,
    ):
        response = client.post(
            "/api/v1/audit",
            json={"regulation_text": "text", "policy_text": "text", "bank_profile_id": entity_id},
        )

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Database write failed: A record with this identifier already exists (duplicate key)."
    assert "duplicate key value violates unique constraint" not in body["detail"]  # raw SQL text stays server-side
    assert mock_logger.exception.called  # server-side terminal logging actually happens now


def test_audit_endpoint_gives_friendly_detail_for_gemini_high_demand_error():
    """Reproduced live: a real google.genai.errors.ServerError (503, "high
    demand") reached the browser as a bare {'error': {'code': 503, ...}}
    dict. Must now render as a short, JSON-free sentence naming which
    component failed (Gemini) and why, without needing logger.exception's
    server-side traceback to understand it."""
    from google.genai.errors import ServerError

    entity_id = str(uuid.uuid4())
    gemini_error = ServerError(
        503,
        {"error": {"code": 503, "message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}},
    )

    @contextmanager
    def _raising_compiled_graph(checkpointer_path=None):
        raise gemini_error
        yield  # pragma: no cover - unreachable, satisfies generator shape

    with (
        mock.patch("src.api.main.compiled_graph", _raising_compiled_graph),
        mock.patch("src.database.supabase_client.get_bank_entity", side_effect=lambda eid: _fake_bank_entity(eid)),
        mock.patch("src.database.supabase_client.insert_run", return_value={}),
        mock.patch("src.database.supabase_client.update_run_status", return_value={}),
        mock.patch("src.database.supabase_client.insert_regulatory_document", return_value={}),
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
    ):
        response = client.post(
            "/api/v1/audit",
            json={"regulation_text": "text", "policy_text": "text", "bank_profile_id": entity_id},
        )

    assert response.status_code == 500
    body = response.json()
    assert "Gemini" in body["detail"]
    assert "overloaded" in body["detail"] or "high demand" in body["detail"]
    assert "{" not in body["detail"]  # never a raw dict/JSON repr on screen


def test_lineage_endpoint_returns_structured_hierarchy():
    run_id = str(uuid.uuid4())
    obligation = {"id": "obl-1", "obligation_text": "Retain logs for five years", "run_id": run_id}
    control = {"id": "ctrl-1", "title": "Log retention control", "run_id": run_id}
    mapping = {
        "id": "map-1",
        "obligation_id": "obl-1",
        "control_id": "ctrl-1",
        "coverage_level": "partial",
        "run_id": run_id,
    }
    gap = {"id": "gap-1", "obligation_id": "obl-1", "gap_class": "partial_coverage", "run_id": run_id}
    remediation = {"id": "rem-1", "gap_id": "gap-1", "action": "Extend retention to five years", "run_id": run_id}

    with (
        mock.patch("src.database.supabase_client.list_obligations_by_run", return_value=[obligation]),
        mock.patch("src.database.supabase_client.list_controls_by_run", return_value=[control]),
        mock.patch("src.database.supabase_client.list_control_mappings_by_run", return_value=[mapping]),
        mock.patch("src.database.supabase_client.list_gaps_by_run", return_value=[gap]),
        mock.patch("src.database.supabase_client.list_remediations_by_run", return_value=[remediation]),
    ):
        response = client.get(f"/api/v1/lineage/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert len(body["obligations"]) == 1

    obligation_out = body["obligations"][0]
    assert obligation_out["id"] == "obl-1"

    assert len(obligation_out["mappings"]) == 1
    mapping_out = obligation_out["mappings"][0]
    assert mapping_out["id"] == "map-1"
    assert mapping_out["control"]["id"] == "ctrl-1"

    assert len(obligation_out["gaps"]) == 1
    gap_out = obligation_out["gaps"][0]
    assert gap_out["id"] == "gap-1"
    assert len(gap_out["remediations"]) == 1
    assert gap_out["remediations"][0]["id"] == "rem-1"


def test_lineage_endpoint_404s_when_run_has_no_data():
    run_id = str(uuid.uuid4())
    with mock.patch("src.database.supabase_client.list_obligations_by_run", return_value=[]):
        response = client.get(f"/api/v1/lineage/{run_id}")
    assert response.status_code == 404


def test_lineage_endpoint_422s_on_malformed_run_id():
    response = client.get("/api/v1/lineage/not-a-uuid")
    assert response.status_code == 422


def test_list_bank_entities_returns_id_and_name():
    entity_id = str(uuid.uuid4())
    row = {
        "id": entity_id,
        "bank_id": str(uuid.uuid4()),
        "name": "Meridian Bank USA",
        "jurisdiction": "US",
        "licences": ["national_bank_charter"],
        "product_lines": ["retail_banking", "consumer_lending"],
        "parent_entity_id": None,
    }
    with mock.patch("src.database.supabase_client.list_bank_entities", return_value=[row]):
        response = client.get("/api/v1/bank_entities")

    assert response.status_code == 200
    body = response.json()
    assert body == [{"id": entity_id, "name": "Meridian Bank USA"}]


def test_cors_middleware_allows_vite_dev_server_origin():
    response = client.options(
        "/api/v1/audit",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


# ============ /api/v1/changes/diff — ChangeWatcherAgent itself is mocked
# here too, same reasoning as the graph mock above: this proves the
# endpoint's wiring (does it build valid RegulatoryObligation/
# ControlMapping objects from raw Supabase rows? does it create the new
# version/clause chain? does it persist what the agent returns? does it
# fail the run cleanly?), not the agent's diff logic, which has its own
# tests. ============

from src.core.schemas import ChangeType, ClauseChange, Materiality  # noqa: E402


def _fake_obligation_row(run_id: str, clause_id: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "clause_id": clause_id,
        "obligation_text": "Report major ICT incidents within 24 hours",
        "verbatim_quote": "shall report major ICT-related incidents",
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


def _fake_clause_change() -> ClauseChange:
    return ClauseChange(
        from_version_id=uuid.uuid4(),
        to_version_id=uuid.uuid4(),
        change_type=ChangeType.AMENDED,
        materiality=Materiality.HIGH,
        text_diff={"old_span": "within 24 hours", "new_span": "within 12 hours", "breaks_control": True},
        rationale="deadline tightened",
    )


class _FakeChangeWatcherAgent:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def diff(self, **kwargs):
        return [_fake_clause_change()], [], []


def test_change_diff_endpoint_returns_200_with_classified_changes():
    old_run_id = str(uuid.uuid4())
    clause_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())

    with (
        mock.patch(
            "src.database.supabase_client.list_obligations_by_run",
            return_value=[_fake_obligation_row(old_run_id, clause_id)],
        ),
        mock.patch(
            "src.database.supabase_client.get_clause",
            return_value={"id": clause_id, "document_version_id": version_id, "text_content": "old text"},
        ),
        mock.patch(
            "src.database.supabase_client.get_document_version",
            return_value={"id": version_id, "document_id": document_id},
        ),
        mock.patch("src.database.supabase_client.list_control_mappings_by_run", return_value=[]),
        mock.patch("src.database.supabase_client.list_controls_by_run", return_value=[]),
        mock.patch("src.database.supabase_client.insert_run", return_value={}),
        mock.patch("src.database.supabase_client.update_run_status", return_value={}) as mock_update_status,
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause_change", return_value={}) as mock_insert_change,
        mock.patch("src.database.supabase_client.insert_obligation", return_value={}),
        mock.patch("src.database.supabase_client.insert_gap", return_value={}) as mock_insert_gap,
        mock.patch("src.api.main.ChangeWatcherAgent", _FakeChangeWatcherAgent),
    ):
        response = client.post(
            "/api/v1/changes/diff",
            json={"run_id": old_run_id, "new_regulation_text": "new text"},
        )

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["diff_run_id"])
    assert len(body["changes"]) == 1
    assert body["changes"][0]["change_type"] == "amended"
    assert body["changes"][0]["materiality"] == "high"
    assert body["new_obligations_count"] == 0
    assert body["gaps_count"] == 0
    assert body["gaps"] == []
    assert mock_insert_change.called
    assert not mock_insert_gap.called
    assert mock_update_status.call_args.args[1] == "completed"


def test_change_diff_endpoint_404s_when_run_has_no_obligations():
    with mock.patch("src.database.supabase_client.list_obligations_by_run", return_value=[]):
        response = client.post(
            "/api/v1/changes/diff",
            json={"run_id": str(uuid.uuid4()), "new_regulation_text": "new text"},
        )
    assert response.status_code == 404


def test_change_diff_endpoint_422s_on_malformed_run_id():
    response = client.post(
        "/api/v1/changes/diff",
        json={"run_id": "not-a-uuid", "new_regulation_text": "new text"},
    )
    assert response.status_code == 422


def test_change_diff_endpoint_marks_diff_run_failed_on_exception():
    old_run_id = str(uuid.uuid4())
    clause_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    document_id = str(uuid.uuid4())

    class _RaisingChangeWatcherAgent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def diff(self, **kwargs):
            raise RuntimeError("simulated diff failure")

    with (
        mock.patch(
            "src.database.supabase_client.list_obligations_by_run",
            return_value=[_fake_obligation_row(old_run_id, clause_id)],
        ),
        mock.patch(
            "src.database.supabase_client.get_clause",
            return_value={"id": clause_id, "document_version_id": version_id, "text_content": "old text"},
        ),
        mock.patch(
            "src.database.supabase_client.get_document_version",
            return_value={"id": version_id, "document_id": document_id},
        ),
        mock.patch("src.database.supabase_client.list_control_mappings_by_run", return_value=[]),
        mock.patch("src.database.supabase_client.list_controls_by_run", return_value=[]),
        mock.patch("src.database.supabase_client.insert_run", return_value={}) as mock_insert_run,
        mock.patch("src.database.supabase_client.update_run_status", return_value={}) as mock_update_status,
        mock.patch("src.database.supabase_client.insert_document_version", return_value={}),
        mock.patch("src.database.supabase_client.insert_clause", return_value={}),
        mock.patch("src.api.main.ChangeWatcherAgent", _RaisingChangeWatcherAgent),
    ):
        response = client.post(
            "/api/v1/changes/diff",
            json={"run_id": old_run_id, "new_regulation_text": "new text"},
        )

    assert response.status_code == 500
    assert mock_insert_run.called
    assert mock_update_status.call_args.args[1] == "failed"


# ============ /api/v1/obligations/relations ============


def test_obligation_relations_endpoint_returns_200_with_detected_relation():
    run_id_a = str(uuid.uuid4())
    run_id_b = str(uuid.uuid4())
    clause_id_a = str(uuid.uuid4())
    clause_id_b = str(uuid.uuid4())
    row_a = _fake_obligation_row(run_id_a, clause_id_a)
    row_b = _fake_obligation_row(run_id_b, clause_id_b)
    row_b["id"] = str(uuid.uuid4())
    row_b["obligation_text"] = "Submit an early warning within 24 hours of a significant incident"

    def _fake_list_obligations_by_run(run_id):
        return [row_a] if str(run_id) == run_id_a else [row_b]

    overlaps_response = {"content": json.dumps({
        "relation_type": "overlaps", "dimension": "incident reporting deadline",
        "rationale": "Both require reporting within 24 hours to a competent authority.",
        "severity": None, "resolution_hint": "Design one control for both.", "confidence": 0.8,
    })}

    with (
        mock.patch("src.database.supabase_client.list_obligations_by_run", side_effect=_fake_list_obligations_by_run),
        mock.patch("src.database.supabase_client.insert_obligation_relation", return_value={}) as mock_insert,
        mock.patch("src.llm.openai_client.call", return_value=overlaps_response),
    ):
        response = client.post(
            "/api/v1/obligations/relations",
            json={"groups": [
                {"run_id": run_id_a, "regulator": "DORA"},
                {"run_id": run_id_b, "regulator": "NIS2"},
            ]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["obligations_compared_count"] == 2
    assert body["relations_count"] == 1
    assert body["relations"][0]["relation_type"] == "overlaps"
    assert body["relations"][0]["obligation_a_text"] == row_a["obligation_text"]
    assert body["relations"][0]["obligation_b_text"] == row_b["obligation_text"]
    assert mock_insert.called


def test_obligation_relations_endpoint_422s_with_fewer_than_2_groups():
    response = client.post(
        "/api/v1/obligations/relations",
        json={"groups": [{"run_id": str(uuid.uuid4()), "regulator": "DORA"}]},
    )
    assert response.status_code == 422


def test_obligation_relations_endpoint_422s_on_malformed_run_id():
    response = client.post(
        "/api/v1/obligations/relations",
        json={"groups": [
            {"run_id": "not-a-uuid", "regulator": "DORA"},
            {"run_id": str(uuid.uuid4()), "regulator": "NIS2"},
        ]},
    )
    assert response.status_code == 422


def test_obligation_relations_endpoint_404s_when_a_run_has_no_obligations():
    with mock.patch("src.database.supabase_client.list_obligations_by_run", return_value=[]):
        response = client.post(
            "/api/v1/obligations/relations",
            json={"groups": [
                {"run_id": str(uuid.uuid4()), "regulator": "DORA"},
                {"run_id": str(uuid.uuid4()), "regulator": "NIS2"},
            ]},
        )
    assert response.status_code == 404
