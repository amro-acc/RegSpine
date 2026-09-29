"""Tests for the vector store (Chroma + reranker) and the Supabase CRUD
layer.

Chroma tests use a disposable, uniquely-named collection (not the real
`regulatory_obligations`/`internal_controls` collections seeded by
scripts/seed_chroma.py) so running the test suite never mutates the real
local index, and clean up after themselves.

The Supabase test is skipped (not failed) when SUPABASE_URL/
SUPABASE_SERVICE_KEY aren't set, rather than silently mocking the client,
since a mocked Supabase client wouldn't actually verify anything about the
live schema (the insert functions rely on real column names matching).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src.database import vector_store  # noqa: E402

SUPABASE_CREDS_PRESENT = bool(os.environ.get("SUPABASE_URL")) and bool(
    os.environ.get("SUPABASE_SERVICE_KEY")
)


@pytest.fixture
def test_collection_name():
    name = f"test_collection_{uuid.uuid4().hex[:8]}"
    yield name
    vector_store.get_chroma_client().delete_collection(name)


def test_chroma_upsert_and_vector_search(test_collection_name):
    docs = [
        {
            "id": "doc-1",
            "text": "A regulated entity must report material incidents within 24 hours.",
            "metadata": {"kind": "obligation"},
        },
        {
            "id": "doc-2",
            "text": "The bank retains transaction records for seven years in the archive.",
            "metadata": {"kind": "control"},
        },
    ]
    count = vector_store.upsert_documents(test_collection_name, docs)
    assert count == 2

    collection = vector_store.get_collection(test_collection_name)
    assert collection.count() == 2

    results = collection.query(
        query_embeddings=vector_store.get_embedding_model().encode(["incident reporting deadline"]).tolist(),
        n_results=2,
    )
    # The incident-reporting document should rank ahead of the retention one
    # for an incident-reporting query — a basic sanity check that embeddings
    # are actually semantically meaningful, not just present.
    assert results["ids"][0][0] == "doc-1"


def test_query_with_rerank_reorders_by_cross_encoder_score(test_collection_name):
    docs = [
        {"id": "a", "text": "Transaction records must be kept for five years.", "metadata": {"topic": "retention"}},
        {"id": "b", "text": "Material incidents must be reported within 24 hours of detection."},  # no metadata key at all — exercises the empty-metadata fallback
        {"id": "c", "text": "The cafeteria menu changes weekly on Mondays.", "metadata": {"topic": "unrelated"}},
    ]
    vector_store.upsert_documents(test_collection_name, docs)

    results = vector_store.query_with_rerank(
        test_collection_name, "incident reporting timeline", top_k=3, rerank_top_n=2
    )

    assert len(results) == 2
    assert results[0]["id"] == "b", "the incident-reporting document should rerank first"
    assert "rerank_score" in results[0]
    assert results[0]["rerank_score"] >= results[1]["rerank_score"], "results must be sorted by rerank score"
    # The irrelevant cafeteria document must not survive into rerank_top_n=2
    assert "c" not in {r["id"] for r in results}


@pytest.mark.skipif(
    not SUPABASE_CREDS_PRESENT,
    reason="SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping live Supabase CRUD cycle",
)
def test_supabase_dummy_crud_cycle():
    from src.core.schemas import ControlType, GapFinding, InternalControl, Modality, RegulatoryObligation, RiskSeverity
    from src.database import supabase_client as db

    client = db.get_client()
    prov = {"source_file": "test.txt", "page_number": 1, "snippet_hash": "test-hash"}
    run_id = uuid.uuid4()
    created = {"runs": [], "regulatory_documents": [], "document_versions": [], "clauses": [], "obligations": [], "controls": [], "gaps": []}

    try:
        client.table("runs").insert({"id": str(run_id), "pipeline": "pytest", "status": "running"}).execute()
        created["runs"].append(str(run_id))

        doc = client.table("regulatory_documents").insert(
            {"regulator": "TEST", "jurisdiction": "TEST", "title": "pytest doc", "doc_class": "regulation"}
        ).execute().data[0]
        created["regulatory_documents"].append(doc["id"])

        ver = client.table("document_versions").insert(
            {"document_id": doc["id"], "version_label": "v1", "source_sha256": "pytest-hash"}
        ).execute().data[0]
        created["document_versions"].append(ver["id"])

        clause = client.table("clauses").insert(
            {"document_version_id": ver["id"], "clause_ref": "Art. 1", "text_content": "pytest clause", "page_no": 1}
        ).execute().data[0]
        created["clauses"].append(clause["id"])

        obligation = RegulatoryObligation(
            clause_id=clause["id"], obligation_text="pytest obligation", verbatim_quote="pytest clause",
            modality=Modality.MUST, obligation_type="governance", confidence=0.9,
            created_by_agent="pytest", model_id="pytest-model", prompt_version="v1", run_id=run_id, provenance=prov,
        )
        obl_row = db.insert_obligation(obligation)
        created["obligations"].append(obl_row["id"])
        assert db.get_obligation(uuid.UUID(obl_row["id"]))["obligation_text"] == "pytest obligation"

        control = InternalControl(
            bank_id=uuid.uuid4(), control_ref=f"PYTEST-{uuid.uuid4().hex[:6]}", title="pytest control",
            control_type=ControlType.PREVENTIVE, confidence=0.8,
            created_by_agent="pytest", model_id="pytest-model", prompt_version="v1", run_id=run_id, provenance=prov,
        )
        ctrl_row = db.insert_control(control)
        created["controls"].append(ctrl_row["id"])

        gap = GapFinding(
            obligation_id=obl_row["id"], gap_class="no_control", narrative="pytest gap",
            risk_factors={"regulatory_severity": 3}, risk_score=50, risk_band=RiskSeverity.MEDIUM,
            confidence=0.7, created_by_agent="pytest", model_id="pytest-model", prompt_version="v1",
            run_id=run_id, provenance=prov,
        )
        gap_row = db.insert_gap(gap)
        created["gaps"].append(gap_row["id"])
        assert db.get_gap(uuid.UUID(gap_row["id"]))["gap_class"] == "no_control"
    finally:
        for table in ["gaps", "controls", "obligations", "clauses", "document_versions", "regulatory_documents", "runs"]:
            for _id in created[table]:
                client.table(table).delete().eq("id", _id).execute()
