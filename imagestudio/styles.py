"""Style preset parameters folded into the payload (the PDF's suggested extension).

Each preset contributes a positive modifier appended to the user prompt and a
negative modifier merged into the negative prompt, so a one-word choice like
"cyberpunk" expands into a full stylistic vector without the user writing it.
"""

from __future__ import annotations

STYLE_PRESETS: dict[str, dict[str, str]] = {
    "none": {
        "label": "No preset",
        "positive": "",
        "negative": "",
    },
    "cyberpunk": {
        "label": "Cyberpunk",
        "positive": (
            "cyberpunk aesthetic, neon signage, rain-slicked reflective streets, "
            "volumetric haze, teal and magenta rim lighting, high contrast, cinematic"
        ),
        "negative": "pastel, rustic, daylight, washed out",
    },
    "minimalism": {
        "label": "Minimalism",
        "positive": (
            "minimalist composition, vast negative space, two-tone palette, "
            "clean geometry, soft even studio light, flat design"
        ),
        "negative": "clutter, busy background, ornate detail, texture noise",
    },
    "photoreal": {
        "label": "Photoreal",
        "positive": (
            "photorealistic, shot on 85mm prime lens, f/1.8 shallow depth of field, "
            "natural light, physically accurate materials, ultra detailed"
        ),
        "negative": "illustration, cartoon, painting, cgi render, plastic skin",
    },
    "blueprint": {
        "label": "Technical Blueprint",
        "positive": (
            "technical blueprint schematic, cyan line work on dark drafting grid, "
            "orthographic projection, annotated callouts, monospaced labels"
        ),
        "negative": "photograph, painterly texture, warm colors",
    },
    "watercolor": {
        "label": "Watercolor",
        "positive": (
            "loose watercolor painting, visible paper grain, bleeding pigment edges, "
            "wet-on-wet washes, muted natural palette"
        ),
        "negative": "hard vector edges, 3d render, photographic detail",
    },
    "anime": {
        "label": "Anime / Cel",
        "positive": (
            "anime key visual, crisp cel shading, expressive linework, "
            "vivid saturated palette, dramatic backlight"
        ),
        "negative": "photorealistic, muddy colors, western comic style",
    },
    "isometric": {
        "label": "Isometric 3D",
        "positive": (
            "isometric 3d render, 45 degree camera, soft ambient occlusion, "
            "clay material, tidy miniature diorama, pastel accent colors"
        ),
        "negative": "perspective distortion, photograph, flat 2d",
    },
    "film-noir": {
        "label": "Film Noir",
        "positive": (
            "film noir, high contrast black and white, venetian blind shadows, "
            "heavy grain, low key single source lighting, 1940s cinematography"
        ),
        "negative": "color, bright even lighting, modern setting",
    },
    "vaporwave": {
        "label": "Vaporwave",
        "positive": (
            "vaporwave, sunset gradient of pink and cyan, chrome typography, "
            "endless grid horizon, VHS scanlines, retro 1980s"
        ),
        "negative": "muted palette, realistic lighting, natural landscape",
    },
    "oil-painting": {
        "label": "Oil Painting",
        "positive": (
            "classical oil painting, thick impasto brush strokes, canvas weave, "
            "chiaroscuro lighting, rich earth pigments, museum finish"
        ),
        "negative": "digital art, smooth gradients, photograph",
    },
    "product": {
        "label": "Product Studio",
        "positive": (
            "commercial product photography, seamless white cyclorama, "
            "three point softbox lighting, subtle contact shadow, catalogue ready"
        ),
        "negative": "cluttered background, harsh shadow, low resolution",
    },
}

# Applied to every request unless the operator clears it - the cheap way to keep
# obvious generation artifacts out of the asset library.
BASE_NEGATIVE = "lowres, jpeg artifacts, watermark, signature, text overlay, extra limbs, deformed"


def style_table() -> list[dict[str, str]]:
    return [
        {"key": key, "label": spec["label"], "positive": spec["positive"], "negative": spec["negative"]}
        for key, spec in STYLE_PRESETS.items()
    ]


def compose(prompt: str, negative: str, style: str, include_base_negative: bool = True) -> tuple[str, str]:
    """Expand (prompt, negative, style) into the final serialized text pair."""
    spec = STYLE_PRESETS.get(style) or STYLE_PRESETS["none"]

    positive = prompt.strip()
    if spec["positive"]:
        positive = f"{positive}, {spec['positive']}"

    parts = [p.strip() for p in (negative, spec["negative"]) if p and p.strip()]
    if include_base_negative:
        parts.append(BASE_NEGATIVE)
    # De-duplicate while preserving order.
    seen: set[str] = set()
    merged: list[str] = []
    for chunk in parts:
        for term in chunk.split(","):
            term = term.strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                merged.append(term)
    return positive, ", ".join(merged)
