"""Multimodal Image Generation Studio - DecodeLabs Generative AI Project 3.

A text-to-image studio built as a six-stage production pipeline rather than a
single API call: payload formulation, a shielded network gateway, dual moderation
gates, memory-safe chunked transport, forced pixel-level integrity verification,
and automated quality assurance.
"""

from .engine import STAGES, AssetOutcome, BatchResult, Studio
from .params import ASPECT_RATIOS, GenerationRequest, ParameterError, ratio_table
from .styles import STYLE_PRESETS, style_table

__version__ = "1.0.0"

__all__ = [
    "Studio",
    "GenerationRequest",
    "ParameterError",
    "BatchResult",
    "AssetOutcome",
    "STAGES",
    "ASPECT_RATIOS",
    "ratio_table",
    "STYLE_PRESETS",
    "style_table",
    "__version__",
]
