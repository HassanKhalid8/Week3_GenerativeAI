<p align="center">
  <img src="docs/banner.svg" alt="Lumen Forge — an animated six-stage text-to-image pipeline diagram" width="100%" />
</p>

# Lumen Forge

**Multimodal Image Generation Studio &middot; DecodeLabs Generative AI — Project 3**

A web application that translates natural-language descriptions into high-quality
digital artwork. It is built as a **six-stage production pipeline** rather than a
single API call, because the interesting engineering in text-to-image is not the
model — it is everything around it: exact parameter payloads, split timeouts,
moderation gates, memory-safe binary transport, and proving the bytes that
reached disk are actually a complete image.

---

## Do I need an API key? No.

**The studio runs with zero keys and zero signup.** The default engine is
**Pollinations (Flux)**, which is free and requires no API key at all. Clone,
install, run, generate.

Every other engine is optional and only adds a row to the matrix:

| Engine | Key | Cost | Where to get it |
|---|---|---|---|
| **Pollinations (Flux)** | **none** | **free** | — *(default)* |
| Google Gemini | `GEMINI_API_KEY` | free tier | [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) |
| Hugging Face | `HF_TOKEN` | free tier | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| Stability AI — Stable Image Core | `STABILITY_API_KEY` | paid | [platform.stability.ai](https://platform.stability.ai/account/keys) |
| OpenAI gpt-image-1 | `OPENAI_API_KEY` | paid | [platform.openai.com](https://platform.openai.com/api-keys) |
| Offline Mock Renderer | none | free | — *(renders locally, no network)* |

`IMAGESTUDIO_PROVIDER=auto` walks that list and picks the first engine that is
actually usable, so adding a key is the only step needed to switch engines.

### Bringing your own key, in the browser

A key does not have to live on the server. **Engine → Manage keys** opens a vault
where anyone using the studio can paste their own key, test it against the live
engine before spending a generation, and unlock that engine instantly.

The privacy model is deliberately simple, because "trust us" is not a design:

* The key is stored in **that browser's `localStorage`** and nowhere else. The
  server has no database, no session and no file it could write a key to.
* It is sent to the studio's own backend **only in the request that uses it** —
  and only the key for the engine actually selected. It is used and dropped
  within that request; nothing is cached across requests.
* Every string the server sends back passes through `imagestudio/redact.py`,
  which masks both the exact keys it was handed and anything matching a known
  key shape (`sk-…`, `hf_…`, `AIza…`). An engine that quotes your Authorization
  header back inside an error body cannot leak it into the gateway log.
* Nothing is written to `manifest.jsonl` — provenance records the engine, never
  the credential.
* **Remove** erases it from the browser immediately.

Wrong keys fail usefully rather than generically. `classify_auth_error` in
`providers/base.py` separates *invalid key* (401, or a body saying so) from
*forbidden* (403), *out of credit* (402), and *rate limited* (429), and a
credential failure is routed to the network stage rather than through Gate 2 —
telling someone to rephrase their prompt when their key expired would be
actively misleading. The failing card offers an **Add key** button that opens the
vault on the right row.

---

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. That is the whole setup — no `.env` required.

To use a key-based engine instead, copy `.env.example` to `.env` and fill in one line.

### Command line

The same pipeline, driven from a terminal:

```bash
python run.py "a brass orrery on a walnut desk, morning light" --ratio 16:9 --style photoreal -n 3
```

```bash
python run.py --engines
```

```bash
python run.py --presets
```

```bash
python run.py --history
```

### Tests

```bash
python -m pytest tests -q
```

27 tests, all offline — the mock engine exercises every stage without a key, a
quota or a network connection.

---

## The six-stage pipeline

Each stage is a module, and each emits an event so the UI can animate the
pipeline as it actually executes rather than staring at one blocking request.

### 1. Prompt payload formulation — `imagestudio/params.py`, `styles.py`

User intent ("make it a wallpaper") is mapped to **exact strict resolution
variables** before anything is transmitted, because passing unsupported
dimensions causes an immediate API handshake failure.

| Ratio | Resolution | Pixel volume | Target |
|---|---|---|---|
| 16:9 | 1344 × 768 | 1,032,192 | Web banners, presentations |
| 1:1 | 1024 × 1024 | 1,048,576 | Avatars, product grids |
| 9:16 | 768 × 1344 | 1,032,192 | Mobile reels, wallpapers |
| 4:3 · 3:4 · 3:2 · 2:3 · 21:9 | — | ~1.0 MP each | slides, posters, prints, film stills |

Every bucket is a multiple of 64 at roughly one megapixel — the sizes diffusion
backends are natively trained on. Twelve **style presets** (cyberpunk,
minimalism, photoreal, blueprint, film-noir, vaporwave, …) expand a single word
into a full stylistic vector on both the positive and negative prompt, and a
base negative strips watermarks and obvious artifacts from every request.

Generation count (1–4) and a seed make batches reproducible: one seed
deterministically derives the whole batch.

### 2. Network API gateway — `imagestudio/transport.py`

`requests` applies **no timeout by default**, which means a dropped remote
connection hangs the program forever, drains TCP resources, and locks the UI
without surfacing an exception. Every call here passes the split tuple:

```python
requests.post(url, payload, timeout=(3.05, 60))
```

* **Connect timeout 3.05 s** — establishes the TCP connection; the 0.05 s buffer
  absorbs one standard packet retransmission.
* **Read timeout 60 s** — accommodates the slow, iterative diffusion matrix
  generation running on remote GPU clusters.

The retry shield distinguishes the two failure modes exactly as the exception
matrix requires:

| Exception | Diagnosis | Rule applied |
|---|---|---|
| `ConnectTimeout` | Cannot establish TCP — routing, firewall, or downed server | **Fail fast. Do not wait.** |
| `ReadTimeout` | Connected, but inference is slow — saturated GPU cluster | Keep alive, retry |

Retries use **exponential backoff with full jitter** (`uniform(w/2, w)`, capped at
30 s) and apply **selectively by HTTP status** — 429, 503, 504 and friends retry;
a 400 does not. `Retry-After` is honoured when the server sends it.

### 3. Dual security gates — `imagestudio/moderation.py`

* **Gate 1 (pre-generation, input filter)** runs locally before transmission —
  **0 compute cost incurred, instant rejection**, error trace `sentinel_block`.
* **Gate 2 (post-generation, output filter)** interprets a request that already
  burned GPU time: refusal codes, `finish_reason=FILTER`, and the blurred/blank
  **placeholder frame** an engine returns in place of an explicit refusal
  (detected by near-zero contrast and edge energy across a full-size canvas).

Neither gate raises. Both return a verdict, so the app surfaces a polite warning
instead of crashing.

### 4. Transport protocol — `imagestudio/transport.py`

Never load a high-resolution image entirely into RAM. Responses are requested
with `stream=True` and piped straight to disk via
`iter_content(chunk_size=65536)`, with a 64 MiB ceiling so a runaway stream
cannot fill the disk. Engines that return base64 JSON (`gpt-image`, Gemini) go
through the same chunked disk-writer, so every path has one contract.

### 5. Integrity verification — `imagestudio/integrity.py`

**The trap:** `imghdr.what()` and `Image.verify()` only read the block structure
and CRC headers at the very beginning of a file. On a JPEG whose connection
dropped mid-download, both report success — a silent, truncated disaster.

**The fix:** `Image.open(path).load()` forces Pillow to decode the entire stream
pixel by pixel. A file cut off short raises `OSError: broken data stream`, and
the pipeline discards the corrupted asset instead of shipping it.

This is verified by test, and the two formats behave differently:

* **JPEG** — scan-based. `verify()` **passes**, `.load()` catches it. This is the
  exact silent failure the stage exists for.
* **PNG** — chunk-structured, so truncation surfaces one readout earlier.

Both are classified `broken_data_stream` and never reach the library. The stage
also sniffs magic bytes (catching an HTML error page saved with a `.png` name),
records a SHA-256, and reconciles the decoded dimensions against what was
requested.

### 6. Automated quality assurance — `imagestudio/qa.py`

Two lenses, both reported per asset:

* **Lens 1 — aesthetic classification**, scored 0–10 against a configurable gate
  (default 7.0). Assets below it are flagged, and optionally discarded.
* **Lens 2 — semantic alignment**, measuring whether the artwork matches the
  prompt that requested it. Divergent assets are flagged for regeneration.

The scorer runs in **two tiers, and always reports which one produced a score**:

* `clip` — real **CLIP ViT-L/14** embeddings for both lenses. Used automatically
  when `torch` and `transformers` are installed.
* `heuristic` — the always-available fallback and the default. Genuine image
  statistics (Laplacian edge energy, contrast, Hasler–Süsstrunk colorfulness,
  entropy, exposure balance) folded into the same 0–10 scale, with alignment
  measured by checking the prompt's colour / lighting / complexity vocabulary
  against what the pixels actually show.

CLIP is not installed by default because it pulls ~1.7 GB of weights. Uncomment
the two lines in `requirements.txt` to upgrade; the pipeline detects it at
runtime. **A heuristic score is never labelled as a CLIP score.**

---

## The web UI

Three columns mirroring the architecture: **Input Phase**, **Process Phase**,
**Output Phase**.

* **Input** — prompt with a live per-engine character counter, negative prompt,
  style chips, an aspect-ratio selector whose buttons are miniatures of the real
  canvas shape and show exact pixels, count, seed, engine, and a collapsible
  panel for the transport and QA policy (both timeouts, retry budget, QA gate).
* **Process** — the seven-step pipeline trace animating live over **Server-Sent
  Events**, the serialized payload as actually sent, and a gateway log showing
  every attempt, retry and backoff interval.
* **Output** — a gallery of generated assets with per-asset metrics (aesthetic,
  alignment, bytes, chunk count, decoded pixels, seed, elapsed), download and
  full-res links, and an inspector with the complete provenance record. A
  **Library** tab reads the manifest for everything generated so far.

Generation runs on a worker thread; the browser consumes stage events over SSE,
so a 45-second denoise on a free GPU queue shows real progress instead of a
spinner.

The engine picker is a hand-built ARIA combobox rather than a `<select>`. A
native listbox renders in the OS's own style and can only hold a line of text
per row; this one carries the engine's badge (`free` / `paid` / `key saved` /
`needs key`), its notes and its prompt ceiling, is fully keyboard-driven
(arrows, Home/End, typeahead, Escape), and turns a locked engine into an
invitation — clicking one opens the key vault on that engine instead of doing
nothing.

---

## Deploying to Vercel

```bash
vercel deploy
```

`vercel.json` and `api/index.py` are already in the repo; `requirements.txt` is
picked up automatically. No environment variables are required — the deployed
studio runs on Pollinations, and visitors supply their own keys for anything
else through the key vault.

Two things about a serverless host change how the studio behaves, and both are
handled rather than hidden:

**The filesystem is read-only.** The deploy bundle is mounted at `/var/task`, so
Stage 4's `open(destination, "wb")` fails with `[Errno 30]`. `assets_root()` now
*probes* each candidate with a real write before accepting it and falls back to
a temp directory, and a sink that still cannot be opened raises a
`TransportError` rather than a bare `OSError` — so one failed write costs one
asset instead of the whole batch. Set `IMAGESTUDIO_ASSETS` to mounted storage to
get a durable library; otherwise the Library tab shows an honest
*ephemeral storage* banner and lists what this browser tab generated, held in
memory. Every asset is also returned inline as a data URI, so the gallery never
depends on a second request finding the file on the same instance.

**Each request is its own process.** The POST that starts a job and the GET that
reads its event stream can land on different instances, which makes SSE and the
in-memory job registry unusable. When `VERCEL` is set, `/api/config` reports
`streaming: false` and the browser calls `POST /api/generate/sync`, which runs
the batch inside one request and returns the collected event log for the client
to replay through the same handlers. The pipeline still animates; it just
animates after the fact. Batch size drops to 2 so the inlined images fit
Vercel's 4.5 MB response cap, and `IMAGESTUDIO_BUDGET_SECONDS` (52 by default)
stops the retry shield from backing off past the 60 s function ceiling — a
throttled engine reports a readable error instead of a gateway timeout.

### Endpoints

| Route | Purpose |
|---|---|
| `GET /api/config` | ratios, styles, engines, stages, transport profile |
| `POST /api/engines` | re-read engine availability with the caller's keys applied |
| `POST /api/keys/validate` | probe one key against its engine without generating |
| `POST /api/generate` → `GET /api/jobs/<id>/events` | streaming transport (local) |
| `POST /api/generate/sync` | single-request transport (serverless) |
| `GET /api/history` | manifest entries plus library stats |
| `GET /assets/<file>[/download]` | stored asset, 404 if this instance never wrote it |

---

## The download pipeline

Every asset that survives Stage 5 lands in `assets/` as
`{utc-timestamp}_{prompt-slug}_s{seed}_{index}.{ext}`, and one JSON line is
appended to `assets/manifest.jsonl` with the full provenance: prompt, composed
prompt, exact payload, engine, seed, byte count, chunk count, SHA-256, integrity
verdict, gate verdicts and both QA scores. Rejected assets are recorded too, with
their failure code — the manifest is the audit trail, not just the successes.

---

## A note on what the free engine actually returns

Pollinations honours the aspect ratio but **downscales the long edge**: a request
for 1344 × 768 comes back at 1015 × 580, and 1024 × 1024 comes back at 768 × 768.
The studio does not hide this. Stage 5 reconciles decoded dimensions against the
requested payload and marks the asset with a dimension deviation, which is
exactly the kind of silent substitution the integrity stage exists to surface.

Engines with exact pixel control (Hugging Face, Stability AI) honour the payload
precisely. Nothing is upscaled or resampled to paper over the difference — the
pipeline reports what actually arrived.

---

## Project layout

```
multimodal-image-generation-studio/
├── app.py                  # entrypoint: python app.py
├── run.py                  # CLI pipeline
├── api/index.py            # Vercel entrypoint (same WSGI app)
├── vercel.json             # maxDuration 60s, everything rewritten to the function
├── requirements.txt
├── .env.example            # every key optional
├── imagestudio/
│   ├── params.py           # Stage 1 - aspect ratio map, payload validation
│   ├── styles.py           # Stage 1 - style presets, negative composition
│   ├── transport.py        # Stage 2 + 4 - split timeout, backoff, chunked stream
│   ├── moderation.py       # Stage 3 - dual security gates
│   ├── integrity.py        # Stage 5 - forced pixel decode
│   ├── qa.py               # Stage 6 - aesthetic + semantic alignment
│   ├── storage.py          # asset library, manifest, writable-root probing
│   ├── redact.py           # scrubs API keys out of anything sent to the browser
│   ├── engine.py           # the orchestrator
│   └── providers/          # pollinations, gemini, huggingface, stability, openai, mock
├── webapp/
│   ├── app.py              # Flask; SSE locally, single-request on serverless
│   ├── templates/index.html
│   └── static/{app.css, app.js}
├── tests/test_pipeline.py  # 54 offline tests
└── assets/                 # generated artwork + manifest.jsonl
```

---

## Key skills demonstrated

Text-to-image API integration across four different response contracts (raw
binary stream, base64 JSON, multipart form, URL redirect) · exact design
parameter payloads · handling image URLs and binary streams · split-timeout
network policy · exponential backoff with jitter · moderation gate handling ·
memory-safe chunked I/O · binary integrity verification · automated quality
assurance · serverless deployment under a read-only filesystem · per-request
credential handling with output redaction · hand-built accessible ARIA widgets.
