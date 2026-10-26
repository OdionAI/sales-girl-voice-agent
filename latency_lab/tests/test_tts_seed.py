from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.providers import resolve_tts_seed


def test_explicit_seed_wins():
    cfg = LabConfig(tts_voice="wema-en-ng-male", tts_seed="12345")
    assert resolve_tts_seed(cfg) == "12345"


def test_derived_seed_is_stable_per_voice():
    a = resolve_tts_seed(LabConfig(tts_voice="wema-en-ng-male"))
    b = resolve_tts_seed(LabConfig(tts_voice="wema-en-ng-male"))
    assert a and a == b
    assert a.isdigit()


def test_different_voices_get_different_seeds():
    seeds = {resolve_tts_seed(LabConfig(tts_voice=v)) for v in
             ["wema-en-ng-female", "wema-en-ng-male", "wema-en-us-female", "wema-en-us-male", "helen-mavino-0030"]}
    assert len(seeds) == 5


def test_no_voice_no_seed_is_empty():
    assert resolve_tts_seed(LabConfig(tts_voice="", tts_seed="")) == ""


def test_runtime_override_carries_seed():
    cfg = apply_runtime_overrides(LabConfig(), {"tts_voice_id": "wema-en-us-female", "tts_seed": "777"})
    assert cfg.tts_voice == "wema-en-us-female"
    assert cfg.tts_seed == "777"
    assert resolve_tts_seed(cfg) == "777"
