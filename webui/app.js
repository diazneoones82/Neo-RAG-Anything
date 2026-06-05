const docCount = document.querySelector("#docCount");
const chunkCount = document.querySelector("#chunkCount");
const pageRefreshBtn = document.querySelector("#pageRefreshBtn");
const docList = document.querySelector("#docList");
const fileInput = document.querySelector("#fileInput");
const folderInput = document.querySelector("#folderInput");
const dropzone = document.querySelector("#dropzone");
const uploadState = document.querySelector("#uploadState");
const ingestProgress = document.querySelector("#ingestProgress");
const progressLabel = document.querySelector("#progressLabel");
const progressCount = document.querySelector("#progressCount");
const progressDetail = document.querySelector("#progressDetail");
const storageDir = document.querySelector("#storageDir");
const saveStorageBtn = document.querySelector("#saveStorageBtn");
const exportIndexBtn = document.querySelector("#exportIndexBtn");
const importIndexInput = document.querySelector("#importIndexInput");
const rebuildVectorBtn = document.querySelector("#rebuildVectorBtn");
const strategy = document.querySelector("#strategy");
const retrievalMode = document.querySelector("#retrievalMode");
const chunkSize = document.querySelector("#chunkSize");
const overlap = document.querySelector("#overlap");
const askBtn = document.querySelector("#askBtn");
const question = document.querySelector("#question");
const engine = document.querySelector("#engine");
const ollamaModel = document.querySelector("#ollamaModel");
const hfProvider = document.querySelector("#hfProvider");
const hfPreset = document.querySelector("#hfPreset");
const hfModel = document.querySelector("#hfModel");
const hfToken = document.querySelector("#hfToken");
const saveTokenBtn = document.querySelector("#saveTokenBtn");
const openrouterModel = document.querySelector("#openrouterModel");
const openrouterPreset = document.querySelector("#openrouterPreset");
const openrouterToken = document.querySelector("#openrouterToken");
const saveOpenRouterBtn = document.querySelector("#saveOpenRouterBtn");
const limit = document.querySelector("#limit");
const useVector = document.querySelector("#useVector");
const preferExact = document.querySelector("#preferExact");
const includeWeb = document.querySelector("#includeWeb");
const tokenStatus = document.querySelector("#tokenStatus");
const searchCacheStatus = document.querySelector("#searchCacheStatus");
const vectorStatus = document.querySelector("#vectorStatus");
const answerText = document.querySelector("#answerText");
const answerMode = document.querySelector("#answerMode");
const contexts = document.querySelector("#contexts");
const contextCount = document.querySelector("#contextCount");
const webResults = document.querySelector("#webResults");
const webCount = document.querySelector("#webCount");
const clearBtn = document.querySelector("#clearBtn");

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

async function refresh() {
  const payload = await api("/api/status");
  docCount.textContent = payload.document_count;
  chunkCount.textContent = payload.chunk_count;
  storageDir.value = payload.storage_dir || "";
  if (payload.hf_token_saved && !hfToken.value) {
    hfToken.placeholder = "Saved token will be used";
  }
  if (payload.openrouter_token_saved && !openrouterToken.value) {
    openrouterToken.placeholder = "Saved OpenRouter token will be used";
  }
  const saved = [];
  if (payload.hf_token_saved) saved.push("Hugging Face");
  if (payload.openrouter_token_saved) saved.push("OpenRouter");
  tokenStatus.textContent = saved.length
    ? `Saved tokens loaded already: ${saved.join(", ")}`
    : "Saved tokens loaded: none saved yet";
  const searchCache = payload.search_cache || {};
  searchCacheStatus.textContent = searchCache.enabled
    ? `Search cache: SQLite FTS active (${searchCache.count || 0} chunks)`
    : `Search cache: building on first query${searchCache.error ? ` (${searchCache.error})` : ""}`;
  const vector = payload.vector_index || {};
  vectorStatus.textContent = vector.enabled
    ? `Vector index: ChromaDB active (${vector.count || 0} vectors)`
    : `Vector index: local fallback${vector.error ? ` (${vector.error})` : ""}`;
  docList.innerHTML = "";
  if (!payload.documents.length) {
    docList.innerHTML = `<div class="doc-item"><span>No documents indexed yet.</span></div>`;
    return;
  }
  for (const doc of payload.documents) {
    const item = document.createElement("div");
    item.className = "doc-item";
    item.innerHTML = `
      <strong>${escapeHtml(doc.name)}</strong>
      <span>${escapeHtml(doc.parser || "parser")} · ${doc.chunk_count || 0} chunks · ${escapeHtml(doc.strategy || "")}</span>
      ${doc.notes ? `<span>${escapeHtml(doc.notes)}</span>` : ""}
    `;
    docList.appendChild(item);
  }
}

async function uploadFiles(files) {
  if (!files.length) return;
  uploadState.textContent = `Selected ${files.length}`;
  updateIngestProgress({
    phase: "Preparing files",
    current: "",
    done: 0,
    total: files.length,
    percent: 0,
    ok: 0,
    failed: 0,
  });
  uploadState.textContent = "Indexing";
  let progressTimer = files.length === 1 ? window.setInterval(() => pollUploadProgress().catch(() => {}), 650) : 0;
  const results = [];
  try {
    for (const [index, file] of files.entries()) {
      const name = file.webkitRelativePath || file.name;
      updateIngestProgress({
        phase: "Uploading",
        current: name,
        done: index,
        total: files.length,
        percent: Math.round((index / files.length) * 100),
        ok: results.filter((item) => item.ok).length,
        failed: results.filter((item) => !item.ok).length,
      });
      const payload = await uploadOneFile(file, name);
      results.push(...(payload.results || []));
      updateIngestProgress({
        phase: "Indexed",
        current: name,
        done: index + 1,
        total: files.length,
        percent: Math.round(((index + 1) / files.length) * 100),
        ok: results.filter((item) => item.ok).length,
        failed: results.filter((item) => !item.ok).length,
      });
    }
    const failed = results.filter((item) => !item.ok);
    uploadState.textContent = failed.length ? `${failed.length} skipped` : "Ready";
    updateIngestProgress({
      phase: failed.length ? "Completed with warnings" : "Completed",
      current: "",
      done: files.length,
      total: files.length,
      percent: 100,
      ok: files.length - failed.length,
      failed: failed.length,
    });
    if (failed.length) {
      answerMode.textContent = "Upload warnings";
      answerText.textContent = failed.map((item) => `${item.name}: ${item.error}`).join("\n");
    } else {
      answerMode.textContent = "Ingest complete";
      answerText.textContent = `Indexed ${results.length} file${results.length === 1 ? "" : "s"}.`;
    }
    await refresh();
  } finally {
    if (progressTimer) window.clearInterval(progressTimer);
    fileInput.value = "";
    folderInput.value = "";
  }
}

async function uploadOneFile(file, name) {
  const form = new FormData();
  form.append("strategy", strategy.value);
  form.append("chunk_size", String(Number(chunkSize.value)));
  form.append("overlap", String(Number(overlap.value)));
  form.append("path", name);
  form.append("files", file, name);
  const response = await fetch("/api/upload", {
    method: "POST",
    body: form,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Upload failed: ${response.status}`);
  }
  return payload;
}

async function pollUploadProgress() {
  const payload = await api("/api/upload-progress");
  updateIngestProgress(payload);
}

function updateIngestProgress(progress) {
  const total = Number(progress.total || 0);
  const done = Number(progress.done || 0);
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  ingestProgress.value = percent;
  progressCount.textContent = `${percent}%`;
  progressLabel.textContent = progress.phase || "Idle";
  const current = progress.current ? `: ${progress.current}` : "";
  const outcome = progress.ok || progress.failed ? ` (${progress.ok || 0} ok, ${progress.failed || 0} skipped)` : "";
  progressDetail.textContent = total ? `${done}/${total}${current}${outcome}` : "No ingest running.";
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function ask() {
  const text = question.value.trim();
  if (!text) return;
  askBtn.disabled = true;
  answerMode.textContent = "Retrieving";
  answerText.textContent = "Finding relevant chunks...";
  contexts.innerHTML = "";
  webResults.innerHTML = "";
  contextCount.textContent = "0";
  webCount.textContent = "0";
  try {
    const payload = await api("/api/query", {
      method: "POST",
      body: JSON.stringify({
        question: text,
        retrieval_mode: retrievalMode.value,
        engine: engine.value,
        ollama_model: ollamaModel.value,
        hf_provider: hfProvider.value,
        hf_model: hfModel.value,
        hf_token: hfToken.value,
        openrouter_model: openrouterModel.value,
        openrouter_token: openrouterToken.value,
        use_vector: useVector.checked,
        prefer_exact: preferExact.checked,
        include_web: includeWeb.checked,
        limit: Number(limit.value),
      }),
    });
    answerMode.textContent = payload.mode;
    answerText.textContent = payload.answer;
    renderContexts(payload.contexts || []);
    renderWebResults(payload.web_results || []);
  } catch (error) {
    answerMode.textContent = "Error";
    answerText.textContent = error.message;
  } finally {
    askBtn.disabled = false;
  }
}

function renderContexts(items) {
  contextCount.textContent = String(items.length);
  contexts.innerHTML = "";
  if (!items.length) {
    contexts.innerHTML = `<div class="context-item"><p>No matching context found.</p></div>`;
    return;
  }
  for (const item of items) {
    const node = document.createElement("div");
    node.className = "context-item";
    const sourceLabel = item.page ? `PDF page ${item.page}` : "Open source";
    node.innerHTML = `
      <strong>${escapeHtml(item.document)} · chunk ${item.chunk}</strong>
      <div class="context-links">
        <a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(sourceLabel)}</a>
        <a href="${escapeHtml(item.chunk_url)}" target="_blank" rel="noreferrer">chunk text</a>
      </div>
      <p>${escapeHtml(item.preview)}</p>
    `;
    contexts.appendChild(node);
  }
}

function renderWebResults(items) {
  webCount.textContent = String(items.length);
  webResults.innerHTML = "";
  if (!items.length) {
    webResults.innerHTML = includeWeb.checked
      ? `<div class="context-item"><p>No internet results were returned. The answer is based on local RAG chunks only.</p></div>`
      : `<div class="context-item"><p>Internet search is off.</p></div>`;
    return;
  }
  for (const item of items) {
    const node = document.createElement("div");
    node.className = "context-item";
    node.innerHTML = `
      <strong>${escapeHtml(item.title || "Web result")}</strong>
      <span>${escapeHtml(item.source || "web")}</span>
      <div class="context-links"><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noreferrer">open result</a></div>
      <p>${escapeHtml(item.snippet || "")}</p>
    `;
    webResults.appendChild(node);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

fileInput.addEventListener("change", () => uploadFiles([...fileInput.files]).catch(showError));
folderInput.addEventListener("change", () => uploadFiles([...folderInput.files]).catch(showError));

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
});

dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragging"));

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
  uploadFiles([...event.dataTransfer.files]).catch(showError);
});

askBtn.addEventListener("click", () => ask());

question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ask();
  }
});

clearBtn.addEventListener("click", async () => {
  if (!confirm("Clear indexed documents and chunks?")) return;
  await api("/api/clear", { method: "POST", body: "{}" });
  await refresh();
});

hfPreset.addEventListener("change", () => {
  const [model, provider] = hfPreset.value.split("|");
  hfModel.value = model || "auto-best";
  hfProvider.value = provider || "model";
});

openrouterPreset.addEventListener("change", () => {
  openrouterModel.value = openrouterPreset.value || "openrouter/free";
});

saveTokenBtn.addEventListener("click", async () => {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ hf_token: hfToken.value }),
  });
  hfToken.value = "";
  hfToken.placeholder = "Saved token will be used";
  answerMode.textContent = "Settings";
  answerText.textContent = "Hugging Face token saved locally.";
  await refresh();
});

saveOpenRouterBtn.addEventListener("click", async () => {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ openrouter_token: openrouterToken.value }),
  });
  openrouterToken.value = "";
  openrouterToken.placeholder = "Saved OpenRouter token will be used";
  answerMode.textContent = "Settings";
  answerText.textContent = "OpenRouter token saved locally.";
  await refresh();
});

saveStorageBtn.addEventListener("click", async () => {
  if (!storageDir.value.trim()) return;
  const payload = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ storage_dir: storageDir.value.trim() }),
  });
  storageDir.value = payload.storage_dir || storageDir.value;
  answerMode.textContent = "Settings";
  answerText.textContent = `Storage folder set to:\n${storageDir.value}\n\nNew uploads and index data will use this folder.`;
  await refresh();
});

pageRefreshBtn.addEventListener("click", () => {
  window.location.reload();
});

exportIndexBtn.addEventListener("click", async () => {
  exportIndexBtn.disabled = true;
  answerMode.textContent = "Export";
  answerText.textContent = "Preparing ingest export ZIP...";
  updateIngestProgress({
    phase: "Preparing export",
    current: "",
    done: 0,
    total: 1,
    percent: 0,
    ok: 0,
    failed: 0,
  });
  const progressTimer = window.setInterval(() => pollUploadProgress().catch(() => {}), 650);
  try {
    const response = await fetch("/api/export");
    if (!response.ok) {
      throw new Error(`Export failed: ${response.status}`);
    }
    const blob = await response.blob();
    downloadBlob(blob, "neo-rag-ingest-export.zip");
    updateIngestProgress({
      phase: "Export completed",
      current: "neo-rag-ingest-export.zip",
      done: 1,
      total: 1,
      percent: 100,
      ok: 1,
      failed: 0,
    });
    answerText.textContent = "Ingest export ZIP is ready.";
  } catch (error) {
    showError(error);
  } finally {
    window.clearInterval(progressTimer);
    exportIndexBtn.disabled = false;
  }
});

importIndexInput.addEventListener("change", async () => {
  const file = importIndexInput.files[0];
  if (!file) return;
  if (!confirm("Import this ingest ZIP and replace the current index in the active storage folder?")) {
    importIndexInput.value = "";
    return;
  }
  answerMode.textContent = "Import";
  answerText.textContent = "Reading ingest ZIP...";
  updateIngestProgress({
    phase: "Reading import ZIP",
    current: file.name,
    done: 0,
    total: 5,
    percent: 0,
    ok: 0,
    failed: 0,
  });
  const progressTimer = window.setInterval(() => pollUploadProgress().catch(() => {}), 650);
  try {
    const form = new FormData();
    form.append("name", file.name);
    form.append("files", file, file.name);
    const response = await fetch("/api/import", {
      method: "POST",
      body: form,
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Import failed: ${response.status}`);
    }
    answerText.textContent = payload.message || "Index imported.";
    await refresh();
  } catch (error) {
    showError(error);
  } finally {
    window.clearInterval(progressTimer);
    importIndexInput.value = "";
  }
});

rebuildVectorBtn.addEventListener("click", async () => {
  rebuildVectorBtn.disabled = true;
  uploadState.textContent = "Vector rebuild";
  answerMode.textContent = "Vector index";
  answerText.textContent = "Rebuilding ChromaDB vectors for existing chunks...";
  updateIngestProgress({
    phase: "Starting vector rebuild",
    current: "ChromaDB vector index",
    done: 0,
    total: 1,
    percent: 0,
    ok: 0,
    failed: 0,
  });
  const progressTimer = window.setInterval(() => pollUploadProgress().catch(() => {}), 650);
  try {
    const payload = await api("/api/rebuild-vector", { method: "POST", body: "{}" });
    const vector = payload.status?.vector_index || {};
    answerText.textContent = `Vector index rebuilt.\nPath: ${vector.path || ""}\nVectors: ${vector.count || 0}${vector.error ? `\nError: ${vector.error}` : ""}`;
    await refresh();
  } catch (error) {
    showError(error);
  } finally {
    window.clearInterval(progressTimer);
    rebuildVectorBtn.disabled = false;
  }
});

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function showError(error) {
  uploadState.textContent = "Error";
  answerMode.textContent = "Error";
  answerText.textContent = error.message;
}

refresh().catch(showError);
