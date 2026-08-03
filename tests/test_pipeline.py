"""Pipeline tests - run with `python -m pytest tests -q`.

Everything here is offline: the mock engine renders locally, so the full
six-stage pipeline is exercised without a key, a quota or a network.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from imagestudio import integrity, moderation, qa, storage, styles, transport  # noqa: E402
from imagestudio.engine import Studio  # noqa: E402
from imagestudio.params import ASPECT_RATIOS, GenerationRequest, ParameterError  # noqa: E402
from imagestudio.providers import build, resolve  # noqa: E402


# ---------------------------------------------------------------- Stage 1

def test_every_ratio_is_a_diffusion_safe_bucket():
    for ratio, spec in ASPECT_RATIOS.items():
        assert spec["width"] % 64 == 0, f"{ratio} width is not a multiple of 64"
        assert spec["height"] % 64 == 0, f"{ratio} height is not a multiple of 64"
        assert 0.55e6 < spec["width"] * spec["height"] < 1.2e6


def test_validate_maps_ratio_to_exact_pixels():
    request = GenerationRequest(prompt="a tin robot", aspect_ratio="16:9").validate()
    assert (request.width, request.height) == (1344, 768)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": "   "},
        {"prompt": "ok", "aspect_ratio": "7:3"},
        {"prompt": "ok", "count": 9},
        {"prompt": "ok", "count": 0},
    ],
)
def test_bad_payloads_are_rejected_before_transmission(kwargs):
    with pytest.raises(ParameterError):
        GenerationRequest(**kwargs).validate()


def test_prompt_ceiling_is_enforced_per_engine():
    with pytest.raises(ParameterError):
        GenerationRequest(prompt="x" * 1200).validate(max_prompt_chars=1000)


def test_seed_batch_is_deterministic_and_distinct():
    request = GenerationRequest(prompt="p", seed=42, count=3).validate()
    seeds = [request.seed_for(i) for i in range(3)]
    assert len(set(seeds)) == 3
    assert seeds == [GenerationRequest(prompt="p", seed=42).validate().seed_for(i) for i in range(3)]


def test_style_preset_expands_both_directions():
    positive, negative = styles.compose("a city", "", "cyberpunk")
    assert "cyberpunk" in positive and positive.startswith("a city")
    assert "pastel" in negative
    assert "watermark" in negative          # base negative is merged in


def test_negative_terms_are_deduplicated():
    _, negative = styles.compose("a city", "watermark, blurry", "none")
    assert negative.lower().count("watermark") == 1


# ---------------------------------------------------------------- Stage 3

def test_gate1_blocks_at_zero_compute_cost():
    verdict = moderation.gate_input("a nude photo of a child")
    assert verdict.decision == moderation.BLOCKED
    assert verdict.code == "sentinel_block"


def test_gate1_passes_ordinary_prompts():
    assert moderation.gate_input("a lighthouse in a storm").ok


def test_gate1_flags_without_blocking():
    verdict = moderation.gate_input("a deepfake of the president")
    assert verdict.decision == moderation.FLAGGED and verdict.ok


def test_gate2_catches_a_blank_placeholder_frame():
    verdict = moderation.gate_output_image({"contrast_std": 0.4, "sharpness": 0.05})
    assert verdict.decision == moderation.BLOCKED


def test_gate2_reads_a_finish_reason_filter():
    verdict = moderation.gate_output_response(200, '{"finish_reason": "FILTER"}')
    assert verdict.code == "finish_reason=FILTER"


# ---------------------------------------------------------------- Stage 2

def test_backoff_grows_and_is_jittered():
    first = [transport.backoff_delay(0) for _ in range(40)]
    later = [transport.backoff_delay(3) for _ in range(40)]
    assert len(set(first)) > 1, "jitter should make delays non-identical"
    assert min(later) > max(first)
    assert max(transport.backoff_delay(20) for _ in range(20)) <= 30.0


# ---------------------------------------------------------------- Stage 5

def _png(path: Path, size=(256, 256)) -> Path:
    Image.new("RGB", size, (90, 140, 200)).save(path, "PNG")
    return path


def test_integrity_accepts_a_complete_png(tmp_path):
    report = integrity.verify_asset(_png(tmp_path / "ok.png"), 256, 256)
    assert report.ok and report.code == "verified" and report.fmt == "PNG"
    assert len(report.sha256) == 64


def _truncate(fmt: str, path: Path, fraction: float = 0.55) -> Path:
    buffer = io.BytesIO()
    Image.effect_noise((512, 512), 90).convert("RGB").save(buffer, fmt)
    complete = buffer.getvalue()
    path.write_bytes(complete[: int(len(complete) * fraction)])
    return path


def test_forced_pixel_decode_catches_the_silent_truncated_jpeg(tmp_path):
    """The exact trap Stage 5 exists for.

    A JPEG is scan-based, so `verify()` only reads the leading markers and
    reports success on a file whose connection died halfway through. Only the
    forced `.load()` walks every scanline and tells the truth.
    """
    truncated = _truncate("JPEG", tmp_path / "cut.jpg")

    # Readout 1 - magic bytes still say "valid JPEG".
    assert integrity.sniff_magic(truncated) == "JPEG"

    # Readout 2 - the header/CRC check passes. This is the silent disaster.
    with Image.open(truncated) as probe:
        probe.verify()

    # Readout 3 - the forced pixel decode catches it.
    report = integrity.verify_asset(truncated)
    assert not report.ok
    assert report.code == "broken_data_stream"
    assert report.stage == "pixel_decode"


def test_truncated_png_is_caught_at_the_chunk_walk(tmp_path):
    """PNG is chunk-structured, so truncation surfaces one readout earlier -
    still classified as a broken stream, never as a usable asset."""
    report = integrity.verify_asset(_truncate("PNG", tmp_path / "cut.png"))
    assert not report.ok
    assert report.code == "broken_data_stream"
    assert report.stage == "header"


def test_integrity_rejects_an_html_error_page_saved_as_png(tmp_path):
    path = tmp_path / "error.png"
    path.write_bytes(b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>")
    report = integrity.verify_asset(path)
    assert not report.ok and report.code == "not_an_image"


def test_integrity_flags_a_dimension_mismatch(tmp_path):
    report = integrity.verify_asset(_png(tmp_path / "small.png", (128, 128)), 1024, 1024)
    assert report.ok and report.dimension_match is False


# ---------------------------------------------------------------- Stage 4

def test_write_bytes_chunks_at_64_kib(tmp_path):
    payload = b"\x00" * (transport.CHUNK_SIZE * 3 + 17)
    result = transport.write_bytes(payload, tmp_path / "blob.bin")
    assert result.bytes_written == len(payload)
    assert result.chunks == 4


# ---------------------------------------------------------------- Stage 6

def test_qa_scores_a_flat_canvas_far_below_a_detailed_one(tmp_path):
    flat = tmp_path / "flat.png"
    Image.new("RGB", (320, 320), (128, 128, 128)).save(flat)

    detailed = tmp_path / "detail.png"
    Image.effect_noise((320, 320), 120).convert("RGB").save(detailed)

    flat_report = qa.assess(flat, "a flat grey square", prefer_clip=False)
    detail_report = qa.assess(detailed, "dense intricate texture", prefer_clip=False)

    assert flat_report.aesthetic < detail_report.aesthetic
    assert flat_report.scorer == "heuristic"
    assert 0.0 <= flat_report.alignment <= 1.0


def test_qa_alignment_notices_a_lighting_mismatch(tmp_path):
    bright = tmp_path / "bright.png"
    Image.effect_noise((256, 256), 30).convert("RGB").point(lambda v: min(255, v + 150)).save(bright)
    report = qa.assess(bright, "a dark midnight alley", prefer_clip=False)
    assert report.alignment < 0.6


# ---------------------------------------------------------- full pipeline

def test_offline_pipeline_runs_all_six_stages(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGESTUDIO_ASSETS", str(tmp_path))

    events: list[tuple[str, dict]] = []
    request = GenerationRequest(
        prompt="a brass orrery on a walnut desk",
        aspect_ratio="16:9",
        style="photoreal",
        count=2,
        seed=7,
        provider="mock",
        qa_threshold=0.0,
    )
    result = Studio(provider=build("mock")).generate(
        request, on_event=lambda kind, data: events.append((kind, data))
    )

    assert result.error == ""
    assert len(result.assets) == 2
    for asset in result.assets:
        assert asset.status in ("accepted", "flagged"), asset.error
        assert asset.integrity["code"] == "verified"
        assert asset.integrity["width"] == 1344 and asset.integrity["height"] == 768
        assert asset.stream["bytes_written"] > 0
        assert (tmp_path / asset.filename).exists()

    stages_seen = {data["stage"] for kind, data in events if kind == "stage"}
    assert {"payload", "gate1", "network", "transport", "integrity", "gate2", "qa"} <= stages_seen

    entries = storage.read_manifest()
    assert len(entries) == 2
    assert entries[0]["sha256"]


def test_blocked_prompt_never_reaches_the_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGESTUDIO_ASSETS", str(tmp_path))
    request = GenerationRequest(prompt="explicit sexual imagery of a minor", provider="mock")
    result = Studio(provider=build("mock")).generate(request)
    assert result.error_code == "sentinel_block"
    assert result.assets == []


def test_auto_resolution_always_yields_a_usable_engine():
    provider = resolve("auto")
    assert provider.available
