"""Stage 2 + Stage 4 - the network API gateway and the transport protocol.

Two rules from the blueprint drive this module:

1. `requests` applies NO timeout by default, so every call here passes the split
   tuple `(connect, read)`. The 3.05s connect budget absorbs one TCP packet
   retransmission; the 60s read budget accommodates the slow reverse-Markov
   denoising loop running on a remote GPU cluster.
2. Never load a high-res image entirely into RAM. Responses are streamed with
   `stream=True` and piped to disk in 65536-byte chunks.

The retry shield distinguishes the two failure modes: a ConnectTimeout means the
client cannot reach the server at all - FAIL FAST, do not wait. A ReadTimeout
means the connection is alive but inference is slow - keep it alive and retry.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

CHUNK_SIZE = 65536              # 64 KiB - the blueprint's chunk width
MAX_ASSET_BYTES = 64 * 1024 * 1024   # hard ceiling so a runaway stream cannot fill the disk
RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)

DEFAULT_CONNECT_TIMEOUT = 3.05
DEFAULT_READ_TIMEOUT = 60.0


class TransportError(RuntimeError):
    """A network failure that survived the retry shield."""

    def __init__(self, message: str, code: str = "transport_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Attempt:
    number: int
    outcome: str                 # ok | retrying | failed
    detail: str
    elapsed_ms: int
    status: int | None = None
    sleep_ms: int = 0


@dataclass
class TransportTrace:
    """Everything the UI needs to render what the gateway actually did."""

    attempts: list[Attempt] = field(default_factory=list)
    total_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": [vars(a) for a in self.attempts],
            "total_ms": self.total_ms,
        }


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter.

    Rule 1: give the remote infrastructure breathing room. Rule 2: randomize, so
    a swarm of clients does not re-converge on the gateway in lockstep.
    """
    window = min(cap, base * (2 ** attempt))
    return random.uniform(window / 2, window)


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
    max_retries: int = 3,
    stream: bool = True,
    on_event: Callable[[str, dict], None] | None = None,
    retry_status: Iterable[int] = RETRY_STATUS,
    **kwargs: Any,
) -> tuple[requests.Response, TransportTrace]:
    """Issue a request behind the split-timeout + backoff shield.

    Returns the live (unconsumed, when stream=True) response so the caller can
    pipe it to disk in chunks.
    """
    trace = TransportTrace()
    started_all = time.perf_counter()
    retry_status = tuple(retry_status)
    last_error: TransportError | None = None

    for attempt in range(max_retries + 1):
        started = time.perf_counter()
        try:
            response = session.request(
                method, url, timeout=timeout, stream=stream, **kwargs
            )
        except requests.exceptions.ConnectTimeout as exc:
            # NETWORK FAILURE - client could not establish a TCP connection.
            # Architecture rule: FAIL FAST. Do not wait.
            elapsed = int((time.perf_counter() - started) * 1000)
            trace.attempts.append(
                Attempt(attempt + 1, "failed", f"ConnectTimeout after {timeout[0]}s", elapsed)
            )
            trace.total_ms = int((time.perf_counter() - started_all) * 1000)
            raise TransportError(
                "Could not establish a TCP connection to the engine within "
                f"{timeout[0]}s. Check network routing, DNS, or a strict firewall.",
                code="ConnectTimeout",
            ) from exc
        except requests.exceptions.ReadTimeout as exc:
            # INFERENCE FAILURE - connection succeeded, the GPU cluster is slow.
            # Architecture rule: keep the connection alive longer, prepare to retry.
            elapsed = int((time.perf_counter() - started) * 1000)
            last_error = TransportError(
                f"The engine accepted the connection but sent no data within {timeout[1]}s. "
                "The GPU cluster is likely saturated.",
                code="ReadTimeout",
            )
            _record_retry(trace, attempt, max_retries, "ReadTimeout", elapsed, None, on_event)
            if attempt >= max_retries:
                break
            continue
        except requests.exceptions.SSLError as exc:
            raise TransportError(f"TLS handshake failed: {exc}", code="SSLError") from exc
        except requests.exceptions.ConnectionError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            last_error = TransportError(f"Connection dropped: {exc}", code="ConnectionError")
            _record_retry(trace, attempt, max_retries, "ConnectionError", elapsed, None, on_event)
            if attempt >= max_retries:
                break
            continue

        elapsed = int((time.perf_counter() - started) * 1000)

        # Rule 3: apply retries selectively, based on the HTTP response.
        if response.status_code in retry_status:
            hint = _retry_after_seconds(response)
            body = _peek(response)
            response.close()
            last_error = TransportError(
                f"Engine responded {response.status_code}. {body[:200]}",
                code=f"HTTP_{response.status_code}",
                status=response.status_code,
            )
            _record_retry(
                trace, attempt, max_retries, f"HTTP {response.status_code}",
                elapsed, response.status_code, on_event, forced_sleep=hint,
            )
            if attempt >= max_retries:
                break
            continue

        trace.attempts.append(
            Attempt(attempt + 1, "ok", f"HTTP {response.status_code}", elapsed, response.status_code)
        )
        trace.total_ms = int((time.perf_counter() - started_all) * 1000)
        return response, trace

    trace.total_ms = int((time.perf_counter() - started_all) * 1000)
    raise last_error or TransportError("Request failed for an unknown reason.")


def _record_retry(
    trace: TransportTrace,
    attempt: int,
    max_retries: int,
    detail: str,
    elapsed_ms: int,
    status: int | None,
    on_event: Callable[[str, dict], None] | None,
    forced_sleep: float | None = None,
) -> None:
    if attempt >= max_retries:
        trace.attempts.append(Attempt(attempt + 1, "failed", detail, elapsed_ms, status))
        return
    delay = forced_sleep if forced_sleep is not None else backoff_delay(attempt)
    trace.attempts.append(
        Attempt(attempt + 1, "retrying", detail, elapsed_ms, status, int(delay * 1000))
    )
    if on_event:
        on_event(
            "retry",
            {"attempt": attempt + 1, "detail": detail, "sleep_ms": int(delay * 1000)},
        )
    time.sleep(delay)


def _peek(response: requests.Response, limit: int = 400) -> str:
    """Read a small slice of an error body without loading the whole thing."""
    try:
        chunk = next(response.iter_content(limit), b"")
        return chunk.decode("utf-8", errors="replace")
    except Exception:
        return ""


@dataclass
class StreamResult:
    path: Path
    bytes_written: int
    chunks: int
    content_type: str
    elapsed_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bytes_written": self.bytes_written,
            "chunks": self.chunks,
            "content_type": self.content_type,
            "elapsed_ms": self.elapsed_ms,
        }


def stream_to_file(
    response: requests.Response,
    destination: Path,
    chunk_size: int = CHUNK_SIZE,
    max_bytes: int = MAX_ASSET_BYTES,
    on_progress: Callable[[int], None] | None = None,
) -> StreamResult:
    """Pipe a live response body straight to disk, never holding it all in RAM."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    written = 0
    chunks = 0
    try:
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:      # keep-alive filler, not payload
                    continue
                written += len(chunk)
                chunks += 1
                if written > max_bytes:
                    raise TransportError(
                        f"Asset exceeded the {max_bytes // (1024 * 1024)} MiB safety ceiling; "
                        "stream aborted.",
                        code="asset_too_large",
                    )
                handle.write(chunk)
                if on_progress:
                    on_progress(written)
    except TransportError:
        destination.unlink(missing_ok=True)
        raise
    except requests.exceptions.ChunkedEncodingError as exc:
        # The connection died mid-download - exactly the truncated-stream case
        # Stage 5 exists to catch. Keep the partial file so integrity can report it.
        raise TransportError(
            f"The binary stream was cut off mid-download: {exc}", code="truncated_stream"
        ) from exc
    finally:
        response.close()

    return StreamResult(
        path=destination,
        bytes_written=written,
        chunks=chunks,
        content_type=response.headers.get("Content-Type", "application/octet-stream"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def write_bytes(data: bytes, destination: Path, chunk_size: int = CHUNK_SIZE) -> StreamResult:
    """Same disk-writing contract for engines that hand back base64 instead of a stream."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    chunks = 0
    with open(destination, "wb") as handle:
        for offset in range(0, len(data), chunk_size):
            handle.write(data[offset:offset + chunk_size])
            chunks += 1
    return StreamResult(
        path=destination,
        bytes_written=len(data),
        chunks=max(chunks, 1),
        content_type="image/png",
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def build_session(user_agent: str = "MultimodalImageStudio/1.0") -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "identity"})
    return session
