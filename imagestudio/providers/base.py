"""The engine contract.

Every backend in the multimodal engine matrix returns its bytes differently:
gpt-image hands back base64 JSON, Stable Image Core returns raw image bytes, and
the free engines redirect to a public URL. `ProviderResult` normalizes all three
into something Stage 4 can stream to disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from ..params import GenerationRequest


@dataclass
class ProviderResult:
    """One of three shapes, exactly one populated."""

    stream: requests.Response | None = None   # live body -> stream_to_file()
    raw: bytes | None = None                  # already-decoded bytes -> write_bytes()
    url: str | None = None                    # public asset URL -> fetch, then stream
    transport: Any = None                     # TransportTrace from the submitting call
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "provider_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class Provider(ABC):
    #: registry key
    name: str = "base"
    #: human label for the UI
    label: str = "Base"
    #: env var that unlocks it; empty string means no key at all
    env_key: str = ""
    #: engine-enforced prompt ceiling, from the engine matrix
    max_prompt_chars: int = 4000
    #: does the engine accept an explicit negative prompt field?
    supports_negative: bool = True
    #: does the engine accept an explicit seed?
    supports_seed: bool = True
    #: free to call with the key (or lack of key) it needs?
    free: bool = False
    notes: str = ""

    def __init__(self, api_key: str = "", **options: Any):
        self.api_key = api_key or ""
        self.options = options

    @property
    def available(self) -> bool:
        return bool(self.api_key) or not self.env_key

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "env_key": self.env_key,
            "max_prompt_chars": self.max_prompt_chars,
            "supports_negative": self.supports_negative,
            "supports_seed": self.supports_seed,
            "free": self.free,
            "available": self.available,
            "notes": self.notes,
        }

    def clamp_size(self, width: int, height: int) -> tuple[int, int]:
        """Map an exact pixel payload onto whatever this engine actually accepts."""
        return width, height

    @abstractmethod
    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        """The serialized payload, returned for display in the trace panel."""

    @abstractmethod
    def submit(
        self,
        session: requests.Session,
        request: GenerationRequest,
        prompt: str,
        negative: str,
        seed: int,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> ProviderResult:
        """Execute one generation and hand back a streamable result."""
