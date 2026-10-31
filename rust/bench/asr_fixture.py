"""Deterministic ASR protocol fixture for parity tests; NEVER a real-ASR benchmark.
Real DeepSeek and TTS can be retained in a private derived config while ASR is unavailable.
"""

import asyncio, base64, json, math, sys
from array import array
from aiohttp import web

TEXT = "Hello, can you hear me? Please answer briefly."


async def realtime(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_json({"type": "session.created"})
    voiced = 0
    emitted = False
    async for message in ws:
        if message.type != web.WSMsgType.TEXT:
            continue
        data = json.loads(message.data)
        if data["type"] == "session.update":
            await ws.send_json({"type": "session.updated"})
        elif data["type"] == "input_audio_buffer.append":
            samples = array("h", base64.b64decode(data["audio"]))
            rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
            if rms >= 280:
                voiced += len(samples)
            if voiced >= 1600 and not emitted:
                emitted = True
                for part in ["language", "English", "<asr_text>", TEXT]:
                    await ws.send_json({"type": "transcription.delta", "delta": part})
        elif data["type"] == "input_audio_buffer.commit" and data.get("final"):
            await ws.send_json({"type": "transcription.done", "text": TEXT})
            voiced = 0
            emitted = False
    return ws


async def final(request):
    await request.read()
    await asyncio.sleep(0.2)
    return web.json_response(
        {"text": TEXT, "choices": [{"message": {"content": TEXT}}]}
    )


app = web.Application(client_max_size=8 * 1024 * 1024)
app.add_routes(
    [
        web.get("/realtime", realtime),
        web.post("/chat/completions", final),
        web.post("/audio/transcriptions", final),
    ]
)


# Optional latency replay extends the existing protocol fixture. It simulates
# model delays locally; it never loads credentials or proxies a real provider.
import argparse
import io
import time
import wave
from pathlib import Path

REPLAY_TEXT = "Please tell me whether you can hear me clearly."
NAME_TEXT = "Actually, please tell me your name."
ARGS = None


def record(kind, **fields):
    if ARGS and ARGS.log:
        with Path(ARGS.log).open("a") as target:
            target.write(json.dumps({"event": kind, "mono_ns": time.perf_counter_ns(), **fields}) + "\n")


async def replay_realtime(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_json({"type": "session.created"})
    voiced = 0
    warm = False
    tasks = []

    async def hypothesis(text, delay=0):
        await asyncio.sleep(delay)
        if not ws.closed:
            # Matches connection.py's forced-language cumulative hypothesis
            # boundary: a standalone, text-empty marker before the new tokens.
            for part in ["language English<asr_text>", text]:
                record("asr_delta", delta=part)
                await ws.send_json({"type": "transcription.delta", "delta": part})

    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if data["type"] == "session.update":
                await ws.send_json({"type": "session.updated"})
            elif data["type"] == "input_audio_buffer.commit" and data.get("final") and ARGS.complete_hypothesis_replay:
                record("asr_flush_start")
                await asyncio.sleep(0.257)
                await hypothesis(REPLAY_TEXT)
                await ws.send_json({"type": "transcription.done", "text": REPLAY_TEXT})
                record("asr_flush_complete")
            elif data["type"] == "input_audio_buffer.commit" and not warm:
                warm = True
                for _ in range(5):
                    await hypothesis("Oh.")
            elif data["type"] == "input_audio_buffer.append":
                samples = array("h", base64.b64decode(data["audio"]))
                rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
                if rms >= 280:
                    voiced += len(samples)
                    if voiced == 3 * 1600:
                        await hypothesis("Please tell me.")
                    elif voiced == 12 * 1600:
                        await hypothesis("Please tell me whether you can.")
                    elif voiced == 23 * 1600:
                        # The last decoder result arrives near EOT. Its stable
                        # timer expires while authoritative HTTP ASR is pending.
                        if not ARGS.complete_hypothesis_replay:
                            tasks.append(asyncio.create_task(hypothesis(REPLAY_TEXT, 0.15)))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return ws


async def replay_final(request):
    body = await request.json()
    encoded = body["messages"][-1]["content"][0]["input_audio"]["data"]
    with wave.open(io.BytesIO(base64.b64decode(encoded))) as audio:
        duration = audio.getnframes() / audio.getframerate()
    text = NAME_TEXT if duration < 2.3 else REPLAY_TEXT
    record("asr_final_request", duration=duration, text=text)
    await asyncio.sleep(0.264 if ARGS.complete_hypothesis_replay else 0.35)
    record("asr_final_response", text=text)
    return web.json_response({"choices": [{"message": {"content": text}}]})


async def replay_llm(request):
    body = await request.json()
    question = body["messages"][-1]["content"]
    record("llm_request", question=question)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    await asyncio.sleep(0.7 if ARGS.complete_hypothesis_replay else 1.0)
    answer = "My name is Helen." if "Actually" in question else "Yes, I can hear you clearly."
    data = {"choices": [{"delta": {"content": answer}}]}
    try:
        if ARGS.complete_hypothesis_replay:
            first, rest = answer.split(" ", 1)
            initial = {"choices": [{"delta": {"content": first}}]}
            await response.write(f"data: {json.dumps(initial)}\n\n".encode())
            record("llm_first_text", question=question)
            await asyncio.sleep(0.24)
            data = {"choices": [{"delta": {"content": " " + rest}}]}
        await response.write(f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n".encode())
        await response.write_eof()
        record("llm_complete" if ARGS.complete_hypothesis_replay else "llm_first_text", question=question)
    except ConnectionError:
        record("llm_cancelled", question=question)
    return response


async def replay_tts(request):
    body = await request.json()
    assert body["voice"] == "helen-mavino-0030"
    record("tts_request", text=body["input"])
    await asyncio.sleep(0.54 if ARGS.complete_hypothesis_replay else 0.55)
    response = web.StreamResponse(headers={"Content-Type": "audio/pcm", "x-sample-rate": "24000"})
    await response.prepare(request)
    frequency = 660 if "Helen" in body["input"] else 440
    pcm = array("h", (int(5000 * math.sin(2 * math.pi * frequency * i / 24000)) for i in range(48000))).tobytes()
    try:
        for offset in range(0, len(pcm), 4800):
            await response.write(pcm[offset:offset + 4800])
            if offset == 0:
                record("tts_first_pcm", text=body["input"])
            await asyncio.sleep(0.02)
        await response.write_eof()
    except ConnectionError:
        record("tts_cancelled", text=body["input"])
    return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--latency-replay", action="store_true")
    parser.add_argument("--complete-hypothesis-replay", action="store_true")
    parser.add_argument("--write-config", type=Path)
    parser.add_argument("--log", type=Path)
    ARGS = parser.parse_args()
    if ARGS.latency_replay or ARGS.complete_hypothesis_replay:
        if ARGS.write_config:
            config = json.loads((Path(__file__).resolve().parents[1] / "fixtures/reference-config.json").read_text())
            config.update(
                stt_ws_url="ws://127.0.0.1:18093/realtime",
                stt_chat_url="http://127.0.0.1:18093/asr/chat/completions",
                stt_batch_url="http://127.0.0.1:18093/audio/transcriptions",
                llm_base_url="http://127.0.0.1:18093/llm/v1",
                llm_provider="deepseek", llm_model="deepseek-flash",
                llm_api_key="fixture-not-a-real-key",
                tts_url="http://127.0.0.1:18093/tts",
                opening_greeting_enabled=True,
            )
            if ARGS.complete_hypothesis_replay:
                config["stt_speculative_flush_ms"] = 350.0
            assert all(not v for k,v in config.items() if k.endswith("_token") or "secret" in k)
            ARGS.write_config.write_text(json.dumps(config, indent=2) + "\n")
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.add_routes([
            web.get("/realtime", replay_realtime),
            web.post("/asr/chat/completions", replay_final),
            web.post("/llm/v1/chat/completions", replay_llm),
            web.post("/tts", replay_tts),
        ])
    web.run_app(app, host="127.0.0.1", port=18093, print=None)
