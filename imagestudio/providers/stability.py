"""Stable Image Core (Stability AI) - the engine matrix's REST v2beta entry.

Multipart form submission, 10,000 character prompt ceiling, and `Accept: image/*`
to get RAW image bytes back instead of base64 JSON. Sizing is expressed as an
aspect ratio string rather than exact pixels, so `clamp_size` maps our exact
payload onto the nearest supported ratio.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from ..params import GenerationRequest
from ..transport import request_with_retry
from .base import Provider, ProviderError, ProviderResult

ENDPOINT = "https://api.stability.ai/v2beta/stable-image/generate/{model}"
SUPPORTED_RATIOS = ("16:9", "1:1", "21:9", "2:3", "3:2", "4:5", "5:4", "9:16", "9:21")


class StabilityProvider(Provider):
    name = "stability"
    label = "Stability AI - Stable Image Core"
    env_key = "STABILITY_API_KEY"
    max_prompt_chars = 10000
    supports_negative = True
    supports_seed = True
    free = False
    notes = "Paid credits (small free trial grant). Optimized for fast, high-quality v2beta iteration."
    key_url = "https://platform.stability.ai/account/keys"
    key_hint = "sk-..."
    verify_url = "https://api.stability.ai/v1/user/balance"

    def verify_summary(self, payload: Any) -> str:
        credits = (payload or {}).get("credits")
        return f"{credits:.2f} credits remaining." if isinstance(credits, (int, float)) else ""

    def _model(self) -> str:
        return str(self.options.get("model") or "core")

    def _ratio(self, request: GenerationRequest) -> str:
        if request.aspect_ratio in SUPPORTED_RATIOS:
            return request.aspect_ratio
        # Nearest supported ratio by numeric distance.
        target = request.width / request.height
        best = min(
            SUPPORTED_RATIOS,
            key=lambda r: abs(target - (int(r.split(":")[0]) / int(r.split(":")[1]))),
        )
        return best

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "prompt": prompt[: self.max_prompt_chars],
            "aspect_ratio": self._ratio(request),
            "output_format": "png",
            "mode": "text-to-image",
        }
        if negative:
            fields["negative_prompt"] = negative[:10000]
        if seed is not None:
            fields["seed"] = str(seed)
        return {
            "method": "POST",
            "url": ENDPOINT.format(model=self._model()),
            "multipart_fields": fields,
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
                "Stability AI needs an API key. Add one under Engine -> Manage keys.",
                code="missing_key",
            )

        payload = self.build_payload(request, prompt, negative, seed)
        # requests needs a files= mapping to emit multipart/form-data with no file part.
        files = {key: (None, str(value)) for key, value in payload["multipart_fields"].items()}

        response, trace = request_with_retry(
            session,
            "POST",
            payload["url"],
            files=files,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "image/*",           # RAW bytes, not b64 JSON
            },
            timeout=request.timeout,
            max_retries=request.max_retries,
            stream=True,
            on_event=on_event,
            deadline=request.deadline,
        )

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            body = b""
            try:
                body = next(response.iter_content(600), b"")
            finally:
                response.close()
            text = body.decode("utf-8", "replace")
            if "content_moderation" in text or "flagged" in text:
                raise ProviderError(
                    f"Engine returned {response.status_code}: {text[:250]}",
                    code="moderation_blocked",
                    status=response.status_code,
                )
            raise self._auth_error(response.status_code, text)

        return ProviderResult(
            stream=response,
            transport=trace,
            meta={
                "engine_model": f"stable-image-{self._model()}",
                "aspect_ratio": payload["multipart_fields"]["aspect_ratio"],
                "seed": response.headers.get("seed", seed),
                "finish_reason": response.headers.get("finish-reason", ""),
                "endpoint": payload["url"],
            },
        )
