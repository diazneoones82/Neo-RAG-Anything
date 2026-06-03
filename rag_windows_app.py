from __future__ import annotations

import json
import math
import os
import re
import hashlib
import shutil
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable
from xml.etree import ElementTree


APP_NAME = "Neo RAG-Anything"
APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "NeoRAGAnything"
CONFIG_PATH = APP_DIR / "config.json"
STORE_DIR = APP_DIR / "store"
SOURCE_DIR = STORE_DIR / "sources"
INDEX_PATH = STORE_DIR / "index.json"
SEARCH_CACHE_PATH = STORE_DIR / "search_cache.sqlite"
CHROMA_DIR = STORE_DIR / "chroma"
CHROMA_COLLECTION = "neo_rag_chunks"
VECTOR_DIMENSIONS = 384
VECTOR_BATCH_SIZE = 256
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".text",
    ".md",
    ".markdown",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".webp",
}
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}")
HF_AUTO_BEST_MODELS = [
    "meta-llama/Llama-3.3-70B-Instruct:sambanova",
    "Qwen/Qwen3-32B:groq",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct:groq",
    "deepseek-ai/DeepSeek-R1:sambanova",
    "deepseek-ai/DeepSeek-R1:hyperbolic",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B:nscale",
    "deepseek-ai/DeepSeek-R1-Distill-Llama-70B:scaleway",
]
OPENROUTER_FALLBACK_MODELS = [
    "deepseek/deepseek-r1:free",
    "openrouter/free",
    "deepseek/deepseek-r1-0528:free",
    "deepseek/deepseek-r1-distill-llama-70b:free",
    "qwen/qwen3-32b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "based",
    "by",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "give",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "with",
}


@dataclass
class ParseResult:
    text: str
    method: str
    notes: str = ""


def load_app_config() -> dict[str, Any]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_app_config(config: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def configure_storage(storage_dir: str | Path | None = None) -> Path:
    global STORE_DIR, SOURCE_DIR, INDEX_PATH, SEARCH_CACHE_PATH, CHROMA_DIR
    config = load_app_config()
    requested = Path(storage_dir or config.get("storage_dir") or (APP_DIR / "store")).expanduser()
    STORE_DIR = requested.resolve()
    SOURCE_DIR = STORE_DIR / "sources"
    INDEX_PATH = STORE_DIR / "index.json"
    SEARCH_CACHE_PATH = STORE_DIR / "search_cache.sqlite"
    CHROMA_DIR = STORE_DIR / "chroma"
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return STORE_DIR


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]


def read_text_file(path: Path) -> ParseResult:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return ParseResult(path.read_text(encoding=encoding, errors="ignore"), f"text/{encoding}")
        except UnicodeError:
            continue
    return ParseResult(path.read_text(errors="ignore"), "text")


def read_docx(path: Path) -> ParseResult:
    paragraphs: list[str] = []
    with zipfile.ZipFile(path) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_bytes)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    return ParseResult("\n\n".join(paragraphs), "docx/xml")


def read_pdf(path: Path) -> ParseResult:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        return ParseResult(
            "",
            "pdf/unavailable",
            f"Install pypdf for PDF text extraction. Details: {exc}",
        )

    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"## Page {index}\n\n{text.strip()}")
    return ParseResult("\n\n".join(pages), "pypdf")


def read_image(path: Path) -> ParseResult:
    details = [f"Image file: {path.name}"]
    try:
        from PIL import Image

        with Image.open(path) as image:
            details.append(f"Dimensions: {image.width} x {image.height}")
            details.append(f"Mode: {image.mode}")
    except Exception as exc:
        details.append(f"Image metadata unavailable: {exc}")

    try:
        import pytesseract

        text = pytesseract.image_to_string(str(path)).strip()
        if text:
            details.append("OCR text:")
            details.append(text)
            return ParseResult("\n".join(details), "image/pytesseract")
    except Exception as exc:
        details.append(f"OCR not available: {exc}")

    tesseract = shutil.which("tesseract")
    if tesseract:
        try:
            completed = subprocess.run(
                [tesseract, str(path), "stdout"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=120,
            )
            text = completed.stdout.strip()
            if text:
                details.append("OCR text:")
                details.append(text)
                return ParseResult("\n".join(details), "image/tesseract")
        except Exception as exc:
            details.append(f"Tesseract command failed: {exc}")

    return ParseResult(
        "\n".join(details),
        "image/metadata",
        "For stronger image understanding, add OCR or a local vision model.",
    )


def read_doc_via_libreoffice(path: Path) -> ParseResult:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return ParseResult("", "doc/unavailable", "Install LibreOffice to ingest legacy .doc files.")
    temp_dir = STORE_DIR / "_office"
    temp_dir.mkdir(parents=True, exist_ok=True)
    command = [
        soffice,
        "--headless",
        "--convert-to",
        "txt:Text",
        "--outdir",
        str(temp_dir),
        str(path),
    ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    converted = temp_dir / f"{path.stem}.txt"
    if not converted.exists():
        return ParseResult("", "doc/libreoffice", "LibreOffice ran, but no text output was created.")
    return ParseResult(converted.read_text(errors="ignore"), "doc/libreoffice")


def parse_document(path: Path) -> ParseResult:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".text", ".md", ".markdown"}:
        return read_text_file(path)
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".doc":
        return read_doc_via_libreoffice(path)
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}:
        return read_image(path)
    return ParseResult("", "unsupported", f"Unsupported file type: {suffix}")


def chunk_text(text: str, strategy: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    if strategy == "Paragraph":
        blocks = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]
        return merge_blocks(blocks, size)
    if strategy == "Recursive":
        blocks = split_recursive(text, size)
        return add_overlap(blocks, overlap)
    return fixed_chunks(text, size, overlap)


def fixed_chunks(text: str, size: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = max(1, size - overlap)
    chunks = []
    for start in range(0, len(words), step):
        chunk = " ".join(words[start : start + size]).strip()
        if chunk:
            chunks.append(chunk)
        if start + size >= len(words):
            break
    return chunks


def split_recursive(text: str, size: int) -> list[str]:
    candidates = [text]
    for separator in ("\n## ", "\n# ", "\n\n", "\n", ". "):
        next_candidates: list[str] = []
        for item in candidates:
            if len(item.split()) <= size:
                next_candidates.append(item.strip())
                continue
            parts = item.split(separator)
            if len(parts) == 1:
                next_candidates.extend(fixed_chunks(item, size, max(50, size // 8)))
            else:
                for idx, part in enumerate(parts):
                    prefix = separator.strip() + " " if idx and separator.startswith("\n#") else ""
                    next_candidates.append((prefix + part).strip())
        candidates = [item for item in next_candidates if item.strip()]
    return merge_blocks(candidates, size)


def merge_blocks(blocks: list[str], size: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for block in blocks:
        words = len(block.split())
        if current and count + words > size:
            chunks.append("\n\n".join(current).strip())
            current = []
            count = 0
        if words > size:
            chunks.extend(fixed_chunks(block, size, max(50, size // 8)))
            continue
        current.append(block)
        count += words
    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


def add_overlap(chunks: list[str], overlap: int) -> list[str]:
    if overlap <= 0:
        return chunks
    output = []
    previous_tail = ""
    for chunk in chunks:
        combined = f"{previous_tail}\n\n{chunk}".strip() if previous_tail else chunk
        output.append(combined)
        previous_tail = " ".join(chunk.split()[-overlap:])
    return output


def page_for_text(text: str) -> int | None:
    matches = re.findall(r"##\s+Page\s+(\d+)", text, flags=re.IGNORECASE)
    if not matches:
        return None
    return int(matches[-1])


def compact_chunk_for_storage(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in chunk.items()
        if key not in {"tokens", "embedding", "vector"}
    }


class RAGStore:
    def __init__(self) -> None:
        configure_storage()
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        self.documents: list[dict[str, Any]] = []
        self.chunks: list[dict[str, Any]] = []
        self._token_counts_by_id: dict[str, Counter[str]] = {}
        self._doc_freq: Counter[str] = Counter()
        self._chunk_by_id: dict[str, dict[str, Any]] = {}
        self.vector_available = False
        self.vector_error = ""
        self._vector_client: Any = None
        self._vector_collection: Any = None
        self._vector_initialized = False
        self.search_cache_available = False
        self.search_cache_error = ""
        self._search_db: sqlite3.Connection | None = None
        self.load()
        self._rebuild_retrieval_cache()

    def load(self) -> None:
        if not INDEX_PATH.exists():
            return
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        self.documents = data.get("documents", [])
        self.chunks = data.get("chunks", [])

    def save(self) -> None:
        payload = {
            "schema": 2,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "documents": self.documents,
            "chunks": [compact_chunk_for_storage(chunk) for chunk in self.chunks],
        }
        INDEX_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _rebuild_retrieval_cache(self) -> None:
        self._token_counts_by_id = {}
        self._doc_freq = Counter()
        self._chunk_by_id = {}
        for chunk in self.chunks:
            chunk_id = str(chunk.get("id"))
            self._chunk_by_id[chunk_id] = chunk

    def clear(self) -> None:
        self.documents = []
        self.chunks = []
        self._rebuild_retrieval_cache()
        self._reset_vector_store()
        self._reset_search_cache()
        self.save()

    def add_document(self, source: Path, strategy: str, chunk_size: int, overlap: int) -> dict[str, Any]:
        parsed = parse_document(source)
        if not parsed.text.strip():
            raise RuntimeError(parsed.notes or f"No text could be extracted from {source.name}")

        doc_id = uuid.uuid4().hex[:12]
        copy_path = SOURCE_DIR / f"{doc_id}_{source.name}"
        shutil.copy2(source, copy_path)
        chunks = chunk_text(parsed.text, strategy, chunk_size, overlap)
        record = {
            "id": doc_id,
            "name": source.name,
            "path": str(copy_path),
            "original_path": str(source),
            "extension": source.suffix.lower(),
            "parser": parsed.method,
            "notes": parsed.notes,
            "chunk_strategy": strategy,
            "chunk_count": len(chunks),
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.documents.append(record)
        new_chunks: list[dict[str, Any]] = []
        for number, chunk in enumerate(chunks, start=1):
            page = page_for_text(chunk)
            chunk_record = {
                    "id": f"{doc_id}-{number}",
                    "doc_id": doc_id,
                    "document": source.name,
                    "chunk": number,
                    "page": page,
                    "extension": source.suffix.lower(),
                    "text": chunk,
                }
            self.chunks.append(chunk_record)
            new_chunks.append(chunk_record)
        self._rebuild_retrieval_cache()
        self._upsert_vector_chunks(new_chunks)
        self.save()
        self._upsert_search_cache(new_chunks)
        self._write_search_cache_signature()
        return record

    def retrieve(
        self,
        question: str,
        limit: int = 6,
        mode: str = "hybrid",
        use_vector: bool = True,
        prefer_exact: bool = True,
    ) -> list[dict[str, Any]]:
        mode = (mode or "hybrid").lower()
        if mode == "lexical":
            return self._retrieve_lexical(question, limit, prefer_exact=prefer_exact)
        if mode == "semantic":
            return self._retrieve_semantic(question, limit, prefer_exact=prefer_exact)
        return self._retrieve_hybrid(question, limit, use_vector=use_vector, prefer_exact=prefer_exact)

    def _retrieve_hybrid(self, question: str, limit: int, use_vector: bool = True, prefer_exact: bool = True) -> list[dict[str, Any]]:
        lexical = self._retrieve_lexical(question, limit=max(limit * 8, 80), prefer_exact=prefer_exact)
        semantic_candidates = lexical or self._retrieve_fts_candidates(question, limit=max(limit * 30, 240), prefer_exact=prefer_exact)
        semantic = self._retrieve_semantic(question, limit=max(limit * 8, 80), candidates=semantic_candidates or None, prefer_exact=prefer_exact)
        rankings = [lexical, semantic]
        if use_vector:
            rankings.append(self._retrieve_vector(question, limit=max(limit * 8, 80)))
        fused = reciprocal_rank_fusion(rankings)
        if prefer_exact:
            fused = rerank_by_exact_similarity(question, fused)
        return fused[:limit]

    def _retrieve_lexical(self, question: str, limit: int, prefer_exact: bool = True) -> list[dict[str, Any]]:
        cached = self._retrieve_fts_candidates(question, limit=limit, prefer_exact=prefer_exact)
        if cached:
            return cached
        query_tokens = [token for token in tokenize(question) if token not in STOP_WORDS]
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)
        total = max(1, len(self.chunks))
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in self.chunks:
            token_counts = self._token_counts_by_id.get(str(chunk.get("id"))) or Counter(chunk.get("tokens") or tokenize(chunk.get("text", "")))
            score = 0.0
            for token, count in query_counts.items():
                if token in token_counts:
                    idf = math.log((1 + total) / (1 + self._doc_freq[token])) + 1
                    score += count * token_counts[token] * idf
            if prefer_exact:
                score += exact_string_score(question, chunk) * 5.0
            score += phrase_match_boost(question, f"{chunk.get('document', '')} {chunk.get('text', '')}") * 8.0
            if score:
                scored.append((score / max(24, len(token_counts)), chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def _retrieve_semantic(
        self,
        question: str,
        limit: int,
        candidates: list[dict[str, Any]] | None = None,
        prefer_exact: bool = True,
    ) -> list[dict[str, Any]]:
        query_terms = semantic_terms(question)
        if not query_terms:
            return []
        search_space = candidates or self._retrieve_fts_candidates(question, limit=max(limit * 40, 400), prefer_exact=prefer_exact) or self.chunks
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in search_space:
            terms = semantic_terms_from_chunk(chunk)
            if not terms:
                continue
            score = cosine_counter(query_terms, terms)
            score += phrase_match_boost(question, str(chunk.get("text") or ""))
            if prefer_exact:
                score += exact_string_score(question, chunk)
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def _retrieve_fts_candidates(self, question: str, limit: int, prefer_exact: bool = True) -> list[dict[str, Any]]:
        query = fts_query_from_question(question)
        if not query:
            return []
        db = self._ensure_search_cache()
        if not db:
            return []
        try:
            rows = db.execute(
                """
                SELECT chunk_id, bm25(chunks_fts) AS rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, max(1, limit)),
            ).fetchall()
        except Exception as exc:
            self.search_cache_error = str(exc)
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk_id, rank in rows:
            chunk = self._chunk_by_id.get(str(chunk_id))
            if not chunk:
                continue
            score = -float(rank or 0.0)
            if prefer_exact:
                score += exact_string_score(question, chunk) * 8.0
            score += phrase_match_boost(question, f"{chunk.get('document', '')} {chunk.get('text', '')}") * 2.0
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def _ensure_search_cache(self) -> sqlite3.Connection | None:
        if self._search_db:
            if self._search_cache_is_current(self._search_db):
                return self._search_db
            self._rebuild_search_cache(self._search_db)
            return self._search_db
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(SEARCH_CACHE_PATH), check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(chunk_id UNINDEXED, document, text, tokenize='unicode61')
                """
            )
            self._search_db = db
            self.search_cache_available = True
            self.search_cache_error = ""
            if not self._search_cache_is_current(db):
                self._rebuild_search_cache(db)
            return db
        except Exception as exc:
            self.search_cache_available = False
            self.search_cache_error = str(exc)
            self._search_db = None
            return None

    def _search_cache_signature(self) -> str:
        try:
            stat = INDEX_PATH.stat()
            return f"{stat.st_size}:{stat.st_mtime_ns}:{len(self.chunks)}"
        except OSError:
            return f"missing:0:{len(self.chunks)}"

    def _search_cache_is_current(self, db: sqlite3.Connection) -> bool:
        try:
            signature = db.execute("SELECT value FROM cache_meta WHERE key = 'index_signature'").fetchone()
            count = db.execute("SELECT count(*) FROM chunks_fts").fetchone()
            return bool(signature and signature[0] == self._search_cache_signature() and count and int(count[0]) >= len(self.chunks))
        except Exception:
            return False

    def _rebuild_search_cache(self, db: sqlite3.Connection) -> None:
        try:
            db.execute("DELETE FROM chunks_fts")
            batch = (
                (
                    str(chunk.get("id") or ""),
                    str(chunk.get("document") or ""),
                    str(chunk.get("text") or ""),
                )
                for chunk in self.chunks
            )
            db.executemany("INSERT INTO chunks_fts(chunk_id, document, text) VALUES (?, ?, ?)", batch)
            self._write_search_cache_signature(db)
            db.commit()
            self.search_cache_available = True
            self.search_cache_error = ""
        except Exception as exc:
            db.rollback()
            self.search_cache_available = False
            self.search_cache_error = str(exc)

    def _upsert_search_cache(self, chunks: list[dict[str, Any]]) -> None:
        if not chunks:
            return
        db = self._ensure_search_cache()
        if not db:
            return
        try:
            for chunk in chunks:
                chunk_id = str(chunk.get("id") or "")
                db.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
                db.execute(
                    "INSERT INTO chunks_fts(chunk_id, document, text) VALUES (?, ?, ?)",
                    (chunk_id, str(chunk.get("document") or ""), str(chunk.get("text") or "")),
                )
            db.commit()
            self.search_cache_available = True
            self.search_cache_error = ""
        except Exception as exc:
            db.rollback()
            self.search_cache_available = False
            self.search_cache_error = str(exc)

    def _write_search_cache_signature(self, db: sqlite3.Connection | None = None) -> None:
        target = db or self._search_db
        if not target:
            return
        target.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('index_signature', ?)",
            (self._search_cache_signature(),),
        )
        target.commit()

    def _reset_search_cache(self) -> None:
        if self._search_db:
            self._search_db.close()
            self._search_db = None
        for path in (
            SEARCH_CACHE_PATH,
            Path(str(SEARCH_CACHE_PATH) + "-wal"),
            Path(str(SEARCH_CACHE_PATH) + "-shm"),
        ):
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass
        self.search_cache_available = False
        self.search_cache_error = ""

    def _retrieve_vector(self, question: str, limit: int) -> list[dict[str, Any]]:
        if not self._vector_initialized:
            self._init_vector_store()
        if not self._vector_collection or not self.chunks:
            return []
        try:
            results = self._vector_collection.query(
                query_embeddings=[deterministic_embedding(question)],
                n_results=min(max(limit, 1), max(len(self.chunks), 1)),
                include=["distances"],
            )
        except Exception as exc:
            self.vector_error = str(exc)
            return []
        ids = (results.get("ids") or [[]])[0]
        distances = (results.get("distances") or [[]])[0]
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk_id, distance in zip(ids, distances):
            chunk = self._chunk_by_id.get(str(chunk_id))
            if not chunk:
                continue
            score = 1.0 / (1.0 + float(distance or 0.0))
            score += phrase_match_boost(question, str(chunk.get("text") or ""))
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in scored[:limit]]

    def _init_vector_store(self) -> None:
        if self._vector_initialized:
            return
        self._vector_initialized = True
        try:
            import chromadb

            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            self._vector_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            self._vector_collection = self._vector_client.get_or_create_collection(
                CHROMA_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            self.vector_available = True
            self.vector_error = ""
        except Exception as exc:
            self._vector_client = None
            self._vector_collection = None
            self.vector_available = False
            self.vector_error = str(exc)

    def _ensure_vector_index(self) -> None:
        if not self._vector_initialized:
            self._init_vector_store()
        if not self._vector_collection or not self.chunks:
            return
        try:
            count = int(self._vector_collection.count())
        except Exception:
            count = 0
        if count < len(self.chunks):
            self._upsert_vector_chunks(self.chunks)

    def _upsert_vector_chunks(
        self,
        chunks: list[dict[str, Any]],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> None:
        if not self._vector_initialized:
            self._init_vector_store()
        if not self._vector_collection or not chunks:
            return
        total = len(chunks)
        done = 0
        for start in range(0, total, VECTOR_BATCH_SIZE):
            batch = chunks[start : start + VECTOR_BATCH_SIZE]
            try:
                self._vector_collection.upsert(
                    ids=[str(chunk.get("id")) for chunk in batch],
                    embeddings=[deterministic_embedding(chunk_text_for_embedding(chunk)) for chunk in batch],
                    documents=[str(chunk.get("text") or "") for chunk in batch],
                    metadatas=[chunk_metadata(chunk) for chunk in batch],
                )
                done += len(batch)
                self.vector_available = True
                self.vector_error = ""
                if progress_callback:
                    progress_callback(done, total, f"Indexed vectors {done}/{total}")
            except Exception as exc:
                self.vector_error = str(exc)
                if progress_callback:
                    progress_callback(done, total, f"Vector batch failed: {exc}")
                break

    def _reset_vector_store(self) -> None:
        if self._vector_client:
            try:
                self._vector_client.delete_collection(CHROMA_COLLECTION)
            except Exception:
                pass
        self.close_vector_store()
        if CHROMA_DIR.exists():
            try:
                shutil.rmtree(CHROMA_DIR)
            except Exception:
                pass
        self._init_vector_store()

    def close_vector_store(self) -> None:
        self._vector_collection = None
        self._vector_client = None
        self.vector_available = False
        self._vector_initialized = False

    def close_search_cache(self) -> None:
        if self._search_db:
            self._search_db.close()
            self._search_db = None


def reciprocal_rank_fusion(rankings: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    chunks: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            chunk_id = str(chunk.get("id"))
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            chunks[chunk_id] = chunk
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [chunks[chunk_id] for chunk_id, _score in ordered if chunk_id in chunks]


def rerank_by_exact_similarity(question: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for rank, chunk in enumerate(chunks, start=1):
        score = exact_string_score(question, chunk)
        score += 1.0 / (rank + 10)
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for score, chunk in scored if score > 0]


def semantic_terms(text: str) -> Counter[str]:
    words = [token for token in tokenize(text) if token not in STOP_WORDS]
    terms: Counter[str] = Counter(words)
    for word in words[:80]:
        if len(word) >= 6:
            for index in range(0, len(word) - 2):
                terms[f"tri:{word[index:index + 3]}"] += 0.35
    return terms


def semantic_terms_from_chunk(chunk: dict[str, Any]) -> Counter[str]:
    tokens = [
        str(token)
        for token in (chunk.get("tokens") or tokenize(str(chunk.get("text") or "")))
        if str(token) not in STOP_WORDS
    ]
    terms: Counter[str] = Counter(tokens)
    document = str(chunk.get("document") or "")
    terms.update(tokenize(document))
    return terms


def cosine_counter(left: Counter[str], right: Counter[str]) -> float:
    common = set(left) & set(right)
    numerator = sum(float(left[key]) * float(right[key]) for key in common)
    if numerator <= 0:
        return 0.0
    left_norm = math.sqrt(sum(float(value) * float(value) for value in left.values()))
    right_norm = math.sqrt(sum(float(value) * float(value) for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def chunk_text_for_embedding(chunk: dict[str, Any]) -> str:
    return f"{chunk.get('document', '')}\n{chunk.get('text', '')}"


def chunk_metadata(chunk: dict[str, Any]) -> dict[str, str | int | float | bool]:
    page = chunk.get("page")
    return {
        "chunk_id": str(chunk.get("id") or ""),
        "doc_id": str(chunk.get("doc_id") or ""),
        "document": str(chunk.get("document") or ""),
        "chunk": int(chunk.get("chunk") or 0),
        "page": int(page or 0),
        "extension": str(chunk.get("extension") or ""),
    }


def deterministic_embedding(text: str, dimensions: int = VECTOR_DIMENSIONS) -> list[float]:
    vector = [0.0] * dimensions
    tokens = [token for token in tokenize(text) if token not in STOP_WORDS]
    for token in tokens:
        add_hashed_feature(vector, token, 1.0)
        if len(token) >= 6:
            for index in range(0, len(token) - 2):
                add_hashed_feature(vector, f"tri:{token[index:index + 3]}", 0.25)
    normalized = normalize_phrase(text)
    words = normalized.split()
    for size, weight in ((5, 0.9), (4, 0.75), (3, 0.55), (2, 0.35)):
        for index in range(0, max(0, len(words) - size + 1)):
            add_hashed_feature(vector, "phrase:" + " ".join(words[index : index + size]), weight)
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def add_hashed_feature(vector: list[float], feature: str, weight: float) -> None:
    digest = hashlib.blake2b(feature.encode("utf-8", errors="ignore"), digest_size=8).digest()
    number = int.from_bytes(digest, "big")
    index = number % len(vector)
    sign = 1.0 if ((number >> 9) & 1) else -1.0
    vector[index] += weight * sign


def normalize_phrase(text: str) -> str:
    return " ".join(tokenize(text))


def fts_query_from_question(question: str) -> str:
    tokens = [token for token in tokenize(question) if token not in STOP_WORDS]
    if not tokens:
        return ""
    unique_tokens = list(dict.fromkeys(tokens[:24]))
    parts = [quote_fts_term(token) for token in unique_tokens if token]
    phrase = " ".join(unique_tokens[:8])
    if len(unique_tokens) >= 2 and len(phrase) >= 4:
        parts.insert(0, quote_fts_term(phrase))
    return " OR ".join(parts)


def quote_fts_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def phrase_match_boost(question: str, text: str) -> float:
    q = normalize_phrase(question)
    t = normalize_phrase(text)
    if not q or not t:
        return 0.0
    boost = 0.0
    q_tokens = q.split()
    if len(q_tokens) >= 2 and q in t:
        boost += min(2.5, 0.35 * len(q_tokens))
    for size in range(min(8, len(q_tokens)), 1, -1):
        for index in range(0, len(q_tokens) - size + 1):
            phrase = " ".join(q_tokens[index : index + size])
            if phrase and phrase in t:
                boost += 0.12 * size
    return min(boost, 4.0)


def exact_string_score(question: str, chunk: dict[str, Any]) -> float:
    query = normalize_phrase(question)
    text = normalize_phrase(f"{chunk.get('document', '')} {chunk.get('text', '')}")
    if not query or not text:
        return 0.0
    query_tokens = query.split()
    text_tokens = text.split()
    text_token_set = set(text_tokens)
    score = 0.0
    if len(query_tokens) >= 2 and query in text:
        score += 12.0
    elif len(query_tokens) == 1 and query_tokens[0] in text_token_set:
        score += 5.0
    present = sum(1 for token in query_tokens if token in text_token_set)
    if query_tokens:
        score += 4.0 * (present / len(query_tokens))
    window = ordered_match_window(query_tokens, text_tokens)
    if window:
        score += max(0.0, 5.0 - (window - len(query_tokens)) * 0.35)
    for token in query_tokens:
        if len(token) >= 4 and token in text_token_set:
            score += 1.5
    return score


def ordered_match_window(query_tokens: list[str], text_tokens: list[str]) -> int:
    if not query_tokens or not text_tokens:
        return 0
    best = 0
    for start, token in enumerate(text_tokens):
        if token != query_tokens[0]:
            continue
        query_index = 1
        end = start
        while query_index < len(query_tokens) and end + 1 < len(text_tokens):
            end += 1
            if text_tokens[end] == query_tokens[query_index]:
                query_index += 1
        if query_index == len(query_tokens):
            window = end - start + 1
            if not best or window < best:
                best = window
    return best


def phrase_overlap_boost(question: str, text: str) -> float:
    return min(phrase_match_boost(question, text), 0.8)


def vector_status(store: "RAGStore") -> dict[str, Any]:
    count = 0
    if store._vector_collection:
        try:
            count = int(store._vector_collection.count())
        except Exception:
            count = 0
    return {
        "enabled": bool(store.vector_available),
        "path": str(CHROMA_DIR),
        "collection": CHROMA_COLLECTION,
        "count": count,
        "error": store.vector_error,
    }


def search_cache_status(store: "RAGStore") -> dict[str, Any]:
    count = 0
    if store._search_db:
        try:
            row = store._search_db.execute("SELECT count(*) FROM chunks_fts").fetchone()
            count = int(row[0]) if row else 0
        except Exception:
            count = 0
    return {
        "enabled": bool(store.search_cache_available),
        "path": str(SEARCH_CACHE_PATH),
        "count": count,
        "error": store.search_cache_error,
    }


def rebuild_vector_index(store: "RAGStore", progress_callback: Callable[[int, int, str], None] | None = None) -> None:
    store._reset_vector_store()
    if progress_callback:
        progress_callback(0, len(store.chunks), "Starting vector rebuild")
    if store.chunks and not store._vector_collection:
        store.vector_error = store.vector_error or "ChromaDB vector collection is unavailable."
        return
    store._upsert_vector_chunks(store.chunks, progress_callback=progress_callback)


def extractive_answer(question: str, contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return "I could not find matching chunks in the indexed documents."
    query_tokens = set(token for token in tokenize(question) if token not in STOP_WORDS)
    sections = ["Best answer from indexed chunks:"]
    for context in contexts[:4]:
        excerpt = best_excerpt(context["text"], query_tokens)
        sections.append(f"\n[{context['document']} | chunk {context['chunk']}]\n{excerpt}")
    sections.append("\nSources: " + "; ".join(sorted({item["document"] for item in contexts[:4]})))
    return "\n".join(sections)


def answer_with_web_context(question: str, contexts: list[dict[str, Any]], web_results: list[dict[str, str]]) -> str:
    answer = extractive_answer(question, contexts)
    if not web_results:
        return answer + "\n\nInternet search: no useful public web results were found."
    lines = [answer, "\nInternet search cross-check:"]
    for index, result in enumerate(web_results[:5], start=1):
        title = result.get("title", "Untitled")
        snippet = result.get("snippet", "")
        url = result.get("url", "")
        lines.append(f"{index}. {title}\n{snippet}\n{url}")
    return "\n".join(lines)


def best_excerpt(text: str, query_tokens: set[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    scored = []
    for sentence in sentences:
        tokens = set(tokenize(sentence))
        score = len(tokens & query_tokens)
        if score:
            scored.append((score, sentence.strip()))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return " ".join(item[1] for item in scored[:4])[:1600]
    return text[:1600]


def ollama_answer(question: str, contexts: list[dict[str, Any]], model: str) -> str:
    if not model.strip():
        raise RuntimeError("Choose an Ollama model name first.")
    context_text = "\n\n".join(
        f"Source: {item['document']} | Chunk {item['chunk']}\n{item['text']}" for item in contexts
    )
    prompt = (
        "Answer only from the provided document chunks. If the chunks do not contain the answer, say so. "
        "Cite document names and chunk numbers.\n\n"
        f"Question: {question}\n\nDocument chunks:\n{context_text}"
    )
    payload = json.dumps({"model": model.strip(), "prompt": prompt, "stream": False}).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data.get("response", "").strip() or "Ollama returned an empty answer."


def huggingface_answer(
    question: str,
    contexts: list[dict[str, Any]],
    model: str,
    token: str = "",
    provider: str = "sambanova",
) -> str:
    config = load_app_config()
    token = (
        token.strip()
        or str(config.get("hf_token") or "").strip()
        or os.getenv("HF_TOKEN", "").strip()
        or os.getenv("HUGGINGFACEHUB_API_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("Set HF_TOKEN or paste a Hugging Face token for this request.")
    model = model.strip() or "auto-best"
    provider = provider.strip().lower()
    context_text = "\n\n".join(
        f"[{index}] Source: {item['document']} | Chunk {item['chunk']}\n{item['text'][:2400]}"
        for index, item in enumerate(contexts, start=1)
    )
    if not context_text:
        raise RuntimeError("No retrieved chunks are available for Hugging Face generation.")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful RAG answer writer. Use only the provided retrieved chunks. "
                "Write a clear, well-structured answer with short sections or bullets when useful. "
                "Cite evidence as document name and chunk number. If the chunks are insufficient, say what is missing."
            ),
        },
        {
            "role": "user",
            "content": f"Question: {question}\n\nRetrieved chunks:\n{context_text}",
        },
    ]

    errors: list[str] = []
    for candidate in _hf_candidate_models(model, provider):
        try:
            data = _huggingface_chat_request(candidate, messages, token)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("Hugging Face returned no choices.")
            message = choices[0].get("message") or {}
            answer = str(message.get("content") or "").strip()
            if answer:
                return answer
            raise RuntimeError("Hugging Face returned an empty answer.")
        except RuntimeError as exc:
            errors.append(f"{candidate}: {exc}")
            if "cloudflare" not in str(exc).lower() and "access denied" not in str(exc).lower():
                break

    raise RuntimeError("Hugging Face generation failed. Tried: " + " | ".join(errors))


def openrouter_answer(
    question: str,
    contexts: list[dict[str, Any]],
    model: str = "deepseek/deepseek-r1:free",
    token: str = "",
) -> str:
    config = load_app_config()
    token = (
        token.strip()
        or str(config.get("openrouter_token") or "").strip()
        or os.getenv("OPENROUTER_API_KEY", "").strip()
    )
    if not token:
        raise RuntimeError("Set OPENROUTER_API_KEY or paste and save an OpenRouter token.")
    model = model.strip() or "deepseek/deepseek-r1:free"
    context_text = "\n\n".join(
        f"[{index}] Source: {item.get('document', 'Unknown')} | Chunk {item.get('chunk', '')}"
        f"{' | Page ' + str(item.get('page')) if item.get('page') else ''}\n{str(item.get('text') or '')[:2600]}"
        for index, item in enumerate(contexts, start=1)
    )
    if not context_text:
        raise RuntimeError("No retrieved chunks are available for OpenRouter generation.")

    errors: list[str] = []
    for candidate in openrouter_candidate_models(model):
        try:
            data = _openrouter_chat_request(candidate, question, context_text, token)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("OpenRouter returned no choices.")
            message = choices[0].get("message") or {}
            answer = str(message.get("content") or "").strip()
            if answer:
                if candidate != model:
                    return f"{answer}\n\nOpenRouter route used: {candidate}"
                return answer
            raise RuntimeError("OpenRouter returned an empty answer.")
        except RuntimeError as exc:
            errors.append(f"{candidate}: {exc}")
            if "no endpoints found" not in str(exc).lower() and "not found" not in str(exc).lower():
                break
    raise RuntimeError("OpenRouter generation failed. Tried: " + " | ".join(errors))


def openrouter_candidate_models(model: str) -> list[str]:
    if model in {"auto-free", "openrouter/free", "free"}:
        return ["openrouter/free", *[item for item in OPENROUTER_FALLBACK_MODELS if item != "openrouter/free"]]
    candidates = [model]
    candidates.extend(item for item in OPENROUTER_FALLBACK_MODELS if item not in candidates)
    return candidates


def _openrouter_chat_request(model: str, question: str, context_text: str, token: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a careful RAG answer writer. Use only the provided retrieved chunks and optional "
                        "web snippets. Cite document names, chunk numbers, pages, and URLs when used. If evidence is "
                        "insufficient, say what is missing."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nRetrieved evidence:\n{context_text}",
                },
            ],
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 1100,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:7860",
            "X-Title": "Neo RAG-Anything",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenRouter request failed ({exc.code}): {details[:700]}") from exc


def _huggingface_chat_request(model: str, messages: list[dict[str, str]], token: str) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 900,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://router.huggingface.co/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Neo-RAG-Anything/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        concise = _html_error_summary(details) or details[:600]
        raise RuntimeError(f"request failed ({exc.code}): {concise}") from exc


def _hf_candidate_models(model: str, provider: str) -> list[str]:
    if model in {"auto-best", "best", "auto"}:
        return HF_AUTO_BEST_MODELS[:]
    base, suffix = _split_hf_model_suffix(model)
    if provider and provider not in {"model", "auto", "fastest", "cheapest", "preferred"}:
        return [f"{base}:{provider}"]
    if provider == "model" and suffix:
        return [model]
    if suffix in {"fastest", "auto", ""}:
        return [
            f"{base}:groq",
            f"{base}:sambanova",
            f"{base}:together",
            f"{base}:novita",
            f"{base}:deepinfra",
            f"{base}:nscale",
            f"{base}:scaleway",
            f"{base}:hyperbolic",
            model if suffix else f"{base}:fastest",
        ]
    return [model]


def _split_hf_model_suffix(model: str) -> tuple[str, str]:
    if ":" not in model:
        return model, ""
    base, suffix = model.rsplit(":", 1)
    if "/" in suffix:
        return model, ""
    return base, suffix.lower()


def _html_error_summary(details: str) -> str:
    if "<html" not in details.lower():
        return ""
    title = re.search(r"<title>(.*?)</title>", details, flags=re.IGNORECASE | re.DOTALL)
    if title:
        return re.sub(r"\s+", " ", title.group(1)).strip()
    return "HTML error page returned by provider"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.store = RAGStore()
        self._build_ui()
        self.refresh_documents()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(14, 12, 14, 8))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(5, weight=1)

        ttk.Button(top, text="Upload Files", command=self.upload_files).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(top, text="Upload Folder", command=self.upload_folder).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(top, text="Chunking").grid(row=0, column=2, padx=(0, 6))
        self.strategy = tk.StringVar(value="Recursive")
        ttk.Combobox(top, textvariable=self.strategy, values=("Recursive", "Paragraph", "Fixed"), width=12, state="readonly").grid(row=0, column=3, padx=(0, 8))
        self.chunk_size = tk.IntVar(value=520)
        ttk.Spinbox(top, from_=180, to=1600, increment=40, textvariable=self.chunk_size, width=6).grid(row=0, column=4, padx=(0, 8))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(top, textvariable=self.status, anchor="e").grid(row=0, column=5, sticky="ew")

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))

        left = ttk.Frame(paned, padding=10)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        paned.add(left, weight=1)

        ttk.Label(left, text="Indexed Documents").grid(row=0, column=0, sticky="w")
        self.docs = tk.Listbox(left, height=16, activestyle="none")
        self.docs.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        ttk.Button(left, text="Clear Index", command=self.clear_index).grid(row=2, column=0, sticky="ew")

        right = ttk.Frame(paned, padding=10)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        paned.add(right, weight=3)

        query_bar = ttk.Frame(right)
        query_bar.grid(row=0, column=0, sticky="ew")
        query_bar.columnconfigure(0, weight=1)
        self.question = tk.StringVar()
        entry = ttk.Entry(query_bar, textvariable=self.question)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda _event: self.ask())
        ttk.Button(query_bar, text="Ask", command=self.ask).grid(row=0, column=1)

        options = ttk.Frame(right)
        options.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        self.use_ollama = tk.BooleanVar(value=False)
        self.ollama_model = tk.StringVar(value="llama3.2")
        ttk.Checkbutton(options, text="Use Ollama generation", variable=self.use_ollama).grid(row=0, column=0, padx=(0, 8))
        ttk.Label(options, text="Model").grid(row=0, column=1, padx=(0, 6))
        ttk.Entry(options, textvariable=self.ollama_model, width=24).grid(row=0, column=2)

        self.answer = tk.Text(right, wrap="word", font=("Segoe UI", 10), padx=12, pady=12)
        self.answer.grid(row=2, column=0, sticky="nsew")
        self.answer.insert("1.0", "Upload documents, then ask a question. Answers are constrained to retrieved chunks.")

    def refresh_documents(self) -> None:
        self.docs.delete(0, tk.END)
        for record in self.store.documents:
            note = f"{record['name']}  [{record['chunk_count']} chunks, {record['parser']}]"
            self.docs.insert(tk.END, note)
        self.status.set(f"{len(self.store.documents)} documents, {len(self.store.chunks)} chunks indexed")

    def upload_files(self) -> None:
        filetypes = [("Documents", " ".join(f"*{ext}" for ext in sorted(SUPPORTED_EXTENSIONS))), ("All files", "*.*")]
        paths = filedialog.askopenfilenames(title="Upload documents", filetypes=filetypes)
        if paths:
            self.ingest([Path(item) for item in paths])

    def upload_folder(self) -> None:
        folder = filedialog.askdirectory(title="Upload folder")
        if not folder:
            return
        paths = [path for path in Path(folder).rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS]
        self.ingest(paths)

    def ingest(self, paths: list[Path]) -> None:
        if not paths:
            messagebox.showinfo(APP_NAME, "No supported documents found.")
            return

        def worker() -> None:
            failures = []
            for index, path in enumerate(paths, start=1):
                self.set_status(f"Ingesting {index}/{len(paths)}: {path.name}")
                try:
                    self.store.add_document(path, self.strategy.get(), int(self.chunk_size.get()), max(40, int(self.chunk_size.get()) // 8))
                except Exception as exc:
                    failures.append(f"{path.name}: {exc}")
            self.after(0, self.refresh_documents)
            if failures:
                self.after(0, lambda: messagebox.showwarning(APP_NAME, "Some files were skipped:\n\n" + "\n".join(failures[:10])))
            self.set_status("Ingestion complete")

        threading.Thread(target=worker, daemon=True).start()

    def ask(self) -> None:
        question = self.question.get().strip()
        if not question:
            return
        self.answer.delete("1.0", tk.END)
        self.answer.insert("1.0", "Retrieving chunks...")

        def worker() -> None:
            contexts = self.store.retrieve(question)
            try:
                if self.use_ollama.get():
                    answer = ollama_answer(question, contexts, self.ollama_model.get())
                    mode = "Ollama + retrieved chunks"
                else:
                    answer = extractive_answer(question, contexts)
                    mode = "Local retrieval"
            except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                answer = extractive_answer(question, contexts) + f"\n\nGeneration fallback: {exc}"
                mode = "Local retrieval fallback"
            self.after(0, lambda: self.show_answer(answer, mode))

        threading.Thread(target=worker, daemon=True).start()

    def show_answer(self, answer: str, mode: str) -> None:
        self.answer.delete("1.0", tk.END)
        self.answer.insert("1.0", f"{mode}\n\n{answer}")
        self.status.set(mode)

    def clear_index(self) -> None:
        if not messagebox.askyesno(APP_NAME, "Clear indexed documents and chunks? Source copies in the app store will remain on disk."):
            return
        self.store.clear()
        self.refresh_documents()

    def set_status(self, value: str) -> None:
        self.after(0, lambda: self.status.set(value))


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
