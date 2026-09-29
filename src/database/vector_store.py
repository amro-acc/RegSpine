"""ChromaDB embedded persistent store + bge-reranker-base cross-encoder
reranking (spec.md §7.2.7, §14.1.1).

Both the embedding model and the reranker are loaded once and cached
module-level (not just the reranker, as originally scoped — MiniLM is exactly
as expensive to reload repeatedly, so the same singleton pattern applies to
both for the same reason).

persist_directory comes from config/pipeline.yaml (`chroma_db` at repo root,
matching spec.md §14.2/CLAUDE.md §6/.gitignore) — not hardcoded, not
data/chroma_db.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_CONFIG_PATH = REPO_ROOT / "config" / "pipeline.yaml"

COLLECTION_OBLIGATIONS = "regulatory_obligations"
COLLECTION_CONTROLS = "internal_controls"


@lru_cache(maxsize=1)
def _load_config() -> dict:
    with open(PIPELINE_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def _persist_directory() -> Path:
    configured = _load_config()["chroma"]["persist_directory"]
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


@lru_cache(maxsize=1)
def get_chroma_client():
    import chromadb

    return chromadb.PersistentClient(path=str(_persist_directory()))


@lru_cache(maxsize=1)
def get_embedding_model():
    """MiniLM, loaded once. Not the field this task named as needing a
    singleton, but equally expensive to reload — same pattern applied for
    the same reason as the reranker below."""
    from sentence_transformers import SentenceTransformer

    model_name = _load_config()["chroma"]["embedding_model"]
    return SentenceTransformer(model_name)


@lru_cache(maxsize=1)
def get_reranker():
    """bge-reranker-base, loaded once at first use and cached for the life of
    the process — this is the singleton this task explicitly asked for."""
    from FlagEmbedding import FlagReranker

    model_name = _load_config()["chroma"]["reranker_model"]
    return FlagReranker(model_name, use_fp16=True)


def get_collection(collection_name: str):
    return get_chroma_client().get_or_create_collection(name=collection_name)


def upsert_documents(collection_name: str, docs: list[dict]) -> int:
    """`docs` is a list of {"id": str, "text": str, "metadata": dict}.
    Returns the number of documents upserted."""
    if not docs:
        return 0

    ids = [d["id"] for d in docs]
    texts = [d["text"] for d in docs]
    # This ChromaDB version rejects an empty metadata dict outright (found by
    # actually running this against real data, not assumed) — fall back to a
    # non-empty placeholder rather than let every caller need to know that.
    metadatas = [d.get("metadata") or {"upserted_by": "vector_store"} for d in docs]

    embeddings = get_embedding_model().encode(texts).tolist()

    get_collection(collection_name).upsert(
        ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas
    )
    return len(docs)


def query_with_rerank(
    collection_name: str,
    query: str,
    top_k: int | None = None,
    rerank_top_n: int | None = None,
) -> list[dict]:
    """Embed -> retrieve top_k -> cross-encoder rerank -> return top
    rerank_top_n (spec.md §7.2.7's embed/rerank/adjudicate flow — this
    function is the first two steps).

    Defaults come from config/pipeline.yaml's retrieval.top_k_initial/
    top_k_reranked rather than being hardcoded here a second time with
    different numbers (CLAUDE.md §5: config over constants) — still
    overridable per call.
    """
    retrieval_cfg = _load_config()["retrieval"]
    top_k = top_k if top_k is not None else retrieval_cfg["top_k_initial"]
    rerank_top_n = rerank_top_n if rerank_top_n is not None else retrieval_cfg["top_k_reranked"]

    query_embedding = get_embedding_model().encode([query]).tolist()
    results = get_collection(collection_name).query(query_embeddings=query_embedding, n_results=top_k)

    ids = results["ids"][0]
    documents = results["documents"][0]
    metadatas = results["metadatas"][0]

    if not documents:
        return []

    pairs = [[query, doc] for doc in documents]
    rerank_scores = get_reranker().compute_score(pairs)
    if isinstance(rerank_scores, float):  # compute_score returns a bare float for a single pair
        rerank_scores = [rerank_scores]

    reranked = sorted(
        zip(ids, documents, metadatas, rerank_scores, strict=True),
        key=lambda row: row[3],
        reverse=True,
    )

    return [
        {"id": _id, "text": doc, "metadata": meta, "rerank_score": score}
        for _id, doc, meta, score in reranked[:rerank_top_n]
    ]
