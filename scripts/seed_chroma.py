"""Rebuild the Chroma index from corpus/ and update corpus/manifest.json.

This is the `make index` step (CLAUDE.md §2: "rebuild Chroma from corpus/
(deterministic; ~2 min CPU)"). Deterministic in the sense that the same
corpus files always produce the same manifest hashes and the same collection
contents — re-running this is how every developer gets an identical local
index without committing the binary chroma_db/ directory (spec.md §14.2).

Scope note: seeds two small SYNTHETIC documents (corpus/regulations/
dummy_regulation.txt, corpus/bank/dummy_policy.txt) — the real Basel/DORA/
PCI DSS corpus and Meridian bank artifacts (spec.md §13.1/§13.2) haven't been
acquired yet. Whole-file-as-one-chunk for now; real clause segmentation
(spec.md §7.2.3) isn't built yet either — each dummy file becomes exactly one
Chroma document.

Usage:
    python scripts/seed_chroma.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

# (file path, corpus doc type, target Chroma collection)
SEED_FILES = [
    (CORPUS_DIR / "regulations" / "dummy_regulation.txt", "regulation", "regulatory_obligations"),
    (CORPUS_DIR / "bank" / "dummy_policy.txt", "policy", "internal_controls"),
]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from src.database import vector_store

    manifest_documents = []
    total_upserted = 0

    for file_path, doc_type, collection_name in SEED_FILES:
        if not file_path.exists():
            print(f"WARNING: {file_path} not found, skipping")
            continue

        text = file_path.read_text(encoding="utf-8")
        sha256 = _sha256_file(file_path)

        doc_id = f"{doc_type}:{file_path.name}"
        count = vector_store.upsert_documents(
            collection_name,
            [
                {
                    "id": doc_id,
                    "text": text,
                    "metadata": {"source_file": file_path.name, "doc_type": doc_type},
                }
            ],
        )
        total_upserted += count

        manifest_documents.append(
            {
                "filename": file_path.name,
                "relative_path": file_path.relative_to(REPO_ROOT).as_posix(),  # forward slashes on every OS
                "type": doc_type,
                "sha256": sha256,
                "collection": collection_name,
            }
        )
        print(f"Indexed {file_path.name} -> collection '{collection_name}' (sha256={sha256[:12]}...)")

    manifest = {
        "generated_by": "scripts/seed_chroma.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": manifest_documents,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nUpserted {total_upserted} document(s) total.")
    print(f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
