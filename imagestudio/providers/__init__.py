"""Engine registry and auto-selection.

`resolve("auto")` walks the preference order and returns the first engine that is
actually usable with the keys available. Pollinations needs no key at all, so
"auto" always lands on something that works.

Keys come from one of two places, in this order:

1. A per-request `keys` mapping - a key the visitor pasted into the studio's key
   vault, which lives in *their* browser and rides along with the one request
   that uses it. It is never assigned to a module global, never cached, and
   never written anywhere.
2. The server's own environment, for a deploy the operator configured.

Everything here therefore takes `keys` as an argument rather than reading state.
"""

from __future__ import annotations

import os
from typing import Mapping

from .base import AUTH_ERROR_CODES, Provider, ProviderError, ProviderResult, classify_auth_error
from .gemini import GeminiProvider
from .huggingface import HuggingFaceProvider
from .mock import MockProvider
from .openai_images import OpenAIImageProvider
from .pollinations import PollinationsProvider
from .stability import StabilityProvider

REGISTRY: dict[str, type[Provider]] = {
    PollinationsProvider.name: PollinationsProvider,
    GeminiProvider.name: GeminiProvider,
    HuggingFaceProvider.name: HuggingFaceProvider,
    StabilityProvider.name: StabilityProvider,
    OpenAIImageProvider.name: OpenAIImageProvider,
    MockProvider.name: MockProvider,
}

# Free and key-less first, then free-with-key, then paid, then offline.
AUTO_ORDER = ("pollinations", "huggingface", "gemini", "stability", "openai", "mock")


def _options_for(name: str) -> dict:
    prefix = f"IMAGESTUDIO_{name.upper()}_"
    options = {
        key[len(prefix):].lower(): value
        for key, value in os.environ.items()
        if key.startswith(prefix) and value
    }
    if name == "pollinations" and os.getenv("POLLINATIONS_TOKEN"):
        options["token"] = os.environ["POLLINATIONS_TOKEN"]
    return options


def build(name: str, keys: Mapping[str, str] | None = None) -> Provider:
    """Instantiate one engine, preferring a caller-supplied key over the environment."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ProviderError(
            f"Unknown engine {name!r}. Available: {', '.join(REGISTRY)}", code="unknown_provider"
        ) from None

    api_key, source = "", ""
    if cls.env_key:
        supplied = ((keys or {}).get(name) or "").strip()
        if supplied:
            api_key, source = supplied, "user"
        else:
            api_key = os.getenv(cls.env_key, "")
            source = "env" if api_key else ""
    return cls(api_key=api_key, key_source=source, **_options_for(name))


def resolve(name: str = "auto", keys: Mapping[str, str] | None = None) -> Provider:
    if name and name != "auto":
        provider = build(name, keys)
        if not provider.available:
            raise ProviderError(
                f"{provider.label} needs an API key. Add one under Engine -> Manage keys, "
                f"or set {provider.env_key} on the server.",
                code="missing_key",
            )
        return provider

    preferred = os.getenv("IMAGESTUDIO_PROVIDER", "").strip().lower()
    order = ([preferred] if preferred and preferred != "auto" else []) + list(AUTO_ORDER)
    for candidate in order:
        if candidate not in REGISTRY:
            continue
        provider = build(candidate, keys)
        if provider.available:
            return provider
    return build("mock", keys)


def catalogue(keys: Mapping[str, str] | None = None) -> list[dict]:
    """Every engine plus whether it is usable right now - drives the UI selector."""
    return [build(name, keys).describe() for name in REGISTRY]


__all__ = [
    "Provider",
    "ProviderError",
    "ProviderResult",
    "AUTH_ERROR_CODES",
    "classify_auth_error",
    "REGISTRY",
    "AUTO_ORDER",
    "build",
    "resolve",
    "catalogue",
]
