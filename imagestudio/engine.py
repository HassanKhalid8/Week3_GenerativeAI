"""The orchestrator - the complete Project 3 blueprint, end to end.

  1. Prompt payload formulation   params.py  + styles.py
  2. Network API gateway          transport.py (split timeout, backoff + jitter)
  3. Security & moderation gates  moderation.py (input gate, then output gate)
  4. Transport protocol           transport.py (65536-byte chunked streaming)
  5. Integrity verification       integrity.py (forced .load() pixel decode)
  6. Automated quality assurance  qa.py (aesthetic + semantic alignment)

Every stage emits an event, so a caller (the web UI, the CLI) can render the
pipeline as it actually runs rather than waiting on one opaque call.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
from PIL import Image

from . import integrity, moderation, qa, storage, styles
from .params import GenerationRequest, ParameterError
from .providers import AUTH_ERROR_CODES, Provider, ProviderError, resolve
from .transport import (
    StreamResult,
    TransportError,
    build_session,
    stream_to_file,
    write_bytes,
)

EventSink = Callable[[str, dict], None]

_MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}

#: Longest edge of the inline card preview. The gallery renders at ~240 CSS px
#: and the inspector at ~700; 1280 is generous for both at 2x density.
PREVIEW_EDGE = 1280
PREVIEW_QUALITY = 84

#: Ceiling on how much base64 a single batch may inline. Serverless platforms cap
#: a response body (Vercel: 4.5 MB), and four full-res PNGs sail past it - so the
#: full-res data URI is a best-effort extra, while the small preview is always sent.
INLINE_BUDGET_BYTES = 3_400_000


def _data_url(path: Path, fmt: str) -> str:
    """Base64 data URI for a verified asset - self-contained, no disk read on serve."""
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:{_MIME_BY_FORMAT.get(fmt, 'image/jpeg')};base64,{encoded}"


def _preview_url(path: Path) -> str:
    """A small inline JPEG of the asset - always affordable, always renderable.

    The gallery must show something even when the full-res bytes are too large to
    inline and /assets/<file> is unreachable, which is the normal state on a
    serverless host whose next request lands on a colder instance.
    """
    try:
        with Image.open(path) as image:
            image.load()
            preview = image.convert("RGB")
            preview.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS)
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    except (OSError, ValueError):
        return ""
    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

STAGES = [
    ("payload", "Prompt Payload Formulation"),
    ("gate1", "Security Gate 1 - Input Filter"),
    ("network", "Network API Gateway"),
    ("transport", "Transport Protocol"),
    ("integrity", "Integrity Verification"),
    ("gate2", "Security Gate 2 - Output Filter"),
    ("qa", "Automated Quality Assurance"),
]


@dataclass
class AssetOutcome:
    index: int
    status: str                       # accepted | flagged | rejected | failed
    seed: int
    filename: str = ""
    url: str = ""
    data_url: str = ""
    preview_url: str = ""
    error: str = ""
    error_code: str = ""
    needs_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    transport: dict[str, Any] = field(default_factory=dict)
    stream: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    gate2: dict[str, Any] = field(default_factory=dict)
    qa: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "seed": self.seed,
            "filename": self.filename,
            "url": self.url,
            "data_url": self.data_url,
            "preview_url": self.preview_url,
            "error": self.error,
            "error_code": self.error_code,
            "needs_key": self.needs_key,
            "payload": self.payload,
            "transport": self.transport,
            "stream": self.stream,
            "integrity": self.integrity,
            "gate2": self.gate2,
            "qa": self.qa,
            "meta": self.meta,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class BatchResult:
    provider: str
    provider_label: str
    request: dict[str, Any]
    composed_prompt: str
    composed_negative: str
    gate1: dict[str, Any]
    assets: list[AssetOutcome] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str = ""
    error_code: str = ""
    needs_key: str = ""
    storage_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "request": self.request,
            "composed_prompt": self.composed_prompt,
            "composed_negative": self.composed_negative,
            "gate1": self.gate1,
            "assets": [a.to_dict() for a in self.assets],
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "error_code": self.error_code,
            "needs_key": self.needs_key,
            "storage_note": self.storage_note,
            "accepted": sum(1 for a in self.assets if a.status == "accepted"),
            "counts": {
                status: sum(1 for a in self.assets if a.status == status)
                for status in ("accepted", "flagged", "rejected", "failed")
            },
        }


class Studio:
    """Runs generation batches through the full pipeline."""

    def __init__(
        self,
        provider: Provider | None = None,
        session: requests.Session | None = None,
        keys: Mapping[str, str] | None = None,
    ):
        self._provider = provider
        self._session = session or build_session()
        # Caller-supplied API keys for this batch only. Held on the instance
        # (which the web layer creates per request and drops) so they never
        # reach GenerationRequest.to_dict(), the manifest, or any global.
        self._keys = dict(keys or {})
        self._inline_budget = INLINE_BUDGET_BYTES

    def provider_for(self, name: str) -> Provider:
        if self._provider is not None and name in ("auto", self._provider.name):
            return self._provider
        return resolve(name, self._keys)

    # -- the batch ------------------------------------------------------
    def generate(self, request: GenerationRequest, on_event: EventSink | None = None) -> BatchResult:
        emit = on_event or (lambda *_: None)
        started = time.perf_counter()
        self._inline_budget = INLINE_BUDGET_BYTES

        try:
            provider = self.provider_for(request.provider)
        except ProviderError as exc:
            # The engine cannot even be constructed - almost always a missing key.
            emit("stage", {"stage": "payload", "state": "failed", "detail": str(exc), "code": exc.code})
            return BatchResult(
                provider=request.provider,
                provider_label=request.provider,
                request=request.to_dict(),
                composed_prompt="",
                composed_negative="",
                gate1={},
                error=str(exc),
                error_code=exc.code,
                needs_key=request.provider if exc.code == "missing_key" else "",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        # ---- Stage 1: prompt payload formulation ----------------------
        emit("stage", {"stage": "payload", "state": "running"})
        try:
            request.validate(max_prompt_chars=provider.max_prompt_chars)
        except ParameterError as exc:
            emit("stage", {"stage": "payload", "state": "failed", "detail": str(exc)})
            return BatchResult(
                provider=provider.name,
                provider_label=provider.label,
                request=request.to_dict(),
                composed_prompt="",
                composed_negative="",
                gate1={},
                error=str(exc),
                error_code="parameter_error",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        composed_prompt, composed_negative = styles.compose(
            request.prompt, request.negative_prompt, request.style
        )
        if not provider.supports_negative:
            emit(
                "note",
                {"text": f"{provider.label} has no negative-prompt field; constraints are folded into the prompt text."},
            )
        emit(
            "stage",
            {
                "stage": "payload",
                "state": "done",
                "detail": f"{request.width}x{request.height} - {request.width * request.height:,} px - "
                          f"{request.count} image(s) - {len(composed_prompt)}/{provider.max_prompt_chars} chars",
            },
        )

        # ---- Stage 3a: security gate 1 (before any compute is spent) --
        emit("stage", {"stage": "gate1", "state": "running"})
        gate1 = moderation.gate_input(composed_prompt, composed_negative)
        emit(
            "stage",
            {
                "stage": "gate1",
                "state": "failed" if not gate1.ok else "done",
                "detail": gate1.message,
                "code": gate1.code,
            },
        )
        if not gate1.ok:
            return BatchResult(
                provider=provider.name,
                provider_label=provider.label,
                request=request.to_dict(),
                composed_prompt=composed_prompt,
                composed_negative=composed_negative,
                gate1=gate1.to_dict(),
                error=gate1.message,
                error_code=gate1.code,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )

        result = BatchResult(
            provider=provider.name,
            provider_label=provider.label,
            request=request.to_dict(),
            composed_prompt=composed_prompt,
            composed_negative=composed_negative,
            gate1=gate1.to_dict(),
        )

        for index in range(request.count):
            seed = request.seed_for(index)
            emit("asset_start", {"index": index, "total": request.count, "seed": seed})
            outcome = self._one(provider, request, composed_prompt, composed_negative, seed, index, emit)
            result.assets.append(outcome)
            emit("asset_done", {"index": index, "outcome": outcome.to_dict()})

        # Surface a credential problem at batch level so the UI can offer the fix
        # once, rather than repeating it on every card.
        for outcome in result.assets:
            if outcome.needs_key:
                result.needs_key = outcome.needs_key
                result.error = result.error or outcome.error
                result.error_code = result.error_code or outcome.error_code
                break

        if storage.is_ephemeral():
            result.storage_note = storage.EPHEMERAL_NOTE

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        emit("batch_done", result.to_dict())
        return result

    # -- one asset ------------------------------------------------------
    def _one(
        self,
        provider: Provider,
        request: GenerationRequest,
        prompt: str,
        negative: str,
        seed: int,
        index: int,
        emit: EventSink,
    ) -> AssetOutcome:
        started = time.perf_counter()
        outcome = AssetOutcome(index=index, status="failed", seed=seed)
        outcome.payload = provider.build_payload(request, prompt, negative, seed)

        # ---- Stage 2: network API gateway -----------------------------
        emit("stage", {"stage": "network", "state": "running", "index": index,
                       "detail": f"timeout=({request.connect_timeout}, {request.read_timeout}) retries={request.max_retries}"})
        try:
            provider_result = provider.submit(
                self._session, request, prompt, negative, seed,
                on_event=lambda kind, data: emit(kind, {**data, "index": index}),
            )
        except (ProviderError, TransportError) as exc:
            code = getattr(exc, "code", "provider_error")
            outcome.error = str(exc)
            outcome.error_code = code

            if code in AUTH_ERROR_CODES:
                # A rejected or exhausted key is a credential problem, not a
                # moderation one - routing it through Gate 2 would tell the user
                # to rephrase their prompt, which cannot possibly help.
                outcome.needs_key = provider.name
                emit(
                    "stage",
                    {"stage": "network", "state": "failed", "index": index,
                     "detail": str(exc), "code": code, "needs_key": provider.name},
                )
                outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
                return outcome

            # A refusal that reached us as an HTTP error is a Gate 2 event.
            verdict = moderation.gate_output_response(getattr(exc, "status", None) or 0, f"{code} {exc}")
            if not verdict.ok:
                outcome.status = "rejected"
                outcome.gate2 = verdict.to_dict()
                emit("stage", {"stage": "gate2", "state": "failed", "index": index, "detail": verdict.message})
            else:
                emit("stage", {"stage": "network", "state": "failed", "index": index, "detail": str(exc)})
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        if provider_result.transport is not None:
            outcome.transport = provider_result.transport.to_dict()
        outcome.meta = provider_result.meta
        emit("stage", {"stage": "network", "state": "done", "index": index,
                       "detail": f"handshake ok in {outcome.transport.get('total_ms', 0)} ms"})

        # ---- Stage 4: transport protocol (chunked, memory-safe) -------
        emit("stage", {"stage": "transport", "state": "running", "index": index})
        suffix = ".png"
        if provider_result.stream is not None:
            ctype = provider_result.stream.headers.get("Content-Type", "")
            if "jpeg" in ctype or "jpg" in ctype:
                suffix = ".jpg"
            elif "webp" in ctype:
                suffix = ".webp"
        destination = storage.assets_root() / storage.asset_filename(request.prompt, seed, index, suffix)

        try:
            stream_result = self._persist(provider_result, destination, emit, index)
        except TransportError as exc:
            outcome.error = str(exc)
            outcome.error_code = exc.code
            emit("stage", {"stage": "transport", "state": "failed", "index": index, "detail": str(exc)})
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        outcome.stream = stream_result.to_dict()
        emit(
            "stage",
            {
                "stage": "transport",
                "state": "done",
                "index": index,
                "detail": f"{stream_result.bytes_written:,} bytes in {stream_result.chunks} chunk(s) of 64 KiB",
            },
        )

        # ---- Stage 5: integrity verification --------------------------
        emit("stage", {"stage": "integrity", "state": "running", "index": index})
        report = integrity.verify_asset(destination, request.width, request.height)
        outcome.integrity = report.to_dict()
        if not report.ok:
            storage.purge_rejected(destination)
            outcome.status = "rejected"
            outcome.error = report.message
            outcome.error_code = report.code
            emit("stage", {"stage": "integrity", "state": "failed", "index": index, "detail": report.message})
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._record(request, provider, outcome, prompt, negative)
            return outcome
        emit(
            "stage",
            {
                # A clean decode at the wrong size is still a deviation worth seeing.
                "stage": "integrity",
                "state": "done" if report.dimension_match else "warn",
                "index": index,
                "detail": f"{report.fmt} {report.width}x{report.height} - {report.message}",
            },
        )

        outcome.filename = destination.name
        outcome.url = f"/assets/{destination.name}"
        # Serverless deploys (Vercel, Lambda) ship a read-only bundle and an
        # ephemeral /tmp, so a later request for /assets/<file> can land on a
        # different, colder instance that never wrote this file. Sending the
        # bytes inline means the browser never depends on a second disk read.
        # The small preview always goes; the full-res copy only while the
        # response body still has room for it.
        outcome.preview_url = _preview_url(destination)
        self._inline_budget -= len(outcome.preview_url)
        full = _data_url(destination, report.fmt)
        if full and len(full) <= self._inline_budget:
            outcome.data_url = full
            self._inline_budget -= len(full)

        # ---- Stage 3b: security gate 2 (output filter) ----------------
        emit("stage", {"stage": "gate2", "state": "running", "index": index})
        stats = qa.measure(destination)
        verdict = moderation.gate_output_image(stats)
        finish = str(provider_result.meta.get("finish_reason", ""))
        if finish.upper() in ("CONTENT_FILTERED", "SAFETY", "FILTER"):
            verdict = moderation.Verdict(
                moderation.BLOCKED,
                "finish_reason=FILTER",
                "The engine flagged its own output and withheld the artwork.",
                "output_filter",
            )
        outcome.gate2 = verdict.to_dict()
        if not verdict.ok:
            storage.purge_rejected(destination)
            outcome.status = "rejected"
            outcome.error = verdict.message
            outcome.error_code = verdict.code
            outcome.filename = ""
            outcome.url = ""
            outcome.data_url = ""
            outcome.preview_url = ""
            emit("stage", {"stage": "gate2", "state": "failed", "index": index, "detail": verdict.message})
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._record(request, provider, outcome, prompt, negative)
            return outcome
        emit("stage", {"stage": "gate2", "state": "done", "index": index, "detail": verdict.message})

        # ---- Stage 6: automated QA ------------------------------------
        emit("stage", {"stage": "qa", "state": "running", "index": index})
        qa_report = qa.assess(destination, prompt, threshold=request.qa_threshold)
        outcome.qa = qa_report.to_dict()
        if qa_report.passed:
            outcome.status = "accepted"
            emit("stage", {"stage": "qa", "state": "done", "index": index,
                           "detail": f"aesthetic {qa_report.aesthetic:.2f}/10 - alignment {qa_report.alignment:.2f} ({qa_report.scorer})"})
        else:
            outcome.status = "flagged"
            emit("stage", {"stage": "qa", "state": "warn", "index": index,
                           "detail": f"below gate: aesthetic {qa_report.aesthetic:.2f}/10, alignment {qa_report.alignment:.2f}"})
            if request.qa_discard:
                storage.purge_rejected(destination)
                outcome.status = "rejected"
                outcome.error = "Discarded by the QA gate."
                outcome.error_code = "qa_below_threshold"
                outcome.filename = ""
                outcome.url = ""
                outcome.data_url = ""
                outcome.preview_url = ""

        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._record(request, provider, outcome, prompt, negative)
        return outcome

    # -- helpers --------------------------------------------------------
    def _persist(self, provider_result, destination: Path, emit: EventSink, index: int) -> StreamResult:
        """Write the engine's output to disk without ever holding it all in RAM."""
        if provider_result.stream is not None:
            return stream_to_file(
                provider_result.stream,
                destination,
                on_progress=lambda written: emit("progress", {"index": index, "bytes": written}),
            )
        if provider_result.raw is not None:
            return write_bytes(provider_result.raw, destination)
        if provider_result.url:
            # Public-URL engines: fetch the asset through the same shielded gateway.
            from .transport import request_with_retry

            response, _ = request_with_retry(
                self._session, "GET", provider_result.url, stream=True, max_retries=2
            )
            return stream_to_file(response, destination)
        raise TransportError("Engine returned no stream, bytes or URL.", code="empty_result")

    def _record(
        self,
        request: GenerationRequest,
        provider: Provider,
        outcome: AssetOutcome,
        prompt: str,
        negative: str,
    ) -> None:
        storage.record(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": outcome.status,
                "provider": provider.name,
                "provider_label": provider.label,
                "prompt": request.prompt,
                "composed_prompt": prompt,
                "negative_prompt": negative,
                "style": request.style,
                "aspect_ratio": request.aspect_ratio,
                "width": request.width,
                "height": request.height,
                "seed": outcome.seed,
                "filename": outcome.filename,
                "elapsed_ms": outcome.elapsed_ms,
                "bytes": outcome.stream.get("bytes_written", 0),
                "chunks": outcome.stream.get("chunks", 0),
                "sha256": outcome.integrity.get("sha256", ""),
                "integrity_code": outcome.integrity.get("code", ""),
                "gate2_code": outcome.gate2.get("code", ""),
                "qa_scorer": outcome.qa.get("scorer", ""),
                "qa_aesthetic": outcome.qa.get("aesthetic"),
                "qa_alignment": outcome.qa.get("alignment"),
                "error": outcome.error,
                "error_code": outcome.error_code,
                "attempts": len(outcome.transport.get("attempts", [])),
                "meta": outcome.meta,
            }
        )
