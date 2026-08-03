"""Engine registry and auto-selection.

`resolve("auto")` walks the preference order and returns the first engine that is
actually usable with the keys present in the environment. Pollinations needs no
key at all, so "auto" always lands on something that works.
"""

from __future__ import annotations

import os

from .base import Provider, ProviderError, ProviderResult
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
AUTO_ORDER = ("pollinations", "gemini", "huggingface", "stability", "openai", "mock")


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


def build(name: str) -> Provider:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ProviderError(
            f"Unknown engine {name!r}. Available: {', '.join(REGISTRY)}", code="unknown_provider"
        ) from None
    api_key = os.getenv(cls.env_key, "") if cls.env_key else ""
    return cls(api_key=api_key, **_options_for(name))


def resolve(name: str = "auto") -> Provider:
    if name and name != "auto":
        provider = build(name)
        if not provider.available:
            raise ProviderError(
                f"{provider.label} needs {provider.env_key} in the environment.",
                code="missing_key",
            )
        return provider

    preferred = os.getenv("IMAGESTUDIO_PROVIDER", "").strip().lower()
    order = ([preferred] if preferred and preferred != "auto" else []) + list(AUTO_ORDER)
    for candidate in order:
        if candidate not in REGISTRY:
            continue
        provider = build(candidate)
        if provider.available:
            return provider
    return build("mock")


def catalogue() -> list[dict]:
    """Every engine plus whether it is usable right now - drives the UI selector."""
    return [build(name).describe() for name in REGISTRY]


__all__ = [
    "Provider",
    "ProviderError",
    "ProviderResult",
    "REGISTRY",
    "AUTO_ORDER",
    "build",
    "resolve",
    "catalogue",
]
