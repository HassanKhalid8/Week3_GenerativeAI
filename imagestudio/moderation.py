"""Stage 3 - the dual security gates.

GATE 1 (pre-generation, input filtering) runs locally *before* the payload is
transmitted: 0 compute cost incurred, instant rejection, error trace
`sentinel_block`. GATE 2 (post-generation, output filtering) interprets whatever
the remote moderator did to a request that already burned GPU time - a refusal
code, a truncated response, or the classic blurred placeholder frame.

Both gates raise nothing. They return a verdict so the pipeline can surface a
polite warning instead of crashing the application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BLOCKED = "blocked"
ALLOWED = "allowed"
FLAGGED = "flagged"


@dataclass
class Verdict:
    decision: str          # allowed | flagged | blocked
    code: str              # machine trace, e.g. sentinel_block
    message: str           # user-facing, polite
    category: str = ""

    @property
    def ok(self) -> bool:
        return self.decision != BLOCKED

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "code": self.code,
            "message": self.message,
            "category": self.category,
        }


# --- GATE 1 -------------------------------------------------------------
# Deliberately narrow: categories that every upstream engine rejects anyway, so
# catching them here saves a round trip rather than inventing new policy.
_HARD_BLOCK: list[tuple[str, str, str]] = [
    (
        "csam",
        r"\b(child|minor|underage|preteen|toddler|kid|teen(?:ager)?)\b[^.]{0,40}\b(nude|naked|nsfw|sexual|erotic|porn|explicit)\b"
        r"|\b(nude|naked|nsfw|sexual|erotic|porn|explicit)\b[^.]{0,40}\b(child|minor|underage|preteen|toddler|kid|teen(?:ager)?)\b",
        "This request pairs minors with sexual content and cannot be generated.",
    ),
    (
        "sexual_explicit",
        r"\b(hardcore\s+porn|explicit\s+sex\s+scene|pornographic|xxx\s+scene)\b",
        "Explicit sexual imagery is outside this studio's content policy.",
    ),
    (
        "extreme_violence",
        r"\b(gore|mutilat\w*|dismember\w*|beheading|torture\s+victim)\b",
        "Graphic gore is blocked before the payload leaves this machine.",
    ),
    (
        "weapon_instructions",
        r"\b(diagram|schematic|blueprint|instructions?)\b[^.]{0,40}\b(pipe\s*bomb|ied|nerve\s+agent|explosive\s+device)\b",
        "Weapon construction diagrams cannot be generated.",
    ),
]

# Softer signals: allowed through, but recorded so the operator sees why an
# upstream engine might refuse the same payload.
_SOFT_FLAG: list[tuple[str, str, str]] = [
    (
        "public_figure",
        r"\b(deepfake|face\s*swap|photo\s+of\s+(?:president|prime\s+minister|celebrity))\b",
        "Likenesses of real people are frequently refused by the remote moderator.",
    ),
    (
        "trademark",
        r"\b(disney|pixar|marvel|nintendo|pokemon|mickey\s+mouse|coca[-\s]?cola)\b",
        "Trademarked characters or brands may trip the upstream output filter.",
    ),
    (
        "violence",
        r"\b(blood|gun|weapon|corpse|war\s+zone)\b",
        "Violent subject matter may be softened or refused by the remote engine.",
    ),
]


def gate_input(prompt: str, negative_prompt: str = "") -> Verdict:
    """Pre-generation filter. Zero compute cost on rejection."""
    haystack = f"{prompt} {negative_prompt}".lower()

    for category, pattern, message in _HARD_BLOCK:
        if re.search(pattern, haystack, re.IGNORECASE):
            return Verdict(BLOCKED, "sentinel_block", message, category)

    for category, pattern, message in _SOFT_FLAG:
        if re.search(pattern, haystack, re.IGNORECASE):
            return Verdict(FLAGGED, "content_policy_advisory", message, category)

    return Verdict(ALLOWED, "gate1_pass", "Input payload cleared for transmission.")


# --- GATE 2 -------------------------------------------------------------
# Remote refusal signatures, keyed by the trace strings the engines actually
# emit. Compute cost is already fully incurred by the time we read these.
_REFUSAL_KEYS = (
    "content_policy_violation",
    "moderation_blocked",
    "safety",
    "nsfw",
    "content_filter",
    "blocked",
    "flagged",
)


def gate_output_response(status_code: int, body_snippet: str) -> Verdict:
    """Read a non-image HTTP response for a moderation refusal."""
    lowered = (body_snippet or "").lower()
    if "finish_reason" in lowered and "filter" in lowered:
        return Verdict(
            BLOCKED,
            "finish_reason=FILTER",
            "The remote engine generated an image and then withheld it on safety grounds.",
            "output_filter",
        )
    if status_code in (400, 403, 422) and any(key in lowered for key in _REFUSAL_KEYS):
        return Verdict(
            BLOCKED,
            "moderation_blocked",
            "The remote moderator rejected this prompt. Try rephrasing the subject.",
            "output_filter",
        )
    return Verdict(ALLOWED, "gate2_pass", "No moderation signature in the response.")


def gate_output_image(stats: dict) -> Verdict:
    """Detect the blurred / blank placeholder an engine returns in place of a refusal.

    `stats` comes from qa.measure(): a near-zero standard deviation with near-zero
    edge energy across a full-size canvas is not a photograph, it is a placeholder.
    """
    std = float(stats.get("contrast_std", 0.0))
    edges = float(stats.get("sharpness", 0.0))
    if std < 3.0 and edges < 1.0:
        return Verdict(
            BLOCKED,
            "moderation_blocked",
            "The engine returned a blank or blurred placeholder frame instead of artwork.",
            "placeholder_frame",
        )
    if std < 8.0 and edges < 6.0:
        return Verdict(
            FLAGGED,
            "low_signal_frame",
            "Output carries very little visual signal - it may be a partial refusal.",
            "placeholder_frame",
        )
    return Verdict(ALLOWED, "gate2_pass", "Output frame carries genuine visual signal.")
