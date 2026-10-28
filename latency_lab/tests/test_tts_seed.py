import asyncio

from latency_lab.config import LabConfig, apply_runtime_overrides
from latency_lab.providers import RTTSClient, WEMA_TTS_SEED, is_wema_voice, resolve_tts_seed

WEMA = ["wema-en-ng-female", "wema-en-ng-male", "wema-en-us-female", "wema-en-us-male"]


def test_explicit_seed_wins():
    cfg = LabConfig(tts_voice="wema-en-ng-male", tts_seed="12345")
    assert resolve_tts_seed(cfg) == "12345"


def test_one_constant_seed_for_all_four_wema_voices():
    # Helen's way: one constant clone seed on the request for all four voices.
    assert {resolve_tts_seed(LabConfig(tts_voice=v)) for v in WEMA} == {WEMA_TTS_SEED}
    assert WEMA_TTS_SEED == "0"


def test_non_wema_voice_seed_is_unchanged():
    seed = resolve_tts_seed(LabConfig(tts_voice="helen-mavino-0030"))
    assert seed and seed.isdigit() and seed != WEMA_TTS_SEED


def test_derived_seed_is_stable_per_voice():
    a = resolve_tts_seed(LabConfig(tts_voice="helen-mavino-0030"))
    b = resolve_tts_seed(LabConfig(tts_voice="helen-mavino-0030"))
    assert a and a == b


def test_no_voice_no_seed_is_empty():
    assert resolve_tts_seed(LabConfig(tts_voice="", tts_seed="")) == ""


def test_runtime_override_carries_seed():
    cfg = apply_runtime_overrides(LabConfig(), {"tts_voice_id": "wema-en-us-female", "tts_seed": "777"})
    assert cfg.tts_voice == "wema-en-us-female"
    assert cfg.tts_seed == "777"
    assert resolve_tts_seed(cfg) == "777"


def test_is_wema_voice():
    assert all(is_wema_voice(v) for v in WEMA)
    assert not is_wema_voice("helen-mavino-0030")
    assert not is_wema_voice("")


class _Resp:
    headers = {"x-sample-rate": "24000"}
    content = None

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.payload = None

    async def post(self, url, json):
        self.payload = json
        return _Resp()


def _request_payload(voice):
    session = _Session()
    client = RTTSClient(LabConfig(tts_voice=voice), session)
    asyncio.run(client.stream("Hello there."))
    return session.payload


def test_wema_request_pins_voice_with_constant_seed_and_xvector():
    for v in WEMA:
        payload = _request_payload(v)
        assert payload["voice"] == v
        assert payload["seed"] == WEMA_TTS_SEED == "0"
        assert payload["x_vector_only_mode"] is True


def test_non_wema_request_keeps_icl_and_derived_seed():
    payload = _request_payload("helen-mavino-0030")
    assert payload["x_vector_only_mode"] is False
    assert payload["seed"] == resolve_tts_seed(LabConfig(tts_voice="helen-mavino-0030"))
