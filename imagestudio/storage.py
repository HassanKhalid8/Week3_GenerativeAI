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
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MANIFEST_NAME = "manifest.jsonl"
_write_lock = threading.Lock()

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def assets_root() -> Path:
    root = Path(os.getenv("IMAGESTUDIO_ASSETS", "")).expanduser() if os.getenv("IMAGESTUDIO_ASSETS") else \
        Path(__file__).resolve().parent.parent / "assets"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(text: str, max_words: int = 6) -> str:
    words = _SLUG_STRIP.sub("-", text.lower()).strip("-").split("-")
    words = [w for w in words if w][:max_words]
    return "-".join(words) or "untitled"


def asset_filename(prompt: str, seed: int, index: int, suffix: str = ".png") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{slugify(prompt)}_s{seed}_{index:02d}{suffix}"


def manifest_path() -> Path:
    return assets_root() / MANIFEST_NAME


def record(entry: dict[str, Any]) -> None:
    """Append one provenance line. Thread-safe; a batch writes several."""
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _write_lock:
        with open(manifest_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_manifest(limit: int = 60) -> list[dict[str, Any]]:
    """Most recent entries first. Skips lines that predate a schema change."""
    path = manifest_path()
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
    entries.reverse()
    return entries[:limit]


def iter_assets() -> Iterator[Path]:
    for path in sorted(assets_root().glob("*"), reverse=True):
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            yield path


def library_stats() -> dict[str, Any]:
    files = list(iter_assets())
    total = sum(f.stat().st_size for f in files)
    return {
        "assets": len(files),
        "bytes": total,
        "megabytes": round(total / (1024 * 1024), 2),
        "root": str(assets_root()),
    }


def purge_rejected(entry_path: Path) -> None:
    """Discard an asset that failed integrity or the QA gate."""
    try:
        entry_path.unlink(missing_ok=True)
    except OSError:
        pass
