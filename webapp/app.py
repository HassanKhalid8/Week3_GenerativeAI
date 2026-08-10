"""The studio web application.

Two transports, one pipeline:

* Locally, a generation runs on a worker thread and pushes stage events onto a
  queue; the browser consumes them over Server-Sent Events, so the seven-stage
  pipeline animates as it actually executes instead of the UI staring at one
  blocking POST.
* On a serverless host, that model cannot work - the POST that starts the job
  and the GET that reads its events are separate invocations that may land on
  different instances, so the in-memory job registry would come up empty. There
  the whole batch runs inside one request and the collected events are returned
  with the result for the browser to replay.

API keys pasted by a visitor arrive in the request body, are used, and are
dropped. They are never stored, and every string sent back to the browser is
scrubbed so an engine that echoes a key in an error body cannot leak it.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import NotFound

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from imagestudio import STAGES, GenerationRequest, ratio_table, style_table  # noqa: E402
from imagestudio import providers as provider_registry  # noqa: E402
from imagestudio.engine import Studio  # noqa: E402
from imagestudio.params import MAX_COUNT  # noqa: E402
from imagestudio.redact import scrub, scrub_deep  # noqa: E402
from imagestudio.storage import assets_root, library_stats, read_manifest  # noqa: E402
from imagestudio.transport import (  # noqa: E402
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    build_session,
)

app = Flask(__name__, static_folder="static", template_folder="templates")

# --- host profile ---------------------------------------------------------
# Serverless hosts kill a function at a fixed ceiling and give each invocation
# its own process, which rules out both SSE and the cross-request job registry.
SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
#: Wall-clock budget handed to the retry shield. Keep it under the platform's
#: function timeout (vercel.json sets maxDuration to 60s) so a saturated engine
#: surfaces a readable error instead of a gateway timeout.
REQUEST_BUDGET_SECONDS = float(os.getenv("IMAGESTUDIO_BUDGET_SECONDS", "52" if SERVERLESS else "0"))
#: Inlining every asset as base64 has to fit the platform's response cap, so a
#: serverless batch is capped tighter than a local one.
EFFECTIVE_MAX_COUNT = 2 if SERVERLESS else MAX_COUNT

MAX_KEY_LENGTH = 400

# --- in-memory job registry (local/SSE transport only) --------------------
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 1800
SENTINEL = object()


def _reap_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with _JOBS_LOCK:
        stale = [jid for jid, job in _JOBS.items() if job["created"] < cutoff]
        for jid in stale:
            _JOBS.pop(jid, None)


# --- request helpers ------------------------------------------------------

def _keys_from(payload: dict) -> dict[str, str]:
    """Pull the visitor's API keys out of one request body.

    Whitelisted against the registry and length-capped, then handed straight to
    the Studio for that batch. Nothing here is cached, logged or persisted.
    """
    raw = payload.get("api_keys")
    if not isinstance(raw, dict):
        return {}
    keys: dict[str, str] = {}
    for name, value in raw.items():
        if name not in provider_registry.REGISTRY or not isinstance(value, str):
            continue
        value = value.strip()
        if value and len(value) <= MAX_KEY_LENGTH:
            keys[name] = value
    return keys


def _build_request(payload: dict) -> GenerationRequest:
    count = payload.get("count", 1)
    try:
        count = min(int(count), EFFECTIVE_MAX_COUNT)
    except (TypeError, ValueError):
        count = 1
    return GenerationRequest(
        prompt=payload.get("prompt", ""),
        negative_prompt=payload.get("negative_prompt", ""),
        aspect_ratio=payload.get("aspect_ratio", "1:1"),
        style=payload.get("style", "none"),
        count=count,
        seed=payload.get("seed"),
        provider=payload.get("provider", "auto"),
        connect_timeout=float(payload.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)),
        read_timeout=float(payload.get("read_timeout", DEFAULT_READ_TIMEOUT)),
        max_retries=int(payload.get("max_retries", 3)),
        budget_seconds=REQUEST_BUDGET_SECONDS,
        qa_threshold=float(payload.get("qa_threshold", 7.0)),
        qa_discard=bool(payload.get("qa_discard", False)),
    )


def _run_pipeline(payload: dict, emit) -> dict:
    """Execute one batch and return its serialized result.

    Shared by both transports so the SSE path and the single-request path can
    never drift apart. `emit` is scrubbed before it is handed to the pipeline,
    which means no event can carry a key even if an engine echoes one back.
    """
    keys = _keys_from(payload)
    secrets = tuple(keys.values())

    def safe_emit(kind: str, data: dict) -> None:
        emit(kind, scrub_deep(data, secrets))

    try:
        req = _build_request(payload)
        result = Studio(keys=keys).generate(req, on_event=safe_emit)
        return scrub_deep(result.to_dict(), secrets)
    except Exception as exc:  # a crash here must still reach the browser
        message = scrub(str(exc), secrets)
        payload_out = {"error": message, "error_code": type(exc).__name__, "assets": []}
        safe_emit("error", {"message": message, "code": type(exc).__name__})
        return payload_out


def _run_job(job_id: str, payload: dict) -> None:
    job = _JOBS[job_id]
    events: queue.Queue = job["queue"]

    def emit(kind: str, data: dict) -> None:
        events.put({"kind": kind, "data": data})

    try:
        job["result"] = _run_pipeline(payload, emit)
    finally:
        emit("done", job.get("result") or {})
        events.put(SENTINEL)


# --- routes ---------------------------------------------------------------

_DOCUMENT_CACHE: dict[str, str] = {}


def _build_document() -> str:
    """The page with its stylesheet and script inlined.

    Two fewer requests on a cold serverless start, and - more importantly - the
    UI no longer depends on /static/* being routed. A catch-all rewrite is a
    single point of failure; folding the assets into the one response that
    definitely works removes two of the three ways this page can come up bare.
    """
    root = Path(app.root_path)
    html = (root / app.template_folder / "index.html").read_text(encoding="utf-8")
    css = (root / app.static_folder / "app.css").read_text(encoding="utf-8")
    js = (root / app.static_folder / "app.js").read_text(encoding="utf-8")

    # A literal </script> inside the script text would close the tag early.
    js = js.replace("</script", "<\\/script")

    html = html.replace(
        '<link rel="stylesheet" href="/static/app.css" />',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>\n{js}\n</script>",
    )
    return html


def _document() -> str:
    if app.debug or "html" not in _DOCUMENT_CACHE:
        _DOCUMENT_CACHE["html"] = _build_document()
    return _DOCUMENT_CACHE["html"]


@app.get("/")
def index():
    try:
        return Response(_document(), mimetype="text/html")
    except OSError as exc:
        # A deploy that failed to bundle the front-end would otherwise answer a
        # generic 404 that says nothing about what is actually missing.
        return (
            jsonify({
                "error": "The front-end files were not bundled with this deployment.",
                "detail": str(exc),
                "hint": "Check includeFiles in vercel.json and .vercelignore.",
            }),
            500,
        )


@app.get("/api/health")
def health():
    """Cheap liveness and routing probe.

    The catch-all rewrite means a misrouted deploy answers 404 for every page
    with no clue why; hitting this shows the path Flask actually received.
    """
    root = Path(app.root_path)
    return jsonify(
        {
            "ok": True,
            "serverless": SERVERLESS,
            "streaming": not SERVERLESS,
            "path_seen_by_flask": request.path,
            "template_bundled": (root / app.template_folder / "index.html").is_file(),
            "static_bundled": (root / app.static_folder / "app.js").is_file(),
            "assets_root": str(assets_root()),
            "assets_ephemeral": library_stats()["ephemeral"],
        }
    )


@app.get("/api/config")
def config():
    active = provider_registry.resolve("auto")
    return jsonify(
        {
            "ratios": ratio_table(),
            "styles": style_table(),
            "providers": provider_registry.catalogue(),
            "active_provider": active.name,
            "stages": [{"key": key, "label": label} for key, label in STAGES],
            "max_count": EFFECTIVE_MAX_COUNT,
            "streaming": not SERVERLESS,
            "serverless": SERVERLESS,
            "defaults": {
                "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
                "read_timeout": DEFAULT_READ_TIMEOUT,
                "max_retries": 3,
                "qa_threshold": 7.0,
            },
            "library": library_stats(),
        }
    )


@app.post("/api/engines")
def engines():
    """Re-read engine availability with the visitor's keys applied.

    Called the moment a key is saved or removed so the picker updates without a
    page reload. The keys are used to answer this one question and discarded.
    """
    payload = request.get_json(silent=True) or {}
    keys = _keys_from(payload)
    catalogue = provider_registry.catalogue(keys)
    return jsonify(
        {
            "providers": catalogue,
            "active_provider": provider_registry.resolve("auto", keys).name,
        }
    )


@app.post("/api/keys/validate")
def validate_key():
    """Check one key against its engine without spending a generation."""
    payload = request.get_json(silent=True) or {}
    name = payload.get("engine", "")
    if name not in provider_registry.REGISTRY:
        return jsonify({"ok": False, "code": "unknown_provider", "message": "Unknown engine."}), 400

    key = (payload.get("key") or "").strip()
    if len(key) > MAX_KEY_LENGTH:
        return jsonify({"ok": False, "code": "invalid_api_key", "message": "That key is implausibly long."}), 400

    provider = provider_registry.build(name, {name: key} if key else None)
    verdict = provider.verify_key(build_session())
    return jsonify(scrub_deep(verdict, (key,) if key else ()))


@app.post("/api/generate")
def generate():
    _reap_jobs()
    payload = request.get_json(silent=True) or {}
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"queue": queue.Queue(), "created": time.time(), "result": None}
    threading.Thread(target=_run_job, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.post("/api/generate/sync")
def generate_sync():
    """Run the whole batch inside this request and hand back the event log.

    The browser replays the events through the same handlers the SSE stream
    feeds, so the pipeline still animates - it just animates after the fact.
    """
    payload = request.get_json(silent=True) or {}
    events: list[dict] = []
    result = _run_pipeline(payload, lambda kind, data: events.append({"kind": kind, "data": data}))
    return jsonify({"events": events, "result": result})


@app.get("/api/jobs/<job_id>/events")
def job_events(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown or expired job."}), 404

    def stream():
        events: queue.Queue = job["queue"]
        yield "retry: 2000\n\n"
        while True:
            try:
                item = events.get(timeout=20)
            except queue.Empty:
                yield ": keep-alive\n\n"      # hold the connection through a slow denoise
                continue
            if item is SENTINEL:
                break
            yield f"event: {item['kind']}\ndata: {json.dumps(item['data'], default=str)}\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/jobs/<job_id>")
def job_result(job_id: str):
    job = _JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown or expired job."}), 404
    return jsonify(job["result"] or {"status": "running"})


@app.get("/api/history")
def history():
    return jsonify({"entries": read_manifest(limit=80), "library": library_stats()})


def _serve_asset(filename: str, as_attachment: bool):
    """Serve a stored asset, or 404 cleanly if this instance never wrote it.

    On an ephemeral filesystem the file may simply not exist here; the browser
    falls back to the inline copy it already holds rather than showing a break.
    """
    try:
        return send_from_directory(assets_root(), filename, as_attachment=as_attachment)
    except (NotFound, OSError):
        return jsonify({"error": "Asset is not on this instance."}), 404


@app.get("/assets/<path:filename>")
def asset(filename: str):
    return _serve_asset(filename, as_attachment=False)


@app.get("/assets/<path:filename>/download")
def asset_download(filename: str):
    return _serve_asset(filename, as_attachment=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Multimodal Image Generation Studio - web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    active = provider_registry.resolve("auto")
    print("  Lumen Forge - Multimodal Image Generation Studio")
    print(f"  Engine    : {active.label} ({'no key required' if not active.env_key else active.env_key + ' detected'})")
    print(f"  Assets    : {assets_root()}")
    print(f"  Transport : {'single-request (serverless)' if SERVERLESS else 'server-sent events'}")
    print(f"  Studio    : http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
