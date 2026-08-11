"""The automated download pipeline - local asset library plus an append-only manifest.

Every asset that survives Stage 5 lands in `assets/` with a deterministic,
sortable filename, and one JSON line is appended to `assets/manifest.jsonl`
recording the full provenance: prompt, exact payload, engine, transport trace,
integrity readout and QA scores.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MANIFEST_NAME = "manifest.jsonl"
_write_lock = threading.Lock()

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

_resolved_root: Path | None = None
_resolved_is_fallback = False
_resolved_for_override: str | None = None


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _fallback_root() -> Path:
    """The last-resort scratch directory. Writable everywhere, persistent nowhere."""
    fallback = Path(tempfile.gettempdir()) / "emulsion-assets"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def assets_root() -> Path:
    """Where generated assets and the manifest live.

    Serverless platforms (Vercel, AWS Lambda) deploy the project as a read-only
    bundle - only /tmp is writable there, and it does not persist across cold
    starts. Every candidate is probed with a real write before it is accepted,
    so a deploy degrades to a temp directory instead of crashing Stage 4; set
    IMAGESTUDIO_ASSETS to point at real persistent storage instead.
    """
    global _resolved_root, _resolved_is_fallback, _resolved_for_override
    override = os.getenv("IMAGESTUDIO_ASSETS") or ""
    # The probe costs a real write, so the answer is cached - but keyed on the
    # override, so re-pointing IMAGESTUDIO_ASSETS takes effect immediately.
    if _resolved_root is not None and _resolved_for_override == override:
        return _resolved_root

    candidate = (
        Path(override).expanduser()
        if override
        else Path(__file__).resolve().parent.parent / "assets"
    )

    if _is_writable(candidate):
        _resolved_root, _resolved_is_fallback = candidate, False
    else:
        _resolved_root, _resolved_is_fallback = _fallback_root(), True
    _resolved_for_override = override
    return _resolved_root


def is_ephemeral() -> bool:
    """True when assets live in a scratch directory that will not survive a restart."""
    assets_root()
    return _resolved_is_fallback


def reset_root_cache() -> None:
    """Forget the resolved root so the next call re-probes. Used by tests."""
    global _resolved_root, _resolved_is_fallback, _resolved_for_override
    _resolved_root, _resolved_is_fallback, _resolved_for_override = None, False, None


def slugify(text: str, max_words: int = 6) -> str:
    words = _SLUG_STRIP.sub("-", text.lower()).strip("-").split("-")
    words = [w for w in words if w][:max_words]
    return "-".join(words) or "untitled"


def asset_filename(prompt: str, seed: int, index: int, suffix: str = ".png") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{slugify(prompt)}_s{seed}_{index:02d}{suffix}"


def manifest_path() -> Path:
    return assets_root() / MANIFEST_NAME


def record(entry: dict[str, Any]) -> bool:
    """Append one provenance line. Thread-safe; a batch writes several.

    Bookkeeping must never cost an image: on a read-only deploy the manifest is
    simply skipped and the asset the caller already produced still stands.
    """
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        with _write_lock:
            with open(manifest_path(), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except OSError:
        return False


def read_manifest(limit: int = 60) -> list[dict[str, Any]]:
    """Most recent entries first. Skips lines that predate a schema change."""
    path = manifest_path()
    try:
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    entries.reverse()
    return entries[:limit]


def iter_assets() -> Iterator[Path]:
    try:
        candidates = sorted(assets_root().glob("*"), reverse=True)
    except OSError:
        return
    for path in candidates:
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            yield path


EPHEMERAL_NOTE = (
    "This deploy has no persistent disk, so the library resets whenever the "
    "instance goes cold. Point IMAGESTUDIO_ASSETS at mounted storage to keep it."
)


def library_stats() -> dict[str, Any]:
    total = 0
    count = 0
    for path in iter_assets():
        try:
            total += path.stat().st_size
        except OSError:
            continue
        count += 1
    ephemeral = is_ephemeral()
    return {
        "assets": count,
        "bytes": total,
        "megabytes": round(total / (1024 * 1024), 2),
        "root": str(assets_root()),
        "ephemeral": ephemeral,
        "note": EPHEMERAL_NOTE if ephemeral else "",
    }


def purge_rejected(entry_path: Path) -> None:
    """Discard an asset that failed integrity or the QA gate."""
    try:
        entry_path.unlink(missing_ok=True)
    except OSError:
        pass
