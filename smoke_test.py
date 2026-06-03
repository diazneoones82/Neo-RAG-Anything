from __future__ import annotations

import tempfile
from pathlib import Path

from rag_windows_app import RAGStore


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        sample = Path(temp) / "sample.txt"
        sample.write_text(
            "Neo RAG-Anything builds a hybrid retrieval index.\n\n"
            "This Windows app chunks uploaded documents and answers from retrieved chunks.",
            encoding="utf-8",
        )
        store = RAGStore()
        before_docs = len(store.documents)
        before_chunks = len(store.chunks)
        record = store.add_document(sample, "Recursive", 120, 20)
        hits = store.retrieve("What does the Windows desk app do?", limit=3)
        store.documents = store.documents[:before_docs]
        store.chunks = store.chunks[:before_chunks]
        store.save()
        assert record["chunk_count"] >= 1
        assert hits
        assert "Windows desk app" in hits[0]["text"]
    print("smoke ok")


if __name__ == "__main__":
    main()
