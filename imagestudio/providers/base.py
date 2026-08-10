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


#: Error codes that mean "the credential is the problem", not "the prompt is".
AUTH_ERROR_CODES = frozenset(
    {"missing_key", "invalid_api_key", "key_forbidden", "insufficient_credits", "rate_limited"}
)

# Body signatures each vendor uses, checked before the status code so a 400 that
# is really an auth problem is still classified correctly.
_CREDIT_SIGNS = ("insufficient_credit", "insufficient credits", "billing", "quota", "payment")
_INVALID_SIGNS = ("invalid_api_key", "invalid api key", "incorrect api key", "api key not valid",
                  "unauthorized", "invalid authentication", "invalid token")


def classify_auth_error(status: int, body: str, label: str) -> tuple[str, str] | None:
    """Turn a credential-shaped HTTP failure into a code plus a sentence to act on.

    Returns None when the failure is not about the key, so the caller keeps its
    own more specific classification (moderation refusals, empty envelopes).
    """
    lowered = (body or "").lower()

    if status == 401 or any(sign in lowered for sign in _INVALID_SIGNS):
        return (
            "invalid_api_key",
            f"{label} rejected the API key (HTTP {status or 401}). Open Manage keys, "
            "check for a stray space or a copied-in-full key, and test it again.",
        )
    if status in (402, 429) and any(sign in lowered for sign in _CREDIT_SIGNS):
        return (
            "insufficient_credits",
            f"{label} accepted the key but the account is out of credit or over its "
            "quota. Top up the account or switch to a free engine.",
        )
    if status == 402:
        return (
            "insufficient_credits",
            f"{label} requires payment for this request (HTTP 402). Check the account balance.",
        )
    if status == 403:
        return (
            "key_forbidden",
            f"{label} accepted the key but refused this call (HTTP 403) - the key most "
            "likely lacks permission for image generation, or the model is not enabled "
            "for this account or region.",
        )
    if status == 429:
        return (
            "rate_limited",
            f"{label} is rate-limiting this key (HTTP 429). Wait a moment, or use a "
            "different engine for now.",
        )
    return None


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
    #: where a user gets a key, and what one looks like - shown in the key vault
    key_url: str = ""
    key_hint: str = ""

    def __init__(self, api_key: str = "", key_source: str = "", **options: Any):
        self.api_key = api_key or ""
        #: "user" (pasted in this browser), "env" (server configured), or "" (none)
        self.key_source = key_source if self.api_key else ""
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
            "needs_key": bool(self.env_key),
            "key_url": self.key_url,
            "key_hint": self.key_hint,
            "key_source": self.key_source,
        }

    def _auth_error(self, status: int, body: str, fallback_code: str = "unexpected_response") -> ProviderError:
        """Build the ProviderError for a non-image response from this engine."""
        classified = classify_auth_error(status, body, self.label)
        if classified:
            code, message = classified
            return ProviderError(message, code=code, status=status)
        return ProviderError(
            f"{self.label} returned {status}: {(body or 'no response body').strip()[:250]}",
            code=fallback_code,
            status=status,
        )

    # -- key validation --------------------------------------------------
    #: authenticated GET that proves a key works, without spending a generation
    verify_url: str = ""

    def verify_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def verify_summary(self, payload: Any) -> str:
        """One line of detail from a successful probe - account name, credits, etc."""
        return ""

    def verify_key(self, session: requests.Session, timeout: tuple[float, float] = (3.05, 12.0)) -> dict[str, Any]:
        """Probe the key against the live engine. Never raises - always a verdict."""
        if not self.env_key:
            return {"ok": True, "code": "no_key_required", "message": f"{self.label} needs no API key.", "detail": ""}
        if not self.api_key:
            return {"ok": False, "code": "missing_key", "message": "Enter a key first.", "detail": ""}
        if not self.verify_url:
            return {
                "ok": True,
                "code": "unverified",
                "message": "Key saved. This engine has no cheap probe, so it is checked on the first generation.",
                "detail": "",
            }

        try:
            response = session.get(self.verify_url, headers=self.verify_headers(), timeout=timeout)
        except requests.exceptions.RequestException as exc:
            return {
                "ok": False,
                "code": "network_error",
                "message": f"Could not reach {self.label} to check the key: {exc}",
                "detail": "",
            }

        body = response.text[:600]
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return {
                "ok": True,
                "code": "valid",
                "message": f"{self.label} accepted this key.",
                "detail": self.verify_summary(payload),
            }

        error = self._auth_error(response.status_code, body)
        return {"ok": False, "code": error.code, "message": str(error), "detail": ""}

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
