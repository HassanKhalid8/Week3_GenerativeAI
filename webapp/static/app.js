/* Studio front-end.
   Drives the control panel, consumes the pipeline trace, and renders assets.

   Two transports feed one renderer: Server-Sent Events locally, and a single
   POST whose collected event log is replayed on serverless hosts, where the
   job that produced the events no longer exists by the time a second request
   arrives. Both funnel through handleEvent(). */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const KEY_STORE = "emulsion.api-keys.v1";
const KEY_STORE_LEGACY = "lumenforge.api-keys.v1";

/* A rename must not quietly sign people out of their own engines: keys saved
   under the previous product name move across once, then the old entry goes. */
(function migrateKeyStore() {
  try {
    const legacy = localStorage.getItem(KEY_STORE_LEGACY);
    if (!legacy) return;
    if (!localStorage.getItem(KEY_STORE)) localStorage.setItem(KEY_STORE, legacy);
    localStorage.removeItem(KEY_STORE_LEGACY);
  } catch {
    /* storage unavailable - nothing saved, nothing to migrate */
  }
})();

const state = {
  config: null,
  ratio: "1:1",
  style: "none",
  provider: "auto",
  cards: new Map(),
  results: new Map(),
  eventSource: null,
  sessionLibrary: [],
  lastRequest: null,
  combo: { open: false, index: 0, options: [], typed: "", typedAt: 0 },
};

/* ------------------------------------------------------------------ urls */

/* Every server URL is built here, because the studio does not always own its
   own address space. On a serverless host the whole site is funnelled into one
   function by a rewrite rule, and the page it served says - in
   window.__STUDIO_ROUTING__ - whether that rewrite can carry a path at all.
   When it cannot, the route travels as a query parameter to the function's own
   URL, which no rewrite has to touch. Locally this is just the identity. */

function appUrl(path, params) {
  const routing = window.__STUDIO_ROUTING__ || { mode: "path" };
  const query = new URLSearchParams(params || {});
  if (routing.mode !== "query") {
    const rest = query.toString();
    return rest ? `${path}?${rest}` : path;
  }
  query.set(routing.param || "__vpath", path.replace(/^\//, ""));
  return `${routing.base || "/api/index"}?${query}`;
}

/* The JSON API lives under /studio, not /api: Vercel reserves /api for its own
   function files and 404s anything else there before our rewrite is reached. */
const api = (route, params) => appUrl(`/studio/${route}`, params);
const assetUrl = (filename) => appUrl(`/assets/${filename}`);

/* ------------------------------------------------------------------ boot */

async function boot() {
  let response;
  try {
    response = await fetch(api("config"));
    state.config = await response.json();
  } catch (error) {
    // A misrouted deploy answers with the HTML shell instead, so the parse
    // fails and every control below would silently never render. Say so.
    bootFailed(response, error);
    return;
  }

  renderStyles();
  renderRatios();
  renderStages();
  wireControls();
  wireCombo();
  wireVault();

  await refreshEngines();
  updateLibraryChip(state.config.library);
  updatePromptLimit();

  const count = $("#count");
  count.max = String(state.config.max_count || 4);
  if (Number(count.value) > Number(count.max)) count.value = count.max;
  $("#count-value").textContent = count.value;
}

function bootFailed(response, error) {
  const status = response ? `${response.status} ${response.statusText}` : "no response";
  const box = $("#form-error");
  box.textContent =
    `The studio could not load its configuration from ${api("config")} (${status}). ` +
    `The server is reachable but that route is not returning JSON, which points at ` +
    `the deployment's rewrite rules rather than the app itself. Check ${api("health")}.`;
  box.hidden = false;
  const engine = $("#engine-chip");
  engine.textContent = "Engine unavailable";
  engine.classList.add("unavailable");
  $("#library-chip").textContent = "library: unavailable";
  $("#generate").disabled = true;
  console.error("boot failed:", error);
}

function updateLibraryChip(library) {
  if (!library) return;
  const suffix = library.ephemeral ? " · ephemeral" : "";
  $("#library-chip").textContent =
    `library: ${library.assets} asset(s) / ${library.megabytes} MB${suffix}`;
}

/* ------------------------------------------------------------- key store */
/* Keys live in this browser and nowhere else. They are attached to a generate
   request only when that request actually needs them, and the server drops
   them the moment it is done. */

function loadKeys() {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY_STORE) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveKey(engine, value) {
  const keys = loadKeys();
  keys[engine] = value;
  localStorage.setItem(KEY_STORE, JSON.stringify(keys));
}

function removeKey(engine) {
  const keys = loadKeys();
  delete keys[engine];
  localStorage.setItem(KEY_STORE, JSON.stringify(keys));
}

function maskKey(value) {
  return value.length >= 8 ? `••••${value.slice(-4)}` : "••••";
}

/** Only the keys this request could possibly need. "Auto" has to consider them
    all because the server picks the engine; a named engine needs exactly one. */
function keysForRequest() {
  const stored = loadKeys();
  if (state.provider === "auto") return stored;
  return stored[state.provider] ? { [state.provider]: stored[state.provider] } : {};
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

/* ------------------------------------------------------- engine combobox */

/** Re-ask the server which engines are usable, with this browser's keys applied.
    The server knows about its own env vars too, so it is the honest authority. */
async function refreshEngines() {
  try {
    const response = await fetch(api("engines"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_keys: loadKeys() }),
    });
    const data = await response.json();
    state.config.providers = data.providers;
    state.config.active_provider = data.active_provider;
  } catch {
    /* keep whatever the config call gave us */
  }
  const active = state.config.providers.find((p) => p.name === state.config.active_provider);
  // The resolved engine belongs next to the picker, where the choice is made.
  $("#engine-chip").textContent = `Ready · ${active ? active.label : state.config.active_provider}`;

  // A saved key may have been removed while its engine was selected.
  if (state.provider !== "auto") {
    const chosen = state.config.providers.find((p) => p.name === state.provider);
    if (!chosen || !chosen.available) state.provider = "auto";
  }
  renderCombo();
  updatePromptLimit();
}

function comboOptions() {
  const auto = state.config.providers.find((p) => p.name === state.config.active_provider);
  const options = [
    {
      value: "auto",
      name: "Auto",
      badge: "auto",
      badgeClass: "",
      note: auto ? `Picks the first usable engine — currently ${auto.label}.` : "",
      locked: false,
    },
  ];
  state.config.providers.forEach((provider) => {
    let badge = provider.free ? "free" : "paid";
    let badgeClass = provider.free ? "free" : "paid";
    let note = provider.notes;

    if (!provider.available) {
      badge = "needs key";
      badgeClass = "locked";
      note = `Requires ${provider.env_key}. Click to add your key.`;
    } else if (provider.key_source === "user") {
      badge = "key saved";
      badgeClass = "saved";
    } else if (provider.key_source === "env") {
      badge = "server key";
      badgeClass = "saved";
    }

    options.push({
      value: provider.name,
      name: provider.label,
      badge,
      badgeClass,
      note,
      locked: !provider.available,
    });
  });
  return options;
}

function renderCombo() {
  const list = $("#provider-list");
  const options = comboOptions();
  state.combo.options = options;

  list.innerHTML = "";
  options.forEach((option, index) => {
    const item = document.createElement("li");
    item.className = `combo-opt${option.locked ? " locked" : ""}`;
    item.id = `provider-opt-${index}`;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(option.value === state.provider));
    if (option.locked) item.setAttribute("aria-disabled", "true");

    const name = document.createElement("span");
    name.className = "combo-opt-name";
    name.textContent = option.locked ? `🔒 ${option.name}` : option.name;

    const badge = document.createElement("span");
    badge.className = `combo-badge ${option.badgeClass}`;
    badge.textContent = option.badge;

    const note = document.createElement("span");
    note.className = "combo-opt-note";
    note.textContent = option.note || "";

    item.append(name, badge, note);
    item.addEventListener("click", () => chooseOption(index));
    item.addEventListener("mousemove", () => setActiveOption(index));
    list.appendChild(item);
  });

  const selected = options.find((o) => o.value === state.provider) || options[0];
  $("#provider-value").textContent = selected.name;
  state.combo.index = Math.max(0, options.indexOf(selected));
  if (state.combo.open) setActiveOption(state.combo.index);
  updateProviderNote();
}

function setActiveOption(index) {
  const items = $$("#provider-list .combo-opt");
  if (!items.length) return;
  state.combo.index = (index + items.length) % items.length;
  items.forEach((item, i) => item.classList.toggle("active", i === state.combo.index));
  const active = items[state.combo.index];
  $("#provider-trigger").setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function openCombo() {
  if (state.combo.open) return;
  state.combo.open = true;
  $("#provider-list").hidden = false;
  $("#provider-trigger").setAttribute("aria-expanded", "true");
  setActiveOption(state.combo.index);
}

function closeCombo(refocus = true) {
  if (!state.combo.open) return;
  state.combo.open = false;
  $("#provider-list").hidden = true;
  const trigger = $("#provider-trigger");
  trigger.setAttribute("aria-expanded", "false");
  trigger.removeAttribute("aria-activedescendant");
  if (refocus) trigger.focus();
}

function chooseOption(index) {
  const option = state.combo.options[index];
  if (!option) return;
  if (option.locked) {
    // A locked engine is not a dead end - it is an invitation to add the key.
    closeCombo(false);
    openVault(option.value);
    return;
  }
  state.provider = option.value;
  closeCombo();
  renderCombo();
  updatePromptLimit();
}

function comboTypeahead(char) {
  const now = Date.now();
  state.combo.typed = now - state.combo.typedAt > 700 ? char : state.combo.typed + char;
  state.combo.typedAt = now;
  const match = state.combo.options.findIndex((o) =>
    o.name.toLowerCase().startsWith(state.combo.typed.toLowerCase())
  );
  if (match >= 0) setActiveOption(match);
}

function wireCombo() {
  const trigger = $("#provider-trigger");

  trigger.addEventListener("click", () => (state.combo.open ? closeCombo() : openCombo()));

  trigger.addEventListener("keydown", (event) => {
    const { key } = event;
    if (!state.combo.open) {
      if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
        event.preventDefault();
        openCombo();
      }
      return;
    }
    if (key === "ArrowDown") { event.preventDefault(); setActiveOption(state.combo.index + 1); }
    else if (key === "ArrowUp") { event.preventDefault(); setActiveOption(state.combo.index - 1); }
    else if (key === "Home") { event.preventDefault(); setActiveOption(0); }
    else if (key === "End") { event.preventDefault(); setActiveOption(state.combo.options.length - 1); }
    else if (key === "Enter" || key === " ") { event.preventDefault(); chooseOption(state.combo.index); }
    else if (key === "Escape") { event.preventDefault(); closeCombo(); }
    else if (key === "Tab") { closeCombo(false); }
    else if (key.length === 1 && /\S/.test(key)) { comboTypeahead(key); }
  });

  document.addEventListener("click", (event) => {
    if (state.combo.open && !$("#provider-combo").contains(event.target)) closeCombo(false);
  });
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
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  });

  $("#inspector-close").addEventListener("click", closeInspector);
  $("#inspector").addEventListener("click", (event) => {
    if (event.target === $("#inspector")) closeInspector();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("#keyvault").hidden) closeVault();
    else closeInspector();
  });
}

/** Switch the work surface. Panels declare which tab owns them, so adding one
    is a matter of markup rather than another branch here. */
function showTab(name) {
  $$(".tab").forEach((tab) => {
    const on = tab.dataset.tab === name;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", String(on));
  });
  $$("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  if (name === "library") loadLibrary();
}

/* --------------------------------------------------------------- stages */

/* The rail runs across the top, so it shows a short name and keeps the server's
   full label as the tooltip. Anything not listed falls back to that label. */
const STAGE_SHORT = {
  payload: "Payload",
  gate1: "Input filter",
  network: "Gateway",
  transport: "Transport",
  integrity: "Integrity",
  gate2: "Output filter",
  qa: "Quality",
};

function renderStages() {
  const host = $("#stages");
  host.innerHTML = "";
  state.config.stages.forEach((stage, index) => {
    const item = document.createElement("li");
    item.className = "stage";
    item.dataset.stage = stage.key;
    item.dataset.state = "idle";
    item.title = stage.label;
    item.innerHTML = `
      <span class="stage-dot">${index + 1}</span>
      <div>
        <div class="stage-name"></div>
        <div class="stage-detail"></div>
      </div>`;
    item.querySelector(".stage-name").textContent = STAGE_SHORT[stage.key] || stage.label;
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** One renderer for both transports. */
function handleEvent(kind, data) {
  switch (kind) {
    case "stage": {
      setStage(data.stage, data.state, data.detail || "");
      if (data.detail) {
        const level =
          data.state === "failed" ? "bad" :
          data.state === "warn" ? "warn" :
          data.state === "done" ? "ok" : "";
        log(`${data.stage}: ${data.detail}`, level);
      }
      break;
    }
    case "note":
      log(data.text, "warn");
      break;
    case "retry":
      log(
        `retry ${data.attempt}: ${data.detail} - backing off ${(data.sleep_ms / 1000).toFixed(1)}s (jittered)`,
        "warn"
      );
      break;
    case "progress": {
      const card = state.cards.get(data.index);
      if (card) {
        const status = card.querySelector(".card-status");
        if (status) status.textContent = `${(data.bytes / 1024).toFixed(0)} KiB`;
      }
      break;
    }
    case "asset_start": {
      log(`asset ${data.index + 1}/${data.total} - seed ${data.seed}`, "info");
      const card = state.cards.get(data.index);
      if (card) {
        const status = card.querySelector(".card-status");
        if (status) status.textContent = "streaming";
      }
      break;
    }
    case "asset_done":
      state.results.set(data.index, data.outcome);
      if (data.outcome.payload) {
        $("#payload-view").textContent = JSON.stringify(data.outcome.payload, null, 2);
      }
      rememberInSession(data.outcome);
      renderCard(data.index, data.outcome);
      break;
    case "error":
      log(`error: ${data.message}`, "bad");
      break;
    case "done":
      finishBatch(data);
      break;
    default:
      break;
  }
}

function finishBatch(batch) {
  setBusy(false);
  if (batch && batch.error) {
    showFormError(batch.error, batch.needs_key);
    log(`batch halted: ${batch.error}`, "bad");
    state.cards.forEach((card) => {
      if (card.classList.contains("pending")) card.remove();
    });
    if (!$("#gallery").children.length) $("#gallery").innerHTML = emptyState();
  }
  renderSummary(batch);
  refreshLibraryChip();
}

function showFormError(message, needsKey) {
  const box = $("#form-error");
  box.textContent = message;
  if (needsKey) {
    const provider = state.config.providers.find((p) => p.name === needsKey);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "fix-btn";
    button.textContent = `Add key for ${provider ? provider.label : needsKey}`;
    button.addEventListener("click", () => openVault(needsKey));
    box.appendChild(document.createElement("br"));
    box.appendChild(button);
  }
  box.hidden = false;
}

async function generate() {
  const prompt = $("#prompt").value.trim();
  const errorBox = $("#form-error");
  errorBox.hidden = true;
  errorBox.textContent = "";

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
    api_keys: keysForRequest(),
  };
  state.lastRequest = payload;

  setBusy(true);
  resetStages();
  clearLog();
  state.cards.clear();
  state.results.clear();
  $("#summary").hidden = true;
  $("#gallery").innerHTML = "";
  for (let i = 0; i < payload.count; i += 1) createCard(i);

  log(`dispatching ${payload.count} generation(s) at ${state.ratio}`, "info");

  if (state.config.streaming === false) {
    await generateSync(payload);
  } else {
    await generateStreaming(payload);
  }
}

/** Local transport: the pipeline narrates itself over SSE while it runs. */
async function generateStreaming(payload) {
  let jobId;
  try {
    const response = await fetch(api("generate"), {
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

  const source = new EventSource(api(`jobs/${jobId}/events`));
  state.eventSource = source;

  const kinds = ["stage", "note", "retry", "progress", "asset_start", "asset_done"];
  kinds.forEach((kind) => {
    source.addEventListener(kind, (event) => handleEvent(kind, JSON.parse(event.data)));
  });

  source.addEventListener("error", (event) => {
    // EventSource also fires "error" for transport hiccups, which carry no data.
    if (event.data) handleEvent("error", JSON.parse(event.data));
  });

  source.addEventListener("done", (event) => {
    source.close();
    state.eventSource = null;
    handleEvent("done", JSON.parse(event.data || "{}"));
  });
}

/** Serverless transport: one request runs the batch, then the log is replayed. */
async function generateSync(payload) {
  let data;
  try {
    const response = await fetch(api("generate/sync"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`server responded ${response.status}`);
    data = await response.json();
  } catch (error) {
    log(`dispatch failed: ${error}`, "bad");
    showFormError(`The generation request failed: ${error}`);
    setBusy(false);
    return;
  }

  for (const event of data.events || []) {
    handleEvent(event.kind, event.data);
    // A short beat between stages so the pipeline still reads as a sequence.
    if (event.kind === "stage") await sleep(70);
  }
  handleEvent("done", data.result || {});
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

/** What the browser can render without asking the server again. The small
    inline preview is always present; the full-res copy is only inlined while
    the response body has room, so the stored asset URL is the fallback for it. */
function thumbSource(outcome) {
  return outcome.preview_url || outcome.data_url || outcome.url || "";
}

function fullSource(outcome) {
  return outcome.data_url || outcome.url || outcome.preview_url || "";
}

function renderCard(index, outcome) {
  const card = state.cards.get(index) || createCard(index);
  card.classList.remove("pending");

  const status = outcome.status;
  const src = thumbSource(outcome);
  const frame = document.createElement("div");
  frame.className = "card-frame";

  if (src) {
    const img = document.createElement("img");
    img.src = src;
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

    if (outcome.needs_key) {
      const fix = document.createElement("button");
      fix.type = "button";
      fix.className = "kv-btn";
      fix.textContent = "Add key";
      fix.addEventListener("click", () => openVault(outcome.needs_key));
      body.appendChild(fix);
    }
  }

  if (src) {
    const actions = document.createElement("div");
    actions.className = "card-actions";
    // data: URLs are self-contained - download/open work with no server round
    // trip, which matters on serverless deploys where disk storage is ephemeral.
    const full = fullSource(outcome);
    const downloadHref = outcome.data_url || (outcome.url ? `${outcome.url}/download` : full);
    actions.innerHTML = `
      <a href="${downloadHref}" download="${outcome.filename || "asset.jpg"}">Download</a>
      <a href="${full}" target="_blank" rel="noopener">Full res</a>`;
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
  image.src = fullSource(outcome);
  image.alt = "Generated asset";
  // The full-res copy may only exist on a server instance that is already gone.
  image.onerror = () => {
    if (outcome.preview_url && image.src !== outcome.preview_url) image.src = outcome.preview_url;
  };

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

/* -------------------------------------------------------------- key vault */

function renderVault(focusEngine = "") {
  const host = $("#kv-rows");
  const wasFocused = $("#keyvault").contains(document.activeElement);
  const stored = loadKeys();
  host.innerHTML = "";

  state.config.providers
    .filter((provider) => provider.needs_key)
    .forEach((provider) => {
      const saved = stored[provider.name] || "";
      const row = document.createElement("div");
      row.className = `kv-row${saved ? " saved" : ""}`;
      row.dataset.engine = provider.name;

      row.innerHTML = `
        <div class="kv-head">
          <span class="kv-name">${escapeHtml(provider.label)}</span>
          <span class="kv-env">${escapeHtml(provider.env_key)}</span>
        </div>
        <div class="kv-input-row">
          <input type="password" autocomplete="off" spellcheck="false"
                 placeholder="${saved ? escapeHtml(maskKey(saved)) : escapeHtml(provider.key_hint || "paste your key")}" />
          <button type="button" class="kv-btn" data-act="reveal" aria-label="Show key">👁</button>
        </div>
        <div class="kv-actions">
          <button type="button" class="kv-btn primary-btn" data-act="save">Save</button>
          <button type="button" class="kv-btn" data-act="test">Test</button>
          <button type="button" class="kv-btn danger" data-act="remove" ${saved ? "" : "disabled"}>Remove</button>
          ${provider.key_url ? `<a class="kv-get" href="${provider.key_url}" target="_blank" rel="noopener">Get a key ↗</a>` : ""}
        </div>
        <div class="kv-status">${saved
          ? `Saved in this browser as ${escapeHtml(maskKey(saved))}.`
          : (provider.key_source === "env" ? "A key is configured on the server." : "No key saved.")}</div>`;

      const input = row.querySelector("input");
      const status = row.querySelector(".kv-status");

      const setStatus = (text, tone = "") => {
        status.textContent = text;
        status.className = `kv-status ${tone}`;
      };

      row.querySelector('[data-act="reveal"]').addEventListener("click", () => {
        input.type = input.type === "password" ? "text" : "password";
      });

      row.querySelector('[data-act="save"]').addEventListener("click", async () => {
        const value = input.value.trim();
        if (!value) {
          setStatus("Paste a key into the field first.", "bad");
          return;
        }
        saveKey(provider.name, value);
        input.value = "";
        setStatus(`Saved in this browser as ${maskKey(value)}. Nothing was sent to the server.`, "ok");
        await refreshEngines();
        renderVault(provider.name);
      });

      row.querySelector('[data-act="test"]').addEventListener("click", async () => {
        const value = input.value.trim() || stored[provider.name] || "";
        if (!value) {
          setStatus("Nothing to test - save a key or paste one above.", "bad");
          return;
        }
        setStatus("Checking the key with the engine…", "busy");
        try {
          const response = await fetch(api("keys/validate"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ engine: provider.name, key: value }),
          });
          const verdict = await response.json();
          const detail = verdict.detail ? ` ${verdict.detail}` : "";
          setStatus(`${verdict.message}${detail}`, verdict.ok ? "ok" : "bad");
        } catch (error) {
          setStatus(`Could not reach the studio server: ${error}`, "bad");
        }
      });

      row.querySelector('[data-act="remove"]').addEventListener("click", async () => {
        removeKey(provider.name);
        await refreshEngines();
        renderVault(provider.name);
      });

      host.appendChild(row);
    });

  // Focus only lands on a rendered element, so this must run after the dialog
  // is visible - and re-rendering after Save must not steal focus away.
  if (focusEngine && !$("#keyvault").hidden) {
    const target = host.querySelector(`.kv-row[data-engine="${focusEngine}"]`);
    if (target) {
      target.scrollIntoView({ block: "nearest" });
      if (wasFocused) target.querySelector("input").focus();
    }
  }
}

function openVault(focusEngine = "") {
  $("#keyvault").hidden = false;
  renderVault(focusEngine);
  const target = focusEngine && $(`.kv-row[data-engine="${focusEngine}"] input`);
  (target || $("#keyvault-close")).focus();
  if (target) target.closest(".kv-row").scrollIntoView({ block: "nearest" });
}

function closeVault() {
  $("#keyvault").hidden = true;
  $("#open-vault").focus();
}

function wireVault() {
  $("#open-vault").addEventListener("click", () => openVault());
  $("#keyvault-close").addEventListener("click", closeVault);
  $("#keyvault").addEventListener("click", (event) => {
    if (event.target === $("#keyvault")) closeVault();
  });
}

/* --------------------------------------------------------------- library */

/** Assets generated in this tab, kept in memory. On a serverless deploy the
    server's manifest is wiped whenever the instance goes cold, so without this
    the Library tab would be empty seconds after a successful generation. */
function rememberInSession(outcome) {
  if (!outcome.filename) return;
  const request = state.lastRequest || {};
  const ratio = state.config.ratios.find((r) => r.ratio === request.aspect_ratio) || {};
  state.sessionLibrary.unshift({
    timestamp: new Date().toISOString().slice(0, 19) + "+00:00",
    status: outcome.status,
    provider: state.provider === "auto" ? state.config.active_provider : state.provider,
    prompt: request.prompt || "",
    style: request.style || "none",
    width: ratio.width || outcome.integrity.width,
    height: ratio.height || outcome.integrity.height,
    seed: outcome.seed,
    filename: outcome.filename,
    qa_aesthetic: outcome.qa ? outcome.qa.aesthetic : null,
    error_code: outcome.error_code,
    preview_url: outcome.preview_url,
    data_url: outcome.data_url,
    session: true,
  });
}

function mergeLibrary(serverEntries) {
  const seen = new Set();
  const merged = [];
  [...state.sessionLibrary, ...serverEntries].forEach((entry) => {
    const id = entry.filename || `${entry.timestamp}|${entry.seed}`;
    if (seen.has(id)) return;
    seen.add(id);
    merged.push(entry);
  });
  return merged;
}

async function loadLibrary() {
  const host = $("#library");
  host.innerHTML = `<p class="muted">Reading manifest…</p>`;

  let data = { entries: [], library: state.config.library };
  try {
    const response = await fetch(api("history"));
    data = await response.json();
  } catch {
    /* fall back to the session list alone */
  }
  updateLibraryChip(data.library);

  const entries = mergeLibrary(data.entries || []);
  host.innerHTML = "";

  if (data.library && data.library.ephemeral) {
    const banner = document.createElement("div");
    banner.className = "lib-banner";
    banner.innerHTML =
      `<strong>Ephemeral storage.</strong> ${escapeHtml(data.library.note || "")} ` +
      `Anything below with a dashed border is held in this browser tab only. ` +
      `Download what you want to keep.`;
    host.appendChild(banner);
  }

  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "Nothing generated yet. Assets and their provenance are recorded in manifest.jsonl.";
    host.appendChild(empty);
    return;
  }

  entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = `lib-row${entry.session ? " session" : ""}`;

    const src = entry.preview_url || entry.data_url || (entry.filename ? assetUrl(entry.filename) : "");
    const thumb = src
      ? `<img src="${src}" alt="" loading="lazy" />`
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

    const img = row.querySelector("img");
    if (img) {
      // A cold instance never wrote this file - drop the thumb instead of
      // leaving a broken-image glyph in the row.
      img.addEventListener("error", () => {
        img.replaceWith(Object.assign(document.createElement("div"), {
          className: "lib-thumb-missing",
          textContent: "✕",
        }));
      });
      img.addEventListener("click", () => {
        const target = entry.data_url || entry.preview_url || assetUrl(entry.filename);
        window.open(target, "_blank", "noopener");
      });
    }
    host.appendChild(row);
  });
}

async function refreshLibraryChip() {
  try {
    const response = await fetch(api("history"));
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
