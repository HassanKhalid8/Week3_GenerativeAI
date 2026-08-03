"""Stage 5 - integrity verification via a forced pixel-level decode.

The trap: `imghdr.what()` and `Image.verify()` only read the block structure and
CRC headers at the very beginning of a file. Both report success on an asset
whose connection dropped halfway through - a silent, truncated disaster where the
top half is artwork and the bottom half is noise.

The fix: call `Image.open(path).load()`, which forces Pillow to decode the entire
stream pixel by pixel. A file that was cut off short raises
`OSError: image file is truncated` / `broken data stream`, and the pipeline can
discard the corrupted asset and request a retry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image, ImageFile

# Explicitly refuse Pillow's forgiving mode. If a stream is short we want the
# OSError, not a silently zero-filled bottom edge.
ImageFile.LOAD_TRUNCATED_IMAGES = False

MAGIC_BYTES: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"RIFF": "WEBP",
    b"GIF87a": "GIF",
    b"GIF89a": "GIF",
}


@dataclass
class IntegrityReport:
    ok: bool
    stage: str            # magic | header | pixel_decode | dimensions
    code: str
    message: str
    fmt: str = ""
    width: int = 0
    height: int = 0
    bytes_on_disk: int = 0
    sha256: str = ""
    dimension_match: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path, chunk_size: int = 65536) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sniff_magic(path: Path) -> str:
    with open(path, "rb") as handle:
        head = handle.read(12)
    for signature, name in MAGIC_BYTES.items():
        if head.startswith(signature):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return ""


def verify_asset(
    path: Path,
    expected_width: int = 0,
    expected_height: int = 0,
) -> IntegrityReport:
    """Run the full three-readout verification chain on a downloaded asset."""
    if not path.exists():
        return IntegrityReport(False, "magic", "missing_file", "The asset never reached disk.")

    size = path.stat().st_size
    if size == 0:
        return IntegrityReport(
            False, "magic", "empty_file", "The stream produced a zero-byte file.", bytes_on_disk=0
        )

    # Readout 0: magic bytes. Catches an HTML error page saved with an image name.
    fmt = sniff_magic(path)
    if not fmt:
        with open(path, "rb") as handle:
            head = handle.read(80)
        hint = head.decode("utf-8", errors="replace").strip()[:60]
        return IntegrityReport(
            False,
            "magic",
            "not_an_image",
            f"No recognized image signature. The body starts with: {hint!r}",
            bytes_on_disk=size,
        )

    # Readout 1 + 2: header structure and CRC. Necessary, and famously insufficient.
    # A chunk-walking format (PNG) can surface truncation here; a scan-based one
    # (JPEG) sails straight through and only fails at the pixel decode below.
    try:
        with Image.open(path) as probe:
            probe.verify()
    except Exception as exc:
        truncated = "truncat" in str(exc).lower()
        return IntegrityReport(
            False,
            "header",
            "broken_data_stream" if truncated else "corrupt_header",
            (
                f"The download was cut off mid-stream ({exc}). Asset discarded."
                if truncated
                else f"Header/CRC check failed: {exc}"
            ),
            fmt=fmt,
            bytes_on_disk=size,
        )

    # Readout 3: the one that actually matters. Full pixel decode.
    try:
        with Image.open(path) as image:
            image.load()                     # forces every scanline through the decoder
            width, height = image.size
            image_format = image.format or fmt
    except OSError as exc:
        return IntegrityReport(
            False,
            "pixel_decode",
            "broken_data_stream",
            f"Pixel decode aborted - the download was truncated ({exc}). Asset discarded.",
            fmt=fmt,
            bytes_on_disk=size,
        )
    except Exception as exc:
        return IntegrityReport(
            False, "pixel_decode", "decode_failure", f"Decoder raised {type(exc).__name__}: {exc}",
            fmt=fmt, bytes_on_disk=size,
        )

    matches = True
    message = "Full pixel decode succeeded; asset is byte-complete."
    if expected_width and expected_height and (width, height) != (expected_width, expected_height):
        matches = False
        message = (
            f"Decoded cleanly, but the engine returned {width}x{height} instead of the "
            f"requested {expected_width}x{expected_height}."
        )

    return IntegrityReport(
        ok=True,
        stage="pixel_decode",
        code="verified",
        message=message,
        fmt=image_format,
        width=width,
        height=height,
        bytes_on_disk=size,
        sha256=sha256_file(path),
        dimension_match=matches,
    )
