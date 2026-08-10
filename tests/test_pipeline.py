"""Pipeline tests - run with `python -m pytest tests -q`.

Everything here is offline: the mock engine renders locally, so the full
six-stage pipeline is exercised without a key, a quota or a network.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from imagestudio import integrity, moderation, qa, redact, storage, styles, transport  # noqa: E402
from imagestudio.engine import Studio  # noqa: E402
from imagestudio.params import ASPECT_RATIOS, GenerationRequest, ParameterError  # noqa: E402
from imagestudio.providers import build, catalogue, resolve  # noqa: E402
from imagestudio.providers.base import classify_auth_error  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_assets(tmp_path, monkeypatch):
    """Every test writes into its own directory, and none inherits a cached root."""
    monkeypatch.setenv("IMAGESTUDIO_ASSETS", str(tmp_path))
    storage.reset_root_cache()
    yield
    storage.reset_root_cache()


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
        # The browser must be able to render/download the asset without a second
        # disk read - required on serverless deploys where /tmp is ephemeral.
        assert asset.data_url.startswith("data:image/png;base64,")

    stages_seen = {data["stage"] for kind, data in events if kind == "stage"}
    assert {"payload", "gate1", "network", "transport", "integrity", "gate2", "qa"} <= stages_seen

    entries = storage.read_manifest()
    assert len(entries) == 2
    assert entries[0]["sha256"]


def test_data_url_is_cleared_when_qa_discards_the_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGESTUDIO_ASSETS", str(tmp_path))
    request = GenerationRequest(
        prompt="a flat grey square",
        provider="mock",
        qa_threshold=10.0,   # nothing clears this bar - forces a flagged result
        qa_discard=True,
    )
    result = Studio(provider=build("mock")).generate(request)
    asset = result.assets[0]
    assert asset.status == "rejected"
    assert asset.error_code == "qa_below_threshold"
    assert asset.data_url == ""
    assert asset.url == ""
    assert not list(tmp_path.glob("*.png"))


def test_blocked_prompt_never_reaches_the_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGESTUDIO_ASSETS", str(tmp_path))
    request = GenerationRequest(prompt="explicit sexual imagery of a minor", provider="mock")
    result = Studio(provider=build("mock")).generate(request)
    assert result.error_code == "sentinel_block"
    assert result.assets == []


def test_auto_resolution_always_yields_a_usable_engine():
    provider = resolve("auto")
    assert provider.available


# ------------------------------------------------------- read-only deploys

def test_stream_sink_reports_an_unwritable_path_as_a_transport_error(tmp_path):
    """The Vercel crash, reproduced.

    A read-only bundle makes open() raise OSError, which Studio._one does not
    catch - one bad write took down the whole batch. Stage 4 must translate it.
    """
    blocker = tmp_path / "not-a-directory.txt"
    blocker.write_text("this is a file, so nothing can be created underneath it")

    with pytest.raises(transport.TransportError) as caught:
        transport.write_bytes(b"\x89PNG", blocker / "sub" / "asset.png")
    assert caught.value.code == "storage_unwritable"


def test_assets_root_falls_back_when_the_candidate_cannot_be_written(monkeypatch):
    monkeypatch.setattr(storage, "_is_writable", lambda path: False)
    storage.reset_root_cache()

    root = storage.assets_root()
    assert root == storage._fallback_root()
    assert storage.is_ephemeral()
    assert storage.library_stats()["ephemeral"] is True
    assert storage.library_stats()["note"]


def test_manifest_failure_never_costs_an_asset(monkeypatch):
    """A read-only manifest is a bookkeeping problem, not a generation failure."""
    def explode(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("builtins.open", explode)
    assert storage.record({"status": "accepted"}) is False


def test_batch_survives_a_read_only_asset_directory(tmp_path, monkeypatch):
    """End to end: the engine still runs, the failure is scoped to one asset."""
    monkeypatch.setattr(
        transport,
        "_open_for_write",
        lambda destination: (_ for _ in ()).throw(
            transport.TransportError("read-only", code="storage_unwritable")
        ),
    )
    result = Studio(provider=build("mock")).generate(
        GenerationRequest(prompt="a brass orrery", provider="mock", count=2)
    )
    assert result.error == ""                      # the batch itself completed
    assert [a.status for a in result.assets] == ["failed", "failed"]
    assert {a.error_code for a in result.assets} == {"storage_unwritable"}


# ------------------------------------------------------ bring-your-own key

def test_a_supplied_key_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-value")

    assert build("gemini").api_key == "env-key-value"
    assert build("gemini").key_source == "env"

    provider = build("gemini", {"gemini": "user-key-value"})
    assert provider.api_key == "user-key-value"
    assert provider.key_source == "user"


def test_a_supplied_key_unlocks_the_engine_in_the_catalogue(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    locked = {row["name"]: row for row in catalogue()}["openai"]
    assert locked["available"] is False and locked["needs_key"] is True
    assert locked["key_url"]                       # the vault can link somewhere

    unlocked = {row["name"]: row for row in catalogue({"openai": "sk-test-key"})}["openai"]
    assert unlocked["available"] is True
    assert unlocked["key_source"] == "user"


def test_a_key_never_reaches_the_serialized_request_or_the_manifest():
    secret = "sk-do-not-leak-me-0000"
    request = GenerationRequest(prompt="a tin robot", provider="mock", qa_threshold=0.0)
    result = Studio(provider=build("mock"), keys={"openai": secret}).generate(request)

    assert secret not in json.dumps(result.to_dict())
    assert secret not in json.dumps(storage.read_manifest())


def test_selecting_a_keyless_engine_asks_for_a_key_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("STABILITY_API_KEY", raising=False)
    result = Studio().generate(GenerationRequest(prompt="a tin robot", provider="stability"))

    assert result.error_code == "missing_key"
    assert result.needs_key == "stability"
    assert result.assets == []


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, "", "invalid_api_key"),
        (400, "API key not valid. Please pass a valid API key.", "invalid_api_key"),
        (403, "", "key_forbidden"),
        (402, "", "insufficient_credits"),
        (429, "insufficient_credits for this month", "insufficient_credits"),
        (429, "slow down", "rate_limited"),
    ],
)
def test_credential_failures_are_classified_specifically(status, body, expected):
    code, message = classify_auth_error(status, body, "Test Engine")
    assert code == expected
    assert "Test Engine" in message


def test_a_moderation_refusal_is_not_mistaken_for_a_key_problem():
    assert classify_auth_error(400, "content_policy_violation", "Test Engine") is None
    assert classify_auth_error(500, "internal error", "Test Engine") is None


# ---------------------------------------------------------------- scrubbing

def test_scrub_removes_a_supplied_key_from_echoed_text():
    secret = "sk-proj-abcdefghijklmnop1234"
    leaked = f"Engine returned 401: bad Authorization header 'Bearer {secret}'"
    cleaned = redact.scrub(leaked, [secret])
    assert secret not in cleaned
    assert "••••1234" in cleaned          # still identifies which key failed


def test_scrub_catches_key_shapes_it_was_never_told_about():
    for secret in ("sk-abcdefghijklmnopqrst", "hf_abcdefghijklmnopqrst", "AIzaSyAbcdefghijklmnopqrstuvw"):
        assert secret not in redact.scrub(f"error: {secret} rejected")


def test_scrub_leaves_a_sha256_digest_alone():
    """The manifest is full of hex digests; masking them would destroy provenance."""
    digest = "a" * 64
    assert redact.scrub(f"sha256={digest}") == f"sha256={digest}"


def test_scrub_deep_walks_nested_event_payloads():
    secret = "sk-nested-secret-000000"
    payload = {"assets": [{"error": f"rejected {secret}", "meta": {"seed": 7}}]}
    cleaned = redact.scrub_deep(payload, [secret])
    assert secret not in json.dumps(cleaned)
    assert cleaned["assets"][0]["meta"]["seed"] == 7


# ------------------------------------------------------------ retry budget

def test_the_retry_shield_stops_when_the_time_budget_is_gone():
    """A retryable 429 must not sleep past a serverless function's ceiling."""
    trace = transport.TransportTrace()
    keep_going = transport._record_retry(
        trace, attempt=0, max_retries=3, detail="HTTP 429", elapsed_ms=10,
        status=429, on_event=None, deadline=time.monotonic() + 0.001,
    )
    assert keep_going is False
    assert trace.attempts[-1].outcome == "failed"
    assert "no time left" in trace.attempts[-1].detail


def test_an_unbudgeted_request_still_retries():
    request = GenerationRequest(prompt="p").validate()
    assert request.deadline is None

    budgeted = GenerationRequest(prompt="p", budget_seconds=30).validate()
    assert budgeted.deadline is not None


# ----------------------------------------------------------- the web layer

@pytest.fixture()
def client():
    from webapp.app import app as flask_app

    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def test_sync_endpoint_returns_the_whole_event_log_and_the_result(client):
    """The serverless transport: no SSE, no cross-request job registry."""
    response = client.post("/api/generate/sync", json={
        "prompt": "a brass orrery on a walnut desk",
        "provider": "mock",
        "qa_threshold": 0.0,
    })
    assert response.status_code == 200
    body = response.get_json()

    kinds = [event["kind"] for event in body["events"]]
    assert "stage" in kinds and "asset_done" in kinds
    stages = {e["data"]["stage"] for e in body["events"] if e["kind"] == "stage"}
    assert {"payload", "gate1", "network", "transport", "integrity", "gate2", "qa"} <= stages

    asset = body["result"]["assets"][0]
    assert asset["status"] in ("accepted", "flagged")
    assert asset["preview_url"].startswith("data:image/jpeg;base64,")


def test_the_engine_catalogue_endpoint_applies_the_callers_keys(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    locked = client.post("/api/engines", json={}).get_json()["providers"]
    assert not next(p for p in locked if p["name"] == "openai")["available"]

    unlocked = client.post("/api/engines", json={"api_keys": {"openai": "sk-x"}}).get_json()["providers"]
    assert next(p for p in unlocked if p["name"] == "openai")["available"]


def test_an_unknown_engine_name_cannot_smuggle_a_key_through(client):
    from webapp.app import _keys_from

    assert _keys_from({"api_keys": {"not-an-engine": "sk-x", "openai": "sk-y"}}) == {"openai": "sk-y"}
    assert _keys_from({"api_keys": {"openai": "x" * 5000}}) == {}
    assert _keys_from({"api_keys": "not-a-dict"}) == {}


def test_validating_a_keyless_engine_needs_no_network(client):
    verdict = client.post("/api/keys/validate", json={"engine": "pollinations", "key": ""}).get_json()
    assert verdict["ok"] is True and verdict["code"] == "no_key_required"


def test_validating_an_empty_key_asks_for_one_instead_of_calling_out(client):
    verdict = client.post("/api/keys/validate", json={"engine": "openai", "key": ""}).get_json()
    assert verdict["ok"] is False and verdict["code"] == "missing_key"
