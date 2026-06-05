from __future__ import annotations

import base64
import email.parser
import email.policy
import gc
import html
import json
import mimetypes
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rag_windows_app import (
    APP_DIR,
    SUPPORTED_EXTENSIONS,
    RAGStore,
    answer_with_web_context,
    configure_storage,
    extractive_answer,
    huggingface_answer,
    load_app_config,
    ollama_answer,
    openrouter_answer,
    rebuild_vector_index,
    save_app_config,
    search_cache_status,
    vector_status,
)


HOST = "127.0.0.1"
PORT = int(os.getenv("RAG_WEBUI_PORT", "7860"))
ROOT = Path(__file__).resolve().parent
STATIC_DIR = Path(getattr(__import__("sys"), "_MEIPASS", ROOT)) / "webui"


class ClientRequestError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.store = RAGStore()
        self.upload_progress: dict[str, Any] = {
            "active": False,
            "phase": "Idle",
            "current": "",
            "done": 0,
            "total": 0,
            "percent": 0,
            "ok": 0,
            "failed": 0,
        }


STATE = State()


class Handler(SimpleHTTPRequestHandler):
    server_version = "NeoRAGAnything/1.0"

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            return self._send_static(STATIC_DIR / "index.html")
        if self.path == "/styles.css":
            return self._send_static(STATIC_DIR / "styles.css")
        if self.path == "/app.js":
            return self._send_static(STATIC_DIR / "app.js")
        if self.path == "/api/status":
            return self._send_json(status_payload())
        if self.path == "/api/upload-progress":
            return self._send_json(upload_progress_payload())
        if self.path.startswith("/api/source"):
            return self._handle_source()
        if self.path.startswith("/api/chunk"):
            return self._handle_chunk()
        if self.path.startswith("/api/export"):
            return self._handle_export()
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        try:
            if self.path == "/api/upload":
                return self._handle_upload()
            if self.path == "/api/query":
                return self._handle_query()
            if self.path == "/api/clear":
                with STATE.lock:
                    STATE.store.clear()
                return self._send_json(status_payload())
            if self.path == "/api/settings":
                return self._handle_settings()
            if self.path == "/api/import":
                return self._handle_import()
            if self.path == "/api/rebuild-vector":
                return self._handle_rebuild_vector()
        except ClientRequestError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        except Exception as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=500)
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            fields, files = self._read_multipart_upload(content_type)
            strategy = str(fields.get("strategy") or "Recursive")
            chunk_size = int(fields.get("chunk_size") or 520)
            overlap = max(40, int(fields.get("overlap") or chunk_size // 8))
        else:
            payload = self._read_json()
            files = payload.get("files") or []
            strategy = str(payload.get("strategy") or "Recursive")
            chunk_size = int(payload.get("chunk_size") or 520)
            overlap = max(40, int(payload.get("overlap") or chunk_size // 8))
        upload_dir().mkdir(parents=True, exist_ok=True)

        results = []
        set_upload_progress(
            {
                "active": True,
                "phase": "Starting ingest",
                "current": "",
                "done": 0,
                "total": len(files),
                "percent": 0,
                "ok": 0,
                "failed": 0,
            }
        )
        with STATE.lock:
            for index, item in enumerate(files, start=1):
                name = safe_upload_name(str(item.get("name") or "upload.bin"))
                set_upload_progress(
                    {
                        "active": True,
                        "phase": "Ingesting",
                        "current": name,
                        "done": index - 1,
                        "total": len(files),
                        "percent": progress_percent(index - 1, len(files)),
                    }
                )
                suffix = Path(name).suffix.lower()
                if suffix not in SUPPORTED_EXTENSIONS:
                    results.append({"name": name, "ok": False, "error": f"Unsupported extension: {suffix}"})
                    bump_upload_progress(index, len(files), name, ok=False, phase="Skipped unsupported file")
                    continue
                data = item.get("data")
                if not isinstance(data, bytes):
                    data = base64.b64decode(str(item.get("content") or ""))
                target = unique_path(upload_dir() / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                try:
                    record = STATE.store.add_document(target, strategy, chunk_size, overlap)
                    results.append({"name": name, "ok": True, "record": record})
                    bump_upload_progress(index, len(files), name, ok=True, phase="Indexed")
                except Exception as exc:
                    results.append({"name": name, "ok": False, "error": str(exc)})
                    bump_upload_progress(index, len(files), name, ok=False, phase="Skipped with error")

        failed = len([item for item in results if not item.get("ok")])
        set_upload_progress(
            {
                "active": False,
                "phase": "Completed with warnings" if failed else "Completed",
                "current": "",
                "done": len(files),
                "total": len(files),
                "percent": 100 if files else 0,
                "ok": len(files) - failed,
                "failed": failed,
            }
        )
        return self._send_json({"ok": True, "results": results, "status": status_payload()})

    def _handle_import(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        try:
            if content_type.startswith("application/zip") or content_type.startswith("application/octet-stream"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = safe_name(str((query.get("name") or ["rag-index.zip"])[0] or "rag-index.zip"))
                length = int(self.headers.get("Content-Length", "0") or "0")
                data = self.rfile.read(length) if length > 0 else b""
            elif content_type.startswith("multipart/form-data"):
                fields, files = self._read_multipart_upload(content_type)
                upload = files[0] if files else {}
                name = safe_name(str(upload.get("name") or fields.get("name") or "rag-index.zip"))
                data = upload.get("data") or b""
            elif content_type.startswith("application/json"):
                payload = self._read_json()
                name = safe_name(str(payload.get("name") or "rag-index.zip"))
                data = base64.b64decode(str(payload.get("content") or ""))
            else:
                raise ClientRequestError(f"Unsupported import upload content type: {content_type or 'missing'}. Select an ingest export ZIP from the Import button.")
        except Exception as exc:
            raise ClientRequestError(f"Import ZIP could not be read from the browser upload: {exc}") from exc
        if not data:
            raise ClientRequestError("Import ZIP is empty or the browser could not read the selected file.")
        set_upload_progress(
            {
                "active": True,
                "phase": "Importing ZIP",
                "current": name,
                "done": 0,
                "total": 5,
                "percent": 0,
                "ok": 0,
                "failed": 0,
            }
        )
        target_root = configure_storage()
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            probe = target_root / "_write_test.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except Exception as exc:
            raise ClientRequestError(f"Active storage folder is not writable: {target_root}. Choose a folder like C:\\RAGData or D:\\RAGData, then save it and import again. Details: {exc}") from exc
        with STATE.lock:
            STATE.store.close_vector_store()
            STATE.store.close_search_cache()
        gc.collect()
        temp_zip = target_root / "_import.zip"
        temp_dir = target_root / "_import_work"
        try:
            try:
                temp_zip.write_bytes(data)
            except Exception as exc:
                raise ClientRequestError(f"Could not stage import ZIP in storage folder {target_root}: {exc}") from exc
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir(parents=True, exist_ok=True)
            set_upload_progress({"active": True, "phase": "Validating ZIP", "done": 1, "percent": 20})
            try:
                with zipfile.ZipFile(temp_zip) as archive:
                    if not archive.infolist():
                        raise ClientRequestError("Import ZIP has no files. Use a ZIP created by Export ingest folder.")
                    for member in archive.infolist():
                        destination = (temp_dir / member.filename).resolve()
                        if not str(destination).startswith(str(temp_dir.resolve())):
                            raise ClientRequestError(f"Unsafe zip entry: {member.filename}")
                    archive.extractall(temp_dir)
            except RuntimeError as exc:
                if "encrypted" in str(exc).lower() or "password" in str(exc).lower():
                    raise ClientRequestError("Import ZIP could not be read because it is password protected. Use an ingest export ZIP, not the protected Windows app package.") from exc
                raise
            except zipfile.BadZipFile as exc:
                raise ClientRequestError("Import file could not be read as a ZIP. Use a ZIP created by Export ingest folder.") from exc
            set_upload_progress({"active": True, "phase": "Locating index", "done": 2, "percent": 40})
            import_root = find_import_root(temp_dir)
            if not (import_root / "index.json").exists():
                raise ClientRequestError(f"{name} does not contain an index.json file. Import expects a ZIP created by Export ingest folder, not the Windows app ZIP.")
            preserve_config = target_root / "config.json"
            set_upload_progress({"active": True, "phase": "Replacing storage data", "done": 3, "percent": 60})
            for item in list(target_root.iterdir()):
                if item.name in {"_import.zip", "_import_work"}:
                    continue
                if item == preserve_config:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in import_root.iterdir():
                destination = target_root / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
            set_upload_progress({"active": True, "phase": "Loading imported index", "done": 4, "percent": 80})
            with STATE.lock:
                STATE.store = RAGStore()
        except Exception as exc:
            set_upload_progress(
                {
                    "active": False,
                    "phase": "Import failed",
                    "current": str(exc),
                    "done": 0,
                    "total": 5,
                    "percent": 0,
                    "ok": 0,
                    "failed": 1,
                }
            )
            raise
        finally:
            if temp_zip.exists():
                temp_zip.unlink()
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        set_upload_progress(
            {
                "active": False,
                "phase": "Import completed",
                "current": name,
                "done": 5,
                "total": 5,
                "percent": 100,
                "ok": 1,
                "failed": 0,
            }
        )
        return self._send_json({"ok": True, "message": "Index imported.", "status": status_payload()})

    def _handle_rebuild_vector(self) -> None:
        def vector_progress(done: int, total: int, phase: str) -> None:
            set_upload_progress(
                {
                    "active": done < total,
                    "phase": phase,
                    "current": "ChromaDB vector index",
                    "done": done,
                    "total": total,
                    "percent": progress_percent(done, total),
                    "ok": done,
                    "failed": 0,
                }
            )

        with STATE.lock:
            total = len(STATE.store.chunks)
            set_upload_progress(
                {
                    "active": True,
                    "phase": "Starting vector rebuild",
                    "current": "ChromaDB vector index",
                    "done": 0,
                    "total": total,
                    "percent": 0,
                    "ok": 0,
                    "failed": 0,
                }
            )
            rebuild_vector_index(STATE.store, progress_callback=vector_progress)
            vector = vector_status(STATE.store)
        vector_count = int(vector.get("count") or 0)
        if not vector.get("error") and total and vector_count < total:
            vector["error"] = f"Only {vector_count} of {total} chunks were written to ChromaDB."
            STATE.store.vector_error = str(vector["error"])
        if vector.get("error"):
            set_upload_progress(
                {
                    "active": False,
                    "phase": "Vector rebuild failed",
                    "current": str(vector.get("error") or ""),
                    "done": vector_count,
                    "total": total,
                    "percent": progress_percent(vector_count, total),
                    "ok": vector_count,
                    "failed": max(0, total - vector_count),
                }
            )
            return self._send_json({"ok": False, "error": f"Vector rebuild failed: {vector.get('error')}", "status": status_payload()}, status=500)
        set_upload_progress(
            {
                "active": False,
                "phase": "Vector rebuild completed",
                "current": "ChromaDB vector index",
                "done": vector_count,
                "total": total,
                "percent": 100 if total == 0 or vector_count >= total else progress_percent(vector_count, total),
                "ok": vector_count,
                "failed": max(0, total - vector_count),
            }
        )
        return self._send_json({"ok": True, "message": "Vector index rebuilt.", "status": status_payload()})

    def _handle_query(self) -> None:
        payload = self._read_json()
        question = str(payload.get("question") or "").strip()
        if not question:
            return self._send_json({"ok": False, "error": "Question is required"}, status=400)
        limit = int(payload.get("limit") or 6)
        retrieval_mode = str(payload.get("retrieval_mode") or "hybrid")
        engine = str(payload.get("engine") or "").strip().lower()
        use_ollama = bool(payload.get("use_ollama"))
        ollama_model = str(payload.get("model") or payload.get("ollama_model") or "llama3.2")
        hf_model = str(payload.get("hf_model") or "auto-best")
        hf_provider = str(payload.get("hf_provider") or "model")
        hf_token = str(payload.get("hf_token") or "")
        openrouter_model = str(payload.get("openrouter_model") or "deepseek/deepseek-r1:free")
        openrouter_token = str(payload.get("openrouter_token") or "")
        include_web = bool(payload.get("include_web"))
        use_vector = bool(payload.get("use_vector", True))
        prefer_exact = bool(payload.get("prefer_exact", True))

        with STATE.lock:
            contexts = STATE.store.retrieve(
                question,
                limit=limit,
                mode=retrieval_mode,
                use_vector=use_vector,
                prefer_exact=prefer_exact,
            )
        web_results = web_search(question, contexts) if include_web else []

        try:
            if engine == "huggingface":
                hf_contexts = contexts + web_results_as_contexts(web_results)
                answer = huggingface_answer(question, hf_contexts, hf_model, hf_token, hf_provider)
                mode = "Hugging Face + RAG" + (" + web" if web_results else "")
            elif engine == "openrouter":
                openrouter_contexts = contexts + web_results_as_contexts(web_results)
                answer = openrouter_answer(question, openrouter_contexts, openrouter_model, openrouter_token)
                mode = "OpenRouter + RAG" + (" + web" if web_results else "")
            elif engine == "ollama" or use_ollama:
                answer = ollama_answer(question, contexts, ollama_model)
                mode = "Ollama + retrieved chunks" + (" + internet results" if include_web else "")
            elif include_web:
                answer = extractive_answer(question, contexts)
                mode = "Local retrieval + internet results"
            else:
                answer = extractive_answer(question, contexts)
                mode = "Local retrieval"
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            answer = extractive_answer(question, contexts) + f"\n\nGeneration fallback: {exc}"
            mode = "Local retrieval fallback"

        public = public_contexts(contexts)
        final_answer = add_reference_summary(answer, public)
        if include_web:
            final_answer = append_internet_results(final_answer, web_results)
        return self._send_json(
            {
                "ok": True,
                "answer": final_answer,
                "mode": mode,
                "contexts": public,
                "web_results": web_results,
            }
        )

    def _handle_settings(self) -> None:
        payload = self._read_json()
        config = load_app_config()
        if "hf_token" in payload:
            config["hf_token"] = str(payload.get("hf_token") or "").strip()
        if "openrouter_token" in payload:
            config["openrouter_token"] = str(payload.get("openrouter_token") or "").strip()
        if "storage_dir" in payload:
            storage_dir = str(payload.get("storage_dir") or "").strip()
            if storage_dir:
                configure_storage(storage_dir)
                config["storage_dir"] = str(Path(storage_dir).expanduser().resolve())
                with STATE.lock:
                    STATE.store.close_vector_store()
                    STATE.store.close_search_cache()
                    STATE.store = RAGStore()
        save_app_config(config)
        return self._send_json(status_payload())

    def _handle_source(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        doc_id = (params.get("id") or [""])[0]
        record = find_doc(doc_id)
        if not record:
            self.send_error(404, "Document not found")
            return
        path = Path(str(record.get("path") or ""))
        if not path.exists():
            self.send_error(404, "Source file not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"inline; filename=\"{path.name}\"")
        self.end_headers()
        self.wfile.write(data)

    def _handle_chunk(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        chunk_id = (params.get("id") or [""])[0]
        chunk = find_chunk(chunk_id)
        if not chunk:
            self.send_error(404, "Chunk not found")
            return
        text = str(chunk.get("text") or "")
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_export(self) -> None:
        target_root = configure_storage()
        data_path = target_root / "_neo_rag_export.zip"
        if data_path.exists():
            data_path.unlink()
        files = [
            path
            for path in target_root.rglob("*")
            if path.is_file() and path != data_path and not path.name.startswith("_import")
        ]
        set_upload_progress(
            {
                "active": True,
                "phase": "Exporting ingest ZIP",
                "current": "",
                "done": 0,
                "total": len(files),
                "percent": 0,
                "ok": 0,
                "failed": 0,
            }
        )
        with zipfile.ZipFile(data_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, path in enumerate(files, start=1):
                archive.write(path, arcname=str(path.relative_to(target_root)))
                set_upload_progress(
                    {
                        "active": index < len(files),
                        "phase": "Exporting ingest ZIP",
                        "current": str(path.relative_to(target_root)),
                        "done": index,
                        "total": len(files),
                        "percent": progress_percent(index, len(files)),
                        "ok": index,
                        "failed": 0,
                    }
                )
        data = data_path.read_bytes()
        data_path.unlink(missing_ok=True)
        set_upload_progress(
            {
                "active": False,
                "phase": "Export completed",
                "current": "neo-rag-ingest-export.zip",
                "done": len(files),
                "total": len(files),
                "percent": 100 if files else 0,
                "ok": len(files),
                "failed": 0,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="neo-rag-ingest-export.zip"')
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def _read_multipart_upload(self, content_type: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + raw)
        fields: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        for part in message.iter_parts():
            disposition = part.get_content_disposition()
            if disposition != "form-data":
                continue
            name = part.get_param("name", header="content-disposition") or ""
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename:
                files.append({"name": filename, "data": data})
            elif name:
                charset = part.get_content_charset() or "utf-8"
                fields[name] = data.decode(charset, errors="ignore")
        if len(files) == 1 and fields.get("path"):
            files[0]["name"] = fields["path"]
        return fields, files

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, path: Path) -> None:
        if not path.exists():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def status_payload() -> dict[str, Any]:
    config = load_app_config()
    docs = []
    for record in STATE.store.documents:
        docs.append(
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "extension": record.get("extension"),
                "parser": record.get("parser"),
                "chunk_count": record.get("chunk_count"),
                "added_at": record.get("added_at"),
                "notes": record.get("notes"),
                "strategy": record.get("chunk_strategy"),
            }
        )
    return {
        "ok": True,
        "app_dir": str(APP_DIR),
        "storage_dir": str(config.get("storage_dir") or configure_storage()),
        "hf_token_saved": bool(config.get("hf_token")),
        "openrouter_token_saved": bool(config.get("openrouter_token")),
        "documents": docs,
        "document_count": len(STATE.store.documents),
        "chunk_count": len(STATE.store.chunks),
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "search_cache": search_cache_status(STATE.store),
        "vector_index": vector_status(STATE.store),
    }


def idle_upload_progress() -> dict[str, Any]:
    return {
        "active": False,
        "phase": "Idle",
        "current": "",
        "done": 0,
        "total": 0,
        "percent": 0,
        "ok": 0,
        "failed": 0,
    }


def progress_percent(done: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, round((done / total) * 100)))


def set_upload_progress(update: dict[str, Any]) -> None:
    with STATE.progress_lock:
        current = dict(STATE.upload_progress or idle_upload_progress())
        current.update(update)
        STATE.upload_progress = current


def bump_upload_progress(done: int, total: int, current: str, ok: bool, phase: str) -> None:
    with STATE.progress_lock:
        progress = dict(STATE.upload_progress or idle_upload_progress())
        progress.update(
            {
                "active": done < total,
                "phase": phase,
                "current": current,
                "done": done,
                "total": total,
                "percent": progress_percent(done, total),
                "ok": int(progress.get("ok") or 0) + (1 if ok else 0),
                "failed": int(progress.get("failed") or 0) + (0 if ok else 1),
            }
        )
        STATE.upload_progress = progress


def upload_progress_payload() -> dict[str, Any]:
    with STATE.progress_lock:
        progress = dict(STATE.upload_progress or idle_upload_progress())
    progress["ok"] = True
    return progress


def public_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in contexts:
        text = str(item.get("text") or "")
        doc = find_doc(str(item.get("doc_id") or ""))
        page = item.get("page") or page_from_text(text)
        source_url = f"/api/source?id={urllib.parse.quote(str(item.get('doc_id') or ''))}"
        if page and str((doc or {}).get("extension") or item.get("extension") or "").lower() == ".pdf":
            source_url += f"#page={page}"
        output.append(
            {
                "id": item.get("id"),
                "doc_id": item.get("doc_id"),
                "document": item.get("document"),
                "chunk": item.get("chunk"),
                "page": page,
                "source_url": source_url,
                "chunk_url": f"/api/chunk?id={urllib.parse.quote(str(item.get('id') or ''))}",
                "preview": text[:900],
            }
        )
    return output


def add_reference_summary(answer: str, contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return answer
    lines = ["\n\nReferences used:"]
    seen: set[str] = set()
    for item in contexts[:10]:
        key = str(item.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        page = f", page {item['page']}" if item.get("page") else ""
        lines.append(
            f"- {item.get('document', 'Unknown')} | chunk {item.get('chunk')}{page} | "
            f"source: {item.get('source_url')} | text: {item.get('chunk_url')}"
        )
    return answer.rstrip() + "\n".join(lines)


def append_internet_results(answer: str, web_results: list[dict[str, str]]) -> str:
    if not web_results:
        return answer.rstrip() + "\n\nInternet results:\nNo internet results were available for this query."
    lines = ["\n\nInternet results:"]
    for index, result in enumerate(web_results[:8], start=1):
        title = result.get("title") or "Untitled result"
        snippet = result.get("snippet") or ""
        url = result.get("url") or ""
        lines.append(f"{index}. {title}\n{snippet}\n{url}".strip())
    return answer.rstrip() + "\n".join(lines)


def upload_dir() -> Path:
    return configure_storage() / "web_uploads"


def find_doc(doc_id: str) -> dict[str, Any] | None:
    for record in STATE.store.documents:
        if str(record.get("id")) == str(doc_id):
            return record
    return None


def find_chunk(chunk_id: str) -> dict[str, Any] | None:
    for chunk in STATE.store.chunks:
        if str(chunk.get("id")) == str(chunk_id):
            return chunk
    return None


def page_from_text(text: str) -> int | None:
    matches = re.findall(r"##\s+Page\s+(\d+)", text, flags=re.IGNORECASE)
    if not matches:
        return None
    return int(matches[-1])


def web_search(question: str, contexts: list[dict[str, Any]]) -> list[dict[str, str]]:
    terms = " ".join(sorted({token for chunk in contexts[:3] for token in re.findall(r"[A-Za-z0-9_-]{4,}", str(chunk.get("document") or ""))}))
    query = f"{question} {terms}".strip()
    errors: list[str] = []
    for searcher in (duckduckgo_search, bing_search, google_search):
        try:
            results = searcher(query)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if results:
            return dedupe_web_results(results)[:8]
    if errors:
        return [
            {
                "title": "Internet search unavailable",
                "snippet": "Search providers could not return parsable results: " + " | ".join(errors[:2]),
                "url": "",
                "source": "web",
            }
        ]
    return []


def duckduckgo_search(query: str) -> list[dict[str, str]]:
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 RAG WebUI"})
    with urllib.request.urlopen(request, timeout=12) as response:
        status = getattr(response, "status", 200)
        body = response.read().decode("utf-8", errors="ignore")
    if status != 200 and "result__a" not in body:
        raise RuntimeError(f"DuckDuckGo returned HTTP {status} without search result markup")
    results: list[dict[str, str]] = []
    pattern = re.compile(r'<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>.*?<a class="result__snippet".*?>(.*?)</a>', re.DOTALL)
    for match in pattern.finditer(body):
        raw_url, title, snippet = match.groups()
        parsed = urllib.parse.urlparse(html.unescape(raw_url))
        params = urllib.parse.parse_qs(parsed.query)
        clean_url = params.get("uddg", [html.unescape(raw_url)])[0]
        results.append(
            {
                "title": clean_html(title),
                "snippet": clean_html(snippet),
                "url": clean_url,
                "source": "web",
            }
        )
        if len(results) >= 5:
            break
    return results


def bing_search(query: str) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read().decode("utf-8", errors="ignore")
    results: list[dict[str, str]] = []
    block_pattern = re.compile(r'<li class="b_algo"[^>]*>(.*?)</li>', re.DOTALL)
    for block in block_pattern.findall(body):
        title_match = re.search(r"<h2[^>]*>.*?<a[^>]+href=\"(.*?)\"[^>]*>(.*?)</a>.*?</h2>", block, re.DOTALL)
        if not title_match:
            continue
        raw_url, title = title_match.groups()
        snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = snippet_match.group(1) if snippet_match else ""
        clean_url = clean_bing_url(raw_url)
        if not clean_url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": clean_html(title),
                "snippet": clean_html(snippet),
                "url": clean_url,
                "source": "bing",
            }
        )
        if len(results) >= 8:
            break
    return results


def google_search(query: str) -> list[dict[str, str]]:
    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query, "hl": "en", "num": "8"})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read().decode("utf-8", errors="ignore")
    results: list[dict[str, str]] = []
    block_pattern = re.compile(r'<div class="g[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL)
    for block in block_pattern.findall(body):
        link_match = re.search(r'<a[^>]+href="(/url\?q=[^"]+|https?://[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', block, re.DOTALL)
        if not link_match:
            continue
        raw_url, title = link_match.groups()
        clean_url = clean_google_url(raw_url)
        if not clean_url.startswith(("http://", "https://")):
            continue
        snippet_match = re.search(r'<div[^>]+(?:data-sncf|class="VwiC3b)[^>]*>(.*?)</div>', block, re.DOTALL)
        snippet = snippet_match.group(1) if snippet_match else ""
        results.append(
            {
                "title": clean_html(title),
                "snippet": clean_html(snippet),
                "url": clean_url,
                "source": "google",
            }
        )
        if len(results) >= 8:
            break
    if results:
        return results
    simple_pattern = re.compile(r'<a[^>]+href="(/url\?q=[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>', re.DOTALL)
    for raw_url, title in simple_pattern.findall(body):
        clean_url = clean_google_url(raw_url)
        if clean_url.startswith(("http://", "https://")):
            results.append({"title": clean_html(title), "snippet": "", "url": clean_url, "source": "google"})
        if len(results) >= 8:
            break
    if not results and "enablejs" in body.lower():
        raise RuntimeError("Google returned a JavaScript-required page instead of result markup")
    return results


def clean_google_url(raw_url: str) -> str:
    clean_url = html.unescape(raw_url)
    if clean_url.startswith("/url?"):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(clean_url).query)
        return params.get("q", [clean_url])[0]
    return clean_url


def clean_bing_url(raw_url: str) -> str:
    clean_url = html.unescape(raw_url)
    parsed = urllib.parse.urlparse(clean_url)
    params = urllib.parse.parse_qs(parsed.query)
    encoded = params.get("u", [""])[0]
    if encoded:
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        try:
            padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            if decoded.startswith(("http://", "https://")):
                return decoded
        except Exception:
            pass
    return clean_url


def dedupe_web_results(results: list[dict[str, str]]) -> list[dict[str, str]]:
    output = []
    seen = set()
    for result in results:
        url = result.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        output.append(result)
    return output


def clean_html(value: str) -> str:
    value = re.sub(r"<.*?>", "", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def web_results_as_contexts(results: list[dict[str, str]]) -> list[dict[str, Any]]:
    contexts = []
    for index, result in enumerate(results, start=1):
        contexts.append(
            {
                "document": "Internet search",
                "chunk": index,
                "text": f"{result.get('title', '')}\n{result.get('snippet', '')}\n{result.get('url', '')}",
            }
        )
    return contexts


def safe_name(name: str) -> str:
    cleaned = "".join(char for char in name if char.isalnum() or char in " ._-()[]").strip()
    return cleaned or "upload.bin"


def safe_upload_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    parts = [safe_name(part) for part in normalized.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts) if parts else "upload.bin"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique upload path for {path.name}")


def find_import_root(temp_dir: Path) -> Path:
    if (temp_dir / "index.json").exists():
        return temp_dir
    candidates = [path for path in temp_dir.rglob("index.json") if path.is_file()]
    if not candidates:
        return temp_dir
    return candidates[0].parent


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Neo RAG-Anything running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
