"""Stage 6 - automated quality assurance.

Two lenses, per the blueprint:

  Lens 1 - Aesthetic classification. Score the asset 0-10; assets below the
           threshold (default 7.0) are flagged, and optionally discarded.
  Lens 2 - Semantic alignment. Does the artwork actually match the prompt that
           requested it? Divergent assets are flagged for regeneration.

The reference implementation uses CLIP ViT-L/14 embeddings plus a linear
aesthetic head, and PickScore / CLIP-IQA for alignment. Those pull in torch and
~1.7 GB of weights, so this module runs a **two-tier scorer**:

  * `clip`      - used automatically when `torch` + `transformers` are installed
                  and CLIP weights are available locally or downloadable.
  * `heuristic` - the always-available fallback. Real image statistics
                  (edge energy, contrast, colorfulness, entropy, exposure) folded
                  into the same 0-10 scale, with alignment measured by matching
                  the prompt's colour / lighting / composition vocabulary against
                  what the pixels actually show.

The scorer in use is reported in every record, so a heuristic score is never
mistaken for a CLIP score.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

AESTHETIC_THRESHOLD = 7.0
ALIGNMENT_THRESHOLD = 0.55
_ANALYSIS_EDGE = 512          # downsample before statistics; QA must stay cheap


@dataclass
class QAReport:
    scorer: str                 # clip | heuristic
    aesthetic: float            # 0 - 10
    alignment: float            # 0 - 1
    passed: bool
    threshold: float
    notes: list[str]
    stats: dict

    def to_dict(self) -> dict:
        return asdict(self)


# --- shared measurement --------------------------------------------------

def _load_array(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((_ANALYSIS_EDGE, _ANALYSIS_EDGE), Image.LANCZOS)
        return np.asarray(image, dtype=np.float32)


def measure(path: Path) -> dict:
    """Raw image statistics. Also feeds Gate 2's placeholder-frame detector."""
    rgb = _load_array(path)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b

    # Edge energy via a discrete Laplacian - the standard blur proxy.
    lap = (
        -4.0 * luma[1:-1, 1:-1]
        + luma[:-2, 1:-1] + luma[2:, 1:-1]
        + luma[1:-1, :-2] + luma[1:-1, 2:]
    )
    sharpness = float(np.sqrt(np.mean(lap ** 2))) if lap.size else 0.0

    # Hasler-Susstrunk colorfulness.
    rg = r - g
    yb = 0.5 * (r + g) - b
    colorfulness = float(
        math.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * math.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )

    hist, _ = np.histogram(luma, bins=64, range=(0, 255))
    p = hist / max(hist.sum(), 1)
    p = p[p > 0]
    entropy = float(-(p * np.log2(p)).sum())      # 0 - 6 for 64 bins

    clipped_low = float((luma < 4).mean())
    clipped_high = float((luma > 251).mean())

    return {
        "sharpness": round(sharpness, 3),
        "contrast_std": round(float(luma.std()), 3),
        "colorfulness": round(colorfulness, 3),
        "entropy": round(entropy, 3),
        "mean_luma": round(float(luma.mean()), 2),
        "clipped_shadows": round(clipped_low, 4),
        "clipped_highlights": round(clipped_high, 4),
        "mean_rgb": [round(float(r.mean()), 1), round(float(g.mean()), 1), round(float(b.mean()), 1)],
        "saturation": round(float(np.mean(rgb.max(axis=2) - rgb.min(axis=2))), 2),
    }


# --- Lens 1: heuristic aesthetic score -----------------------------------

def _band(value: float, low: float, high: float) -> float:
    """Linear 0-1 ramp between low and high, clamped."""
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def _heuristic_aesthetic(stats: dict) -> tuple[float, list[str]]:
    notes: list[str] = []

    sharp = _band(stats["sharpness"], 2.0, 18.0)
    contrast = _band(stats["contrast_std"], 12.0, 62.0)
    color = _band(stats["colorfulness"], 8.0, 70.0)
    detail = _band(stats["entropy"], 3.0, 5.6)

    # Exposure: reward mid-key, penalize crushed or blown frames.
    luma = stats["mean_luma"]
    exposure = 1.0 - min(1.0, abs(luma - 122.0) / 122.0)
    clip_penalty = min(0.35, (stats["clipped_shadows"] + stats["clipped_highlights"]) * 1.8)

    score01 = (
        0.30 * sharp
        + 0.24 * contrast
        + 0.18 * color
        + 0.18 * detail
        + 0.10 * exposure
    ) - clip_penalty
    score = round(max(0.0, min(1.0, score01)) * 10.0, 2)

    if sharp < 0.25:
        notes.append("Low edge energy - the frame reads soft or out of focus.")
    if contrast < 0.25:
        notes.append("Flat tonal range; the composition lacks contrast.")
    if detail < 0.25:
        notes.append("Low entropy - very little structural detail in the frame.")
    if clip_penalty > 0.12:
        notes.append("Significant clipping in shadows or highlights.")
    if not notes:
        notes.append("Strong edge energy, tonal range and detail distribution.")
    return score, notes


# --- Lens 2: heuristic semantic alignment --------------------------------
# The prompt vocabulary we can genuinely check against pixel statistics. This is
# a proxy for CLIP similarity, not a replacement, and is labelled as such.

_COLOR_TERMS: dict[str, tuple[float, float, float]] = {
    "red": (200, 60, 60), "crimson": (190, 40, 60), "orange": (230, 140, 50),
    "yellow": (230, 220, 70), "gold": (210, 175, 70), "green": (70, 170, 90),
    "emerald": (50, 165, 110), "teal": (50, 160, 165), "cyan": (70, 210, 225),
    "blue": (65, 105, 210), "navy": (35, 55, 110), "purple": (140, 80, 190),
    "violet": (150, 90, 210), "magenta": (210, 70, 170), "pink": (235, 150, 185),
    "brown": (130, 95, 65), "beige": (215, 200, 170), "white": (240, 240, 240),
    "black": (25, 25, 25), "grey": (130, 130, 130), "gray": (130, 130, 130),
    "silver": (185, 185, 190), "sepia": (160, 130, 95),
}

_DARK_TERMS = ("night", "dark", "midnight", "shadow", "noir", "moonlit", "dim", "silhouette", "eclipse")
_BRIGHT_TERMS = ("bright", "daylight", "sunlit", "sunny", "noon", "glowing", "luminous", "snow", "white background")
_VIVID_TERMS = ("vivid", "neon", "saturated", "vibrant", "colorful", "rainbow", "psychedelic", "vaporwave")
_MUTED_TERMS = ("muted", "pastel", "desaturated", "monochrome", "greyscale", "grayscale", "black and white", "minimalist")
_DETAIL_TERMS = ("intricate", "detailed", "ornate", "complex", "busy", "crowded", "baroque", "filigree")
_SIMPLE_TERMS = ("simple", "minimal", "clean", "flat", "empty", "negative space", "sparse")


def _heuristic_alignment(prompt: str, stats: dict) -> tuple[float, list[str]]:
    text = prompt.lower()
    checks: list[float] = []
    notes: list[str] = []

    # Colour intent vs. measured mean colour.
    wanted = [_COLOR_TERMS[t] for t in _COLOR_TERMS if re.search(rf"\b{t}\b", text)]
    if wanted:
        mean = np.array(stats["mean_rgb"], dtype=np.float32)
        best = min(float(np.linalg.norm(mean - np.array(c, dtype=np.float32))) for c in wanted)
        colour_score = max(0.0, 1.0 - best / 220.0)
        checks.append(colour_score)
        if colour_score < 0.45:
            notes.append("Requested colour vocabulary is weakly represented in the frame.")

    # Lighting intent vs. measured luma.
    if any(t in text for t in _DARK_TERMS):
        checks.append(1.0 - _band(stats["mean_luma"], 90.0, 190.0))
        if stats["mean_luma"] > 150:
            notes.append("Prompt asks for a dark scene but the frame is high-key.")
    if any(t in text for t in _BRIGHT_TERMS):
        checks.append(_band(stats["mean_luma"], 70.0, 175.0))
        if stats["mean_luma"] < 70:
            notes.append("Prompt asks for a bright scene but the frame is low-key.")

    # Saturation intent.
    if any(t in text for t in _VIVID_TERMS):
        checks.append(_band(stats["colorfulness"], 20.0, 75.0))
    if any(t in text for t in _MUTED_TERMS):
        checks.append(1.0 - _band(stats["colorfulness"], 25.0, 80.0))

    # Complexity intent vs. entropy.
    if any(t in text for t in _DETAIL_TERMS):
        checks.append(_band(stats["entropy"], 3.6, 5.7))
    if any(t in text for t in _SIMPLE_TERMS):
        checks.append(1.0 - _band(stats["entropy"], 4.2, 5.9))

    if not checks:
        # Nothing in the prompt is measurable from pixels alone. Fall back to a
        # neutral prior driven by whether the frame carries real signal at all.
        base = 0.5 + 0.4 * _band(stats["entropy"], 3.2, 5.6)
        notes.append("No pixel-checkable attributes in the prompt; alignment is a neutral prior.")
        return round(min(0.92, base), 3), notes

    score = float(np.mean(checks))
    if not notes:
        notes.append("Measured colour, lighting and complexity track the prompt.")
    return round(score, 3), notes


# --- CLIP tier (used automatically when torch is present) ----------------

@lru_cache(maxsize=1)
def _clip_backend():
    """Return (model, processor) or None. Cached so weights load once per process."""
    try:
        import torch  # noqa: F401
        from transformers import CLIPModel, CLIPProcessor
    except Exception:
        return None
    try:
        name = "openai/clip-vit-large-patch14"
        model = CLIPModel.from_pretrained(name)
        processor = CLIPProcessor.from_pretrained(name)
        model.eval()
        return model, processor
    except Exception:
        return None


def _clip_scores(path: Path, prompt: str) -> tuple[float, float] | None:
    backend = _clip_backend()
    if backend is None:
        return None
    import torch

    model, processor = backend
    with Image.open(path) as image:
        image = image.convert("RGB")
        # Alignment: cosine similarity between the image and the prompt, plus a
        # quality contrast pair that stands in for the linear aesthetic head.
        prompts = [
            prompt[:300],
            "a beautiful, professionally composed, high quality artwork",
            "a blurry, ugly, poorly composed, low quality image",
        ]
        inputs = processor(text=prompts, images=image, return_tensors="pt", padding=True, truncation=True)
        with torch.no_grad():
            out = model(**inputs)
        img = out.image_embeds / out.image_embeds.norm(dim=-1, keepdim=True)
        txt = out.text_embeds / out.text_embeds.norm(dim=-1, keepdim=True)
        sims = (img @ txt.T).squeeze(0)

    alignment = float(max(0.0, min(1.0, (sims[0].item() - 0.10) / 0.25)))
    good, bad = sims[1].item(), sims[2].item()
    aesthetic = float(max(0.0, min(1.0, 0.5 + (good - bad) * 8.0))) * 10.0
    return round(aesthetic, 2), round(alignment, 3)


# --- entry point ---------------------------------------------------------

def assess(
    path: Path,
    prompt: str,
    threshold: float = AESTHETIC_THRESHOLD,
    prefer_clip: bool = True,
) -> QAReport:
    """Run both QA lenses and return a pass/fail verdict against the threshold."""
    stats = measure(path)

    clip_result = _clip_scores(path, prompt) if prefer_clip else None
    if clip_result is not None:
        aesthetic, alignment = clip_result
        scorer = "clip"
        notes = ["Scored with CLIP ViT-L/14 embeddings."]
    else:
        aesthetic, aesthetic_notes = _heuristic_aesthetic(stats)
        alignment, alignment_notes = _heuristic_alignment(prompt, stats)
        scorer = "heuristic"
        notes = aesthetic_notes + alignment_notes

    passed = aesthetic >= threshold and alignment >= ALIGNMENT_THRESHOLD
    if aesthetic < threshold:
        notes.append(f"Aesthetic {aesthetic:.2f} is below the {threshold:.1f} gate.")
    if alignment < ALIGNMENT_THRESHOLD:
        notes.append(f"Semantic alignment {alignment:.2f} is below the {ALIGNMENT_THRESHOLD:.2f} gate - flagged as divergent.")

    return QAReport(
        scorer=scorer,
        aesthetic=aesthetic,
        alignment=alignment,
        passed=passed,
        threshold=threshold,
        notes=notes,
        stats=stats,
    )
