"""Pollinations - the zero-key default engine.

Free, no account, no API key. The prompt is URL-encoded into the path and the
structural parameters ride as the query string; the response body is the raw
image binary, which is exactly the streaming case Stage 4 was built for.

An optional POLLINATIONS_TOKEN raises the rate limit but is never required.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

import requests

from ..params import GenerationRequest
from ..transport import request_with_retry
from .base import Provider, ProviderError, ProviderResult

ENDPOINT = "https://image.pollinations.ai/prompt/{prompt}"


class PollinationsProvider(Provider):
    name = "pollinations"
    label = "Pollinations (Flux)"
    env_key = ""                     # deliberately none
    max_prompt_chars = 2000          # path-length safety, not an engine limit
    supports_negative = False        # folded into the prompt text instead
    supports_seed = True
    free = True
    notes = "No API key required. Free Flux/Turbo text-to-image; returns raw image bytes."

    MODELS = ("flux", "turbo")

    def _model(self) -> str:
        model = str(self.options.get("model") or "flux").lower()
        return model if model in self.MODELS else "flux"

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        text = prompt
        if negative:
            # No negative field on this engine, so the constraint is expressed
            # in-band. Documented rather than hidden.
            text = f"{prompt}. Avoid: {negative}"
        return {
            "method": "GET",
            "url": ENDPOINT.format(prompt="<url-encoded prompt>"),
            "path_prompt": text[: self.max_prompt_chars],
            "query": {
                "width": request.width,
                "height": request.height,
                "seed": seed,
                "model": self._model(),
                "nologo": "true",
                "safe": "false",
                "referrer": "multimodal-image-studio",
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
        payload = self.build_payload(request, prompt, negative, seed)
        url = ENDPOINT.format(prompt=quote(payload["path_prompt"], safe=""))

        headers: dict[str, str] = {"Accept": "image/*"}
        token = self.options.get("token") or self.api_key
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response, trace = request_with_retry(
            session,
            "GET",
            url,
            params=payload["query"],
            headers=headers,
            timeout=request.timeout,
            max_retries=request.max_retries,
            stream=True,
            on_event=on_event,
            deadline=request.deadline,
        )

        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or not content_type.startswith("image/"):
            body = ""
            try:
                body = next(response.iter_content(400), b"").decode("utf-8", "replace")
            finally:
                response.close()
            raise ProviderError(
                f"Engine returned {response.status_code} ({content_type or 'unknown type'}). {body[:200]}",
                code="unexpected_response",
                status=response.status_code,
            )

        return ProviderResult(
            stream=response,
            transport=trace,
            meta={"engine_model": payload["query"]["model"], "seed": seed, "endpoint": url.split("?")[0]},
        )
