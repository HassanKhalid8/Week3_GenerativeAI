"""OpenAI gpt-image series.

Per the engine matrix: DALL-E 3 was retired in March 2026, so this integrates
`gpt-image-1` - a 4,000 character prompt ceiling and a Base64 JSON (`b64_json`)
output format rather than a binary stream. There is no negative-prompt field and
no seed, so both are expressed in-band.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

import requests

from ..params import GenerationRequest
from ..transport import request_with_retry
from .base import Provider, ProviderError, ProviderResult

ENDPOINT = "https://api.openai.com/v1/images/generations"
# The engine accepts only these three canvases; anything else fails the handshake.
SUPPORTED_SIZES = {
    "1:1": "1024x1024",
    "landscape": "1536x1024",
    "portrait": "1024x1536",
}


class OpenAIImageProvider(Provider):
    name = "openai"
    label = "OpenAI gpt-image-1"
    env_key = "OPENAI_API_KEY"
    max_prompt_chars = 4000
    supports_negative = False
    supports_seed = False
    free = False
    notes = "Paid. DALL-E 3 retired March 2026; migrated to gpt-image. Returns b64_json."

    def _size(self, request: GenerationRequest) -> str:
        if request.aspect_ratio == "1:1":
            return SUPPORTED_SIZES["1:1"]
        return SUPPORTED_SIZES["landscape"] if request.width >= request.height else SUPPORTED_SIZES["portrait"]

    def clamp_size(self, width: int, height: int) -> tuple[int, int]:
        if width == height:
            return 1024, 1024
        return (1536, 1024) if width > height else (1024, 1536)

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        text = prompt
        if negative:
            text = f"{prompt}. Do not include: {negative}"
        return {
            "method": "POST",
            "url": ENDPOINT,
            "json": {
                "model": str(self.options.get("model") or "gpt-image-1"),
                "prompt": text[: self.max_prompt_chars],
                "size": self._size(request),
                "n": 1,                      # batching is handled by our own loop
                "quality": str(self.options.get("quality") or "high"),
            },
        }

    def submit(
        self,
        session: requests.Session,
        request: GenerationRequest,
        prompt: str,
        negative: str,
        seed: int,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> ProviderResult:
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY is not set.", code="missing_key")

        payload = self.build_payload(request, prompt, negative, seed)
        response, trace = request_with_retry(
            session,
            "POST",
            payload["url"],
            json=payload["json"],
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=request.timeout,
            max_retries=request.max_retries,
            stream=False,
            on_event=on_event,
        )

        try:
            envelope = response.json()
        except ValueError as exc:
            raise ProviderError("Engine returned a non-JSON envelope.", code="bad_envelope") from exc
        finally:
            response.close()

        if response.status_code != 200:
            error = envelope.get("error") or {}
            code = str(error.get("code") or "unexpected_response")
            raise ProviderError(
                f"Engine returned {response.status_code}: {error.get('message', 'unknown error')}",
                code="content_policy_violation" if "policy" in code else code,
                status=response.status_code,
            )

        items = envelope.get("data") or []
        if not items or not items[0].get("b64_json"):
            raise ProviderError("Envelope carried no b64_json payload.", code="empty_response")

        return ProviderResult(
            raw=base64.b64decode(items[0]["b64_json"]),
            transport=trace,
            meta={
                "engine_model": payload["json"]["model"],
                "size": payload["json"]["size"],
                "endpoint": ENDPOINT,
                "usage": envelope.get("usage", {}),
            },
        )
