"""Google Gemini image generation - free tier via AI Studio.

Returns base64 inline data inside a JSON envelope, so the bytes are decoded in
memory and then written through the same chunked disk-writer as every other
engine. Aspect ratio is passed as a ratio string; the engine picks the exact
bucket, which Stage 5 then reconciles against what was requested.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

import requests

from ..params import GenerationRequest
from ..transport import request_with_retry
from .base import Provider, ProviderError, ProviderResult

DEFAULT_MODEL = "gemini-2.5-flash-image"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini (Nano Banana)"
    env_key = "GEMINI_API_KEY"
    max_prompt_chars = 4000
    supports_negative = False       # expressed in-band
    supports_seed = False
    free = True
    notes = "Free-tier key from aistudio.google.com/app/apikey. Returns base64 inline image data."
    key_url = "https://aistudio.google.com/app/apikey"
    key_hint = "AIza..."
    verify_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def verify_headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key}

    def verify_summary(self, payload: Any) -> str:
        models = (payload or {}).get("models") or []
        return f"{len(models)} model(s) visible to this key." if models else ""

    def _model(self) -> str:
        return str(self.options.get("model") or DEFAULT_MODEL)

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        text = prompt
        if negative:
            text = f"{prompt}. Strictly avoid: {negative}"
        return {
            "method": "POST",
            "url": ENDPOINT.format(model=self._model()),
            "json": {
                "contents": [{"parts": [{"text": text[: self.max_prompt_chars]}]}],
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "imageConfig": {"aspectRatio": request.aspect_ratio},
                },
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
            raise ProviderError(
                "Gemini needs an API key. Add one under Engine -> Manage keys.", code="missing_key"
            )

        payload = self.build_payload(request, prompt, negative, seed)
        response, trace = request_with_retry(
            session,
            "POST",
            payload["url"],
            json=payload["json"],
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            timeout=request.timeout,
            max_retries=request.max_retries,
            stream=False,
            on_event=on_event,
            deadline=request.deadline,
        )

        try:
            envelope = response.json()
        except ValueError as exc:
            raise ProviderError("Engine returned a non-JSON envelope.", code="bad_envelope") from exc
        finally:
            response.close()

        if response.status_code != 200:
            message = (envelope.get("error") or {}).get("message", "unknown error")
            raise self._auth_error(response.status_code, message)

        candidates = envelope.get("candidates") or []
        if not candidates:
            block = (envelope.get("promptFeedback") or {}).get("blockReason", "")
            raise ProviderError(
                f"No candidate returned. blockReason={block or 'unspecified'}",
                code="moderation_blocked" if block else "empty_response",
            )

        candidate = candidates[0]
        finish = str(candidate.get("finishReason", ""))
        for part in candidate.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return ProviderResult(
                    raw=base64.b64decode(blob["data"]),
                    transport=trace,
                    meta={
                        "engine_model": self._model(),
                        "mime": blob.get("mimeType") or blob.get("mime_type", "image/png"),
                        "finish_reason": finish,
                        "endpoint": payload["url"],
                    },
                )

        raise ProviderError(
            f"Candidate contained no image part (finishReason={finish or 'unset'}).",
            code="finish_reason=FILTER" if finish.upper() in ("SAFETY", "PROHIBITED_CONTENT") else "empty_response",
        )
