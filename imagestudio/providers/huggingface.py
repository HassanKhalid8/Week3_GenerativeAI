"""Hugging Face Inference - free tier, free token.

POST a JSON payload, receive `image/*` raw bytes back. A cold model answers 503
with an `estimated_time` field, which is precisely the case the retry shield's
selective 503 handling exists for.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests

from ..params import GenerationRequest
from ..transport import request_with_retry
from .base import Provider, ProviderError, ProviderResult

DEFAULT_MODEL = "black-forest-labs/FLUX.1-schnell"
DEFAULT_BASE = "https://router.huggingface.co/hf-inference/models"


class HuggingFaceProvider(Provider):
    name = "huggingface"
    label = "Hugging Face Inference"
    env_key = "HF_TOKEN"
    max_prompt_chars = 1000
    supports_negative = True
    supports_seed = True
    free = True
    notes = "Free token from huggingface.co/settings/tokens. Returns raw image bytes."

    def _model(self) -> str:
        return str(self.options.get("model") or DEFAULT_MODEL)

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "width": request.width,
            "height": request.height,
        }
        if negative:
            parameters["negative_prompt"] = negative
        if seed is not None:
            parameters["seed"] = seed
        return {
            "method": "POST",
            "url": f"{self.options.get('base_url') or DEFAULT_BASE}/{self._model()}",
            "json": {"inputs": prompt[: self.max_prompt_chars], "parameters": parameters},
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
            raise ProviderError("HF_TOKEN is not set.", code="missing_key")

        payload = self.build_payload(request, prompt, negative, seed)
        response, trace = request_with_retry(
            session,
            "POST",
            payload["url"],
            json=payload["json"],
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "image/*",
                "Content-Type": "application/json",
            },
            timeout=request.timeout,
            max_retries=request.max_retries,
            stream=True,
            on_event=on_event,
        )

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            body = b""
            try:
                body = next(response.iter_content(600), b"")
            finally:
                response.close()
            text = body.decode("utf-8", "replace")
            message = text[:250]
            try:
                message = json.loads(text).get("error", message)
            except Exception:
                pass
            raise ProviderError(
                f"Engine returned {response.status_code}: {message}",
                code="unexpected_response",
                status=response.status_code,
            )

        return ProviderResult(
            stream=response,
            transport=trace,
            meta={"engine_model": self._model(), "seed": seed, "endpoint": payload["url"]},
        )
