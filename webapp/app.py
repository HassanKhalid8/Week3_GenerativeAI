"""The studio web application.

A generation runs on a worker thread and pushes stage events onto a queue; the
browser consumes them over Server-Sent Events, so the six-stage pipeline animates
as it actually executes instead of the UI staring at one blocking POST.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

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
from imagestudio.storage import assets_root, library_stats, read_manifest  # noqa: E402
from imagestudio.transport import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT  # noqa: E402

app = Flask(__name__, static_folder="static", template_folder="templates")

# --- in-memory job registry ---------------------------------------------
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


def _run_job(job_id: str, payload: dict) -> None:
    job = _JOBS[job_id]
    events: queue.Queue = job["queue"]

    def emit(kind: str, data: dict) -> None:
        events.put({"kind": kind, "data": data})

    try:
        req = GenerationRequest(
            prompt=payload.get("prompt", ""),
            negative_prompt=payload.get("negative_prompt", ""),
            aspect_ratio=payload.get("aspect_ratio", "1:1"),
            style=payload.get("style", "none"),
            count=payload.get("count", 1),
            seed=payload.get("seed"),
            provider=payload.get("provider", "auto"),
            connect_timeout=float(payload.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)),
            read_timeout=float(payload.get("read_timeout", DEFAULT_READ_TIMEOUT)),
            max_retries=int(payload.get("max_retries", 3)),
            qa_threshold=float(payload.get("qa_threshold", 7.0)),
            qa_discard=bool(payload.get("qa_discard", False)),
        )
        result = Studio().generate(req, on_event=emit)
        job["result"] = result.to_dict()
    except Exception as exc:  # a crash here must still reach the browser
        job["result"] = {"error": str(exc), "error_code": type(exc).__name__, "assets": []}
        emit("error", {"message": str(exc), "code": type(exc).__name__})
    finally:
        emit("done", job.get("result") or {})
        events.put(SENTINEL)


@app.get("/")
def index():
    return send_from_directory(app.template_folder, "index.html")


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
            "max_count": MAX_COUNT,
            "defaults": {
                "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
                "read_timeout": DEFAULT_READ_TIMEOUT,
                "max_retries": 3,
                "qa_threshold": 7.0,
            },
            "library": library_stats(),
        }
    )


@app.post("/api/generate")
def generate():
    _reap_jobs()
    payload = request.get_json(silent=True) or {}
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"queue": queue.Queue(), "created": time.time(), "result": None}
    threading.Thread(target=_run_job, args=(job_id, payload), daemon=True).start()
    return jsonify({"job_id": job_id})


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


@app.get("/assets/<path:filename>")
def asset(filename: str):
    return send_from_directory(assets_root(), filename)


@app.get("/assets/<path:filename>/download")
def asset_download(filename: str):
    return send_from_directory(assets_root(), filename, as_attachment=True)


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
    print(f"  Studio    : http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
