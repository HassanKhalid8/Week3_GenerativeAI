"""Offline engine - renders locally, touches no network.

Exists so the entire six-stage pipeline (payload -> gates -> transport ->
integrity -> QA -> manifest) can be exercised and tested with no key, no quota
and no internet. It produces a deterministic seeded composition rather than a
blank canvas, so the QA scorer has real signal to measure.
"""

from __future__ import annotations

import colorsys
import io
import math
import random
from typing import Any, Callable

import requests
from PIL import Image, ImageDraw, ImageFilter

from ..params import GenerationRequest
from ..transport import TransportTrace, Attempt
from .base import Provider, ProviderResult


class MockProvider(Provider):
    name = "mock"
    label = "Offline Mock Renderer"
    env_key = ""
    max_prompt_chars = 10000
    supports_negative = True
    supports_seed = True
    free = True
    notes = "No network, no key. Renders a deterministic seeded composition for pipeline testing."

    def build_payload(self, request: GenerationRequest, prompt: str, negative: str, seed: int) -> dict[str, Any]:
        return {
            "method": "LOCAL",
            "url": "memory://mock-renderer",
            "json": {
                "prompt": prompt,
                "negative_prompt": negative,
                "width": request.width,
                "height": request.height,
                "seed": seed,
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
        rng = random.Random(f"{prompt}|{seed}")
        width, height = request.width, request.height
        base_hue = rng.random()

        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)

        # Vertical gradient ground.
        for y in range(height):
            t = y / max(height - 1, 1)
            hue = (base_hue + 0.12 * t) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.55 - 0.25 * t, 0.28 + 0.55 * t)
            draw.line([(0, y), (width, y)], fill=(int(r * 255), int(g * 255), int(b * 255)))

        # Seeded geometry so entropy and edge energy are genuinely non-trivial.
        for i in range(rng.randint(14, 26)):
            hue = (base_hue + 0.5 + rng.random() * 0.3) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.7, 0.95)
            colour = (int(r * 255), int(g * 255), int(b * 255))
            cx, cy = rng.randint(0, width), rng.randint(0, height)
            span = rng.randint(min(width, height) // 14, min(width, height) // 3)
            box = [cx - span, cy - span, cx + span, cy + span]
            shape = rng.choice(("ellipse", "rect", "line", "arc"))
            if shape == "ellipse":
                draw.ellipse(box, outline=colour, width=max(2, span // 22))
            elif shape == "rect":
                draw.rectangle(box, outline=colour, width=max(2, span // 26))
            elif shape == "arc":
                draw.arc(box, rng.randint(0, 180), rng.randint(180, 360), fill=colour, width=max(2, span // 20))
            else:
                draw.line([box[0], box[1], box[2], box[3]], fill=colour, width=max(1, span // 30))

        # Horizon band keyed to the prompt hash - a visual fingerprint.
        band = int(height * (0.35 + 0.3 * ((hash(prompt) % 100) / 100.0)))
        draw.rectangle([0, band, width, band + max(2, height // 90)], fill=(245, 245, 250))

        image = image.filter(ImageFilter.SMOOTH)

        # Fine grain keeps the QA sharpness metric honest.
        pixels = image.load()
        for _ in range(width * height // 60):
            x, y = rng.randrange(width), rng.randrange(height)
            px = pixels[x, y]
            jitter = rng.randint(-26, 26)
            pixels[x, y] = tuple(max(0, min(255, c + jitter)) for c in px)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)

        trace = TransportTrace(
            attempts=[Attempt(1, "ok", "LOCAL render (no network)", 0, 200)],
            total_ms=0,
        )
        return ProviderResult(
            raw=buffer.getvalue(),
            transport=trace,
            meta={"engine_model": "mock-procedural-v1", "seed": seed, "endpoint": "memory://mock-renderer"},
        )
