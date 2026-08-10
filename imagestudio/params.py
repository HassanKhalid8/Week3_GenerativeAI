"""Stage 1 - Prompt payload formulation.

The blueprint's critical architecture rule: passing unsupported dimensions causes
an immediate API handshake failure, so user intent ("make it a wallpaper") is
mapped to *exact* strict resolution variables before anything is transmitted.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, asdict
from typing import Any

# --- The aspect-ratio -> exact pixel payload map -------------------------
# Every entry is an SDXL-native bucket (multiple of 64, ~1.05 MP total volume),
# which is what keeps diffusion backends from silently re-cropping the canvas.
ASPECT_RATIOS: dict[str, dict[str, Any]] = {
    "16:9": {"width": 1344, "height": 768, "label": "Landscape", "target": "Web banners, presentations"},
    "1:1": {"width": 1024, "height": 1024, "label": "Square", "target": "Avatars, product grids"},
    "9:16": {"width": 768, "height": 1344, "label": "Vertical", "target": "Mobile reels, wallpapers"},
    "4:3": {"width": 1152, "height": 896, "label": "Classic", "target": "Slides, editorial"},
    "3:4": {"width": 896, "height": 1152, "label": "Portrait", "target": "Posters, book covers"},
    "3:2": {"width": 1216, "height": 832, "label": "Photo", "target": "Photography, prints"},
    "2:3": {"width": 832, "height": 1216, "label": "Photo Tall", "target": "Magazine, portraits"},
    "21:9": {"width": 1536, "height": 640, "label": "Cinematic", "target": "Film stills, headers"},
}

DEFAULT_ASPECT = "1:1"
MAX_COUNT = 4
SEED_MAX = 2**31 - 1


class ParameterError(ValueError):
    """Raised when a payload would be rejected by the remote handshake."""


def pixel_volume(ratio: str) -> int:
    spec = ASPECT_RATIOS[ratio]
    return spec["width"] * spec["height"]


def ratio_table() -> list[dict[str, Any]]:
    """Serializable ratio catalogue for the UI selector."""
    rows = []
    for key, spec in ASPECT_RATIOS.items():
        rows.append(
            {
                "ratio": key,
                "width": spec["width"],
                "height": spec["height"],
                "label": spec["label"],
                "target": spec["target"],
                "pixels": spec["width"] * spec["height"],
            }
        )
    return rows


@dataclass
class GenerationRequest:
    """The structural parameters captured in the input phase, pre-serialization."""

    prompt: str
    negative_prompt: str = ""
    aspect_ratio: str = DEFAULT_ASPECT
    style: str = "none"
    count: int = 1
    seed: int | None = None
    provider: str = "auto"
    # Transport policy - surfaced so the operator can tune the split timeout.
    connect_timeout: float = 3.05
    read_timeout: float = 60.0
    max_retries: int = 3
    # Wall-clock budget for the whole batch. Serverless hosts kill a function at
    # a fixed ceiling, so the retry shield needs to know when to stop waiting.
    # 0 means unbounded, which is the right default for a local run.
    budget_seconds: float = 0.0
    # Stage 6 gate
    qa_threshold: float = 7.0
    qa_discard: bool = False

    # Derived, filled by validate()
    width: int = field(default=0, init=False)
    height: int = field(default=0, init=False)
    started_at: float = field(default=0.0, init=False)

    def validate(self, max_prompt_chars: int = 4000) -> "GenerationRequest":
        prompt = (self.prompt or "").strip()
        if not prompt:
            raise ParameterError("Prompt is empty - nothing to serialize into the payload.")
        if len(prompt) > max_prompt_chars:
            raise ParameterError(
                f"Prompt is {len(prompt)} characters; this engine caps at {max_prompt_chars}."
            )
        self.prompt = prompt
        self.negative_prompt = (self.negative_prompt or "").strip()

        if self.aspect_ratio not in ASPECT_RATIOS:
            raise ParameterError(
                f"Unsupported aspect ratio {self.aspect_ratio!r}. "
                f"Supported: {', '.join(ASPECT_RATIOS)}"
            )
        spec = ASPECT_RATIOS[self.aspect_ratio]
        self.width, self.height = spec["width"], spec["height"]

        try:
            self.count = int(self.count)
        except (TypeError, ValueError):
            raise ParameterError("Generation count must be a whole number.") from None
        if not 1 <= self.count <= MAX_COUNT:
            raise ParameterError(f"Generation count must be between 1 and {MAX_COUNT}.")

        if self.seed in (None, "", -1):
            self.seed = None
        else:
            try:
                self.seed = int(self.seed) % (SEED_MAX + 1)
            except (TypeError, ValueError):
                raise ParameterError("Seed must be an integer.") from None

        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ParameterError("Timeouts must be positive.")
        if self.read_timeout < self.connect_timeout:
            raise ParameterError("Read timeout must exceed the connect timeout.")
        self.max_retries = max(0, min(int(self.max_retries), 6))
        self.qa_threshold = max(0.0, min(float(self.qa_threshold), 10.0))
        self.budget_seconds = max(0.0, float(self.budget_seconds or 0.0))
        self.started_at = time.monotonic()
        return self

    def seed_for(self, index: int) -> int:
        """Deterministic per-image seed so a batch is reproducible from one number."""
        if self.seed is None:
            return random.randint(0, SEED_MAX)
        return (self.seed + index * 7919) % (SEED_MAX + 1)

    @property
    def timeout(self) -> tuple[float, float]:
        """The split-timeout tuple handed to requests: (connect, read)."""
        return (self.connect_timeout, self.read_timeout)

    @property
    def deadline(self) -> float | None:
        """Monotonic stamp the batch must finish by, or None when unbounded."""
        if not self.budget_seconds:
            return None
        return (self.started_at or time.monotonic()) + self.budget_seconds

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["width"] = self.width
        data["height"] = self.height
        return data
