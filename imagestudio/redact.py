"""Secret scrubbing for anything that leaves the server.

A user-supplied API key travels exactly one hop: browser -> this process -> the
remote engine. It is never persisted. The remaining leak surface is *echoed
text* - an engine that quotes the offending Authorization header back in its
error body, a stack trace, a gateway log line. Everything the studio sends to
the browser passes through `scrub()` first, so a key can never round-trip into
the UI, the manifest, or a screenshot pasted into a bug report.

Two layers, because either alone is insufficient:

1. Exact-value removal for the keys we were actually handed this request.
2. Shape matching for well-known key formats, which also covers a key the
   operator put in the server's own environment.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

MASK = "••••"

# Prefixed formats published by each vendor. Deliberately anchored on the
# prefix - a bare hex blob is indistinguishable from a sha256 digest, and the
# manifest is full of those.
_SHAPES = re.compile(
    r"""(
        sk-[A-Za-z0-9_\-]{16,}          # OpenAI (and Stability's sk- keys)
      | hf_[A-Za-z0-9]{16,}             # Hugging Face
      | AIza[A-Za-z0-9_\-]{20,}         # Google AI Studio / Gemini
      | sk-ant-[A-Za-z0-9_\-]{16,}      # Anthropic
    )""",
    re.VERBOSE,
)


def mask(secret: str) -> str:
    """A stable, non-reversible stand-in that still identifies which key it was."""
    tail = secret[-4:] if len(secret) >= 8 else ""
    return f"{MASK}{tail}"


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace every known or key-shaped secret in `text` with a mask."""
    if not text:
        return text
    # Longest first, so a key that contains another as a substring is not
    # partially masked into an unrecognizable fragment.
    for secret in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, mask(secret))
    return _SHAPES.sub(lambda m: mask(m.group(0)), text)


def scrub_deep(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively scrub every string inside a JSON-shaped structure."""
    secrets = tuple(secrets)
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, Mapping):
        return {key: scrub_deep(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_deep(item, secrets) for item in value]
    return value
