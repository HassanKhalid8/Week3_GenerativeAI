/* Studio front-end.
   Drives the control panel, consumes the SSE pipeline trace, and renders assets. */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  config: null,
  ratio: "1:1",
  style: "none",
  provider: "auto",
  cards: new Map(),
  results: new Map(),
  eventSource: null,
};

/* ------------------------------------------------------------------ boot */

async function boot() {
  const response = await fetch("/api/config");
  state.config = await response.json();

  renderStyles();
  renderRatios();
  renderProviders();
  renderStages();
  wireControls();

  const active = state.config.providers.find((p) => p.name === state.config.active_provider);
  $("#engine-chip").textContent = `engine: ${active ? active.label : state.config.active_provider}`;
  updateLibraryChip(state.config.library);
  updatePromptLimit();
}

function updateLibraryChip(library) {
  if (!library) return;
  $("#library-chip").textContent = `library: ${library.assets} asset(s) / ${library.megabytes} MB`;
}

/* -------------------------------------------------------------- controls */

function renderStyles() {
  const host = $("#styles");
  host.innerHTML = "";
  state.config.styles.forEach((style) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "style-chip";
    button.textContent = style.label;
    button.title = style.positive || "No modifiers added";
    button.setAttribute("aria-pressed", String(style.key === state.style));
    button.addEventListener("click", () => {
      state.style = style.key;
      $$("#styles .style-chip").forEach((chip) =>
        chip.setAttribute("aria-pressed", String(chip === button))
      );
    });
    host.appendChild(button);
  });
}

function renderRatios() {
  const host = $("#ratios");
  host.innerHTML = "";
  state.config.ratios.forEach((ratio) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ratio-btn";
    button.setAttribute("aria-pressed", String(ratio.ratio === state.ratio));
    button.title = `${ratio.label} - ${ratio.width}x${ratio.height} - ${ratio.target}`;

    // Miniature of the actual canvas shape, scaled into a 26px box.
    const shape = document.createElement("div");
    shape.className = "ratio-shape";
    const scale = 24 / Math.max(ratio.width, ratio.height);
    shape.style.width = `${Math.max(6, ratio.width * scale)}px`;
    shape.style.height = `${Math.max(6, ratio.height * scale)}px`;

    const tag = document.createElement("span");
    tag.textContent = ratio.ratio;

    button.append(shape, tag);
    button.addEventListener("click", () => {
      state.ratio = ratio.ratio;
      $$("#ratios .ratio-btn").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      updateRatioDetail();
    });
    host.appendChild(button);
  });
  updateRatioDetail();
}

function updateRatioDetail() {
  const ratio = state.config.ratios.find((r) => r.ratio === state.ratio);
  $("#ratio-detail").textContent =
    `${ratio.width} x ${ratio.height} - ${ratio.pixels.toLocaleString()} px - ${ratio.target}`;
}

function renderProviders() {
  const select = $("#provider");
  select.innerHTML = "";

  const auto = document.createElement("option");
  auto.value = "auto";
  auto.textContent = "Auto - first available engine";
  select.appendChild(auto);

  state.config.providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.name;
    const badge = provider.available ? (provider.free ? "free" : "paid") : `needs ${provider.env_key}`;
    option.textContent = `${provider.label} - ${badge}`;
    option.disabled = !provider.available;
    select.appendChild(option);
  });

  select.value = "auto";
  select.addEventListener("change", () => {
    state.provider = select.value;
    updatePromptLimit();
  });
  updateProviderNote();
}

function activeProvider() {
  const name = state.provider === "auto" ? state.config.active_provider : state.provider;
  return state.config.providers.find((p) => p.name === name);
}

function updateProviderNote() {
  const provider = activeProvider();
  $("#provider-note").textContent = provider ? provider.notes : "";
}

function updatePromptLimit() {
  const provider = activeProvider();
  const limit = provider ? provider.max_prompt_chars : 4000;
  const length = $("#prompt").value.length;
  const counter = $("#prompt-count");
  counter.textContent = `${length} / ${limit}`;
  counter.style.color = length > limit ? "var(--bad)" : "";
  updateProviderNote();
}

function wireControls() {
  $("#prompt").addEventListener("input", updatePromptLimit);

  const bind = (id, target, format) => {
    const input = $(id);
    const out = $(target);
    const sync = () => (out.textContent = format(input.value));
    input.addEventListener("input", sync);
    sync();
  };
  bind("#count", "#count-value", (v) => v);
  bind("#connect-timeout", "#ct-value", (v) => `${Number(v).toFixed(2)}s`);
  bind("#read-timeout", "#rt-value", (v) => `${v}s`);
  bind("#max-retries", "#retry-value", (v) => v);
  bind("#qa-threshold", "#qa-value", (v) => Number(v).toFixed(1));

  $("#dice").addEventListener("click", () => {
    $("#seed").value = Math.floor(Math.random() * 2147483647);
  });

  $("#generate").addEventListener("click", generate);
  $("#prompt").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") generate();
  });

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      const showLibrary = tab.dataset.tab === "library";
      $("#tab-results").hidden = showLibrary;
      $("#tab-library").hidden = !showLibrary;
      if (showLibrary) loadLibrary();
    });
  });

  $("#inspector-close").addEventListener("click", closeInspector);
  $("#inspector").addEventListener("click", (event) => {
    if (event.target === $("#inspector")) closeInspector();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeInspector();
  });
}

/* --------------------------------------------------------------- stages */

function renderStages() {
  const host = $("#stages");
  host.innerHTML = "";
  state.config.stages.forEach((stage, index) => {
    const item = document.createElement("li");
    item.className = "stage";
    item.dataset.stage = stage.key;
    item.dataset.state = "idle";
    item.innerHTML = `
      <span class="stage-dot">${index + 1}</span>
      <div>
        <div class="stage-name">${stage.label}</div>
        <div class="stage-detail"></div>
      </div>`;
    host.appendChild(item);
  });
}

function setStage(key, stageState, detail) {
  const node = $(`.stage[data-stage="${key}"]`);
  if (!node) return;
  node.dataset.state = stageState;
  if (detail) node.querySelector(".stage-detail").textContent = detail;
}

function resetStages() {
  $$(".stage").forEach((node) => {
    node.dataset.state = "idle";
    node.querySelector(".stage-detail").textContent = "";
  });
}

/* ------------------------------------------------------------------ log */

function log(message, level = "") {
  const host = $("#log");
  if (host.querySelector(".muted")) host.innerHTML = "";
  const line = document.createElement("p");
  const time = new Date().toLocaleTimeString([], { hour12: false });
  line.innerHTML = `<span class="t">${time}</span><span class="${level}"></span>`;
  line.querySelector("span:last-child").textContent = message;
  host.appendChild(line);
  host.scrollTop = host.scrollHeight;
}

function clearLog() {
  $("#log").innerHTML = "";
}

/* ------------------------------------------------------------- generate */

async function generate() {
  const prompt = $("#prompt").value.trim();
  const errorBox = $("#form-error");
  errorBox.hidden = true;

  if (!prompt) {
    errorBox.textContent = "Enter a prompt first - the payload cannot be serialized without one.";
    errorBox.hidden = false;
    return;
  }

  const seedRaw = $("#seed").value.trim();
  const payload = {
    prompt,
    negative_prompt: $("#negative").value.trim(),
    aspect_ratio: state.ratio,
    style: state.style,
    count: Number($("#count").value),
    seed: seedRaw === "" ? null : Number(seedRaw),
    provider: state.provider,
    connect_timeout: Number($("#connect-timeout").value),
    read_timeout: Number($("#read-timeout").value),
    max_retries: Number($("#max-retries").value),
    qa_threshold: Number($("#qa-threshold").value),
    qa_discard: $("#qa-discard").checked,
  };

  setBusy(true);
  resetStages();
  clearLog();
  state.cards.clear();
  state.results.clear();
  $("#summary").hidden = true;
  $("#gallery").innerHTML = "";
  for (let i = 0; i < payload.count; i += 1) createCard(i);

  log(`dispatching ${payload.count} generation(s) at ${state.ratio}`, "info");

  let jobId;
  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    ({ job_id: jobId } = await response.json());
  } catch (error) {
    log(`dispatch failed: ${error}`, "bad");
    setBusy(false);
    return;
  }

  const source = new EventSource(`/api/jobs/${jobId}/events`);
  state.eventSource = source;

  source.addEventListener("stage", (event) => {
    const data = JSON.parse(event.data);
    setStage(data.stage, data.state, data.detail || "");
    if (data.detail) {
      const level = data.state === "failed" ? "bad" : data.state === "warn" ? "warn" : data.state === "done" ? "ok" : "";
      log(`${data.stage}: ${data.detail}`, level);
    }
    if (data.stage === "payload" && data.state === "done") {
      // Payload preview lands with the first asset event; keep the stage detail here.
    }
  });

  source.addEventListener("note", (event) => log(JSON.parse(event.data).text, "warn"));

  source.addEventListener("retry", (event) => {
    const data = JSON.parse(event.data);
    log(`retry ${data.attempt}: ${data.detail} - backing off ${(data.sleep_ms / 1000).toFixed(1)}s (jittered)`, "warn");
  });

  source.addEventListener("progress", (event) => {
    const data = JSON.parse(event.data);
    const card = state.cards.get(data.index);
    if (card) card.querySelector(".card-status").textContent = `${(data.bytes / 1024).toFixed(0)} KiB`;
  });

  source.addEventListener("asset_start", (event) => {
    const data = JSON.parse(event.data);
    log(`asset ${data.index + 1}/${data.total} - seed ${data.seed}`, "info");
    const card = state.cards.get(data.index);
    if (card) card.querySelector(".card-status").textContent = "streaming";
  });

  source.addEventListener("asset_done", (event) => {
    const data = JSON.parse(event.data);
    state.results.set(data.index, data.outcome);
    if (data.outcome.payload) {
      $("#payload-view").textContent = JSON.stringify(data.outcome.payload, null, 2);
    }
    renderCard(data.index, data.outcome);
  });

  source.addEventListener("error", (event) => {
    if (event.data) {
      const data = JSON.parse(event.data);
      log(`error: ${data.message}`, "bad");
    }
  });

  source.addEventListener("done", (event) => {
    const data = JSON.parse(event.data || "{}");
    source.close();
    state.eventSource = null;
    setBusy(false);
    if (data.error) {
      const box = $("#form-error");
      box.textContent = data.error;
      box.hidden = false;
      log(`batch halted: ${data.error}`, "bad");
      state.cards.forEach((card) => {
        if (card.classList.contains("pending")) card.remove();
      });
      if (!$("#gallery").children.length) $("#gallery").innerHTML = emptyState();
    }
    renderSummary(data);
    refreshLibraryChip();
  });
}

function setBusy(busy) {
  const button = $("#generate");
  button.disabled = busy;
  button.textContent = busy ? "Generating…" : "Generate";
}

/* ---------------------------------------------------------------- cards */

function emptyState() {
  return `<div class="empty">
    <div class="empty-frame"></div>
    <p>Nothing rendered.</p>
    <p class="muted">Adjust the prompt or the engine and try again.</p>
  </div>`;
}

function createCard(index) {
  const card = document.createElement("article");
  card.className = "card pending";
  card.innerHTML = `
    <div class="card-frame loading">
      <span class="card-status working">queued</span>
    </div>
    <div class="card-body">
      <div class="metrics"><span class="metric">waiting for the denoise loop…</span></div>
    </div>`;
  $("#gallery").appendChild(card);
  state.cards.set(index, card);
  return card;
}

function metric(label, value, tone = "") {
  return `<span class="metric ${tone}">${label} <b>${value}</b></span>`;
}

function renderCard(index, outcome) {
  const card = state.cards.get(index) || createCard(index);
  card.classList.remove("pending");

  const status = outcome.status;
  const frame = document.createElement("div");
  frame.className = "card-frame";

  if (outcome.url) {
    const img = document.createElement("img");
    img.src = outcome.url;
    img.alt = `Generated asset ${index + 1}`;
    img.loading = "lazy";
    img.addEventListener("click", () => openInspector(outcome));
    frame.appendChild(img);
    const ratio = outcome.integrity.width && outcome.integrity.height
      ? `${outcome.integrity.width} / ${outcome.integrity.height}`
      : "1 / 1";
    frame.style.aspectRatio = ratio;
  } else {
    frame.innerHTML = `<div class="empty-frame" style="margin:22px auto"></div>`;
  }

  const badge = document.createElement("span");
  badge.className = `card-status ${status}`;
  badge.textContent = status;
  frame.appendChild(badge);

  const body = document.createElement("div");
  body.className = "card-body";

  const metrics = [];
  if (outcome.qa && outcome.qa.aesthetic !== undefined) {
    const passed = outcome.qa.passed;
    metrics.push(metric("aesthetic", `${outcome.qa.aesthetic.toFixed(1)}/10`, passed ? "good" : "warn"));
    metrics.push(metric("align", outcome.qa.alignment.toFixed(2), outcome.qa.alignment >= 0.55 ? "good" : "warn"));
  }
  if (outcome.stream && outcome.stream.bytes_written) {
    metrics.push(metric("size", `${(outcome.stream.bytes_written / 1024).toFixed(0)} KiB`));
    metrics.push(metric("chunks", outcome.stream.chunks));
  }
  if (outcome.integrity && outcome.integrity.width) {
    metrics.push(metric("px", `${outcome.integrity.width}×${outcome.integrity.height}`,
      outcome.integrity.dimension_match ? "" : "warn"));
  }
  metrics.push(metric("seed", outcome.seed));
  metrics.push(metric("t", `${(outcome.elapsed_ms / 1000).toFixed(1)}s`));

  body.innerHTML = `<div class="metrics">${metrics.join("")}</div>`;

  if (outcome.error) {
    const error = document.createElement("div");
    error.className = "card-error";
    error.textContent = `${outcome.error_code}: ${outcome.error}`;
    body.appendChild(error);
  }

  if (outcome.url) {
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.innerHTML = `
      <a href="${outcome.url}/download" download>Download</a>
      <a href="${outcome.url}" target="_blank" rel="noopener">Full res</a>`;
    const details = document.createElement("button");
    details.type = "button";
    details.textContent = "Details";
    details.addEventListener("click", () => openInspector(outcome));
    actions.appendChild(details);
    body.appendChild(actions);
  }

  card.innerHTML = "";
  card.append(frame, body);
}

function renderSummary(batch) {
  if (!batch || !batch.counts) return;
  const host = $("#summary");
  const counts = batch.counts;
  const parts = [
    `<span>engine: ${batch.provider_label || batch.provider || "-"}</span>`,
    `<span>total: ${(batch.elapsed_ms / 1000).toFixed(1)}s</span>`,
  ];
  if (counts.accepted) parts.push(`<span class="good">accepted ${counts.accepted}</span>`);
  if (counts.flagged) parts.push(`<span class="flag">flagged ${counts.flagged}</span>`);
  if (counts.rejected) parts.push(`<span class="fail">rejected ${counts.rejected}</span>`);
  if (counts.failed) parts.push(`<span class="fail">failed ${counts.failed}</span>`);
  host.innerHTML = parts.join("");
  host.hidden = false;
}

/* ------------------------------------------------------------ inspector */

function openInspector(outcome) {
  const image = $("#inspector-image");
  image.src = outcome.url || "";
  image.alt = "Generated asset";

  const q = outcome.qa || {};
  const i = outcome.integrity || {};
  const s = outcome.stream || {};
  const attempts = (outcome.transport && outcome.transport.attempts) || [];

  const rows = (pairs) =>
    `<dl>${pairs.map(([k, v]) => `<dt>${k}</dt><dd>${v ?? "-"}</dd>`).join("")}</dl>`;

  $("#inspector-meta").innerHTML = `
    <h4>Payload</h4>
    ${rows([
      ["status", outcome.status],
      ["seed", outcome.seed],
      ["engine", (outcome.meta && outcome.meta.engine_model) || "-"],
      ["endpoint", (outcome.meta && outcome.meta.endpoint) || "-"],
      ["elapsed", `${(outcome.elapsed_ms / 1000).toFixed(2)}s`],
    ])}

    <h4>Transport</h4>
    ${rows([
      ["bytes", (s.bytes_written || 0).toLocaleString()],
      ["chunks", `${s.chunks || 0} × 64 KiB`],
      ["content-type", s.content_type || "-"],
      ["attempts", attempts.length],
      ["gateway", `${(outcome.transport && outcome.transport.total_ms) || 0} ms`],
    ])}
    ${attempts.length ? `<ul>${attempts.map((a) =>
      `<li>#${a.number} ${a.outcome} - ${a.detail} (${a.elapsed_ms} ms${a.sleep_ms ? `, slept ${a.sleep_ms} ms` : ""})</li>`
    ).join("")}</ul>` : ""}

    <h4>Integrity</h4>
    ${rows([
      ["verdict", i.code || "-"],
      ["format", i.fmt || "-"],
      ["decoded", i.width ? `${i.width} × ${i.height}` : "-"],
      ["dims match", i.dimension_match === false ? "no" : "yes"],
      ["sha256", i.sha256 ? `${i.sha256.slice(0, 24)}…` : "-"],
    ])}

    <h4>Quality assurance</h4>
    ${rows([
      ["scorer", q.scorer || "-"],
      ["aesthetic", q.aesthetic !== undefined ? `${q.aesthetic.toFixed(2)} / 10 (gate ${q.threshold})` : "-"],
      ["alignment", q.alignment !== undefined ? q.alignment.toFixed(3) : "-"],
      ["passed", q.passed === undefined ? "-" : q.passed ? "yes" : "no"],
    ])}
    ${q.notes ? `<ul>${q.notes.map((n) => `<li>${n}</li>`).join("")}</ul>` : ""}
    ${q.stats ? `<h4>Measured statistics</h4>${rows(Object.entries(q.stats).map(([k, v]) =>
      [k, Array.isArray(v) ? v.join(", ") : v]))}` : ""}
  `;

  $("#inspector").hidden = false;
}

function closeInspector() {
  $("#inspector").hidden = true;
  $("#inspector-image").src = "";
}

/* --------------------------------------------------------------- library */

async function loadLibrary() {
  const host = $("#library");
  host.innerHTML = `<p class="muted">Reading manifest…</p>`;
  const response = await fetch("/api/history");
  const data = await response.json();
  updateLibraryChip(data.library);

  if (!data.entries.length) {
    host.innerHTML = `<p class="muted">The manifest is empty. Generated assets are recorded in assets/manifest.jsonl.</p>`;
    return;
  }

  host.innerHTML = "";
  data.entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "lib-row";

    const thumb = entry.filename
      ? `<img src="/assets/${entry.filename}" alt="" loading="lazy" />`
      : `<div class="lib-thumb-missing">✕</div>`;

    const score = entry.qa_aesthetic !== null && entry.qa_aesthetic !== undefined
      ? `${Number(entry.qa_aesthetic).toFixed(1)}/10`
      : entry.error_code || entry.status;

    row.innerHTML = `
      ${thumb}
      <div>
        <div class="lib-prompt" title="${escapeHtml(entry.prompt || "")}">${escapeHtml(entry.prompt || "(no prompt)")}</div>
        <div class="lib-meta">${entry.timestamp || ""} · ${entry.provider || "-"} · ${entry.width}×${entry.height} · ${entry.style || "none"} · seed ${entry.seed}</div>
      </div>
      <div class="lib-score">${escapeHtml(String(score))}<br /><span class="muted">${entry.status}</span></div>`;

    if (entry.filename) {
      row.querySelector("img").addEventListener("click", () =>
        window.open(`/assets/${entry.filename}`, "_blank", "noopener")
      );
    }
    host.appendChild(row);
  });
}

async function refreshLibraryChip() {
  try {
    const response = await fetch("/api/history");
    const data = await response.json();
    updateLibraryChip(data.library);
  } catch {
    /* non-critical */
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

boot();
