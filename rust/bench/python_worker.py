"""Local-only adapter around the UNMODIFIED pinned Python coordinator/providers/transport.
No configured identity, business tools or authenticated side-effect services are loaded.
Benchmark mode uses zero idle child processes on both sides; active Python child is included.
"""

import asyncio, dataclasses, json, os, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["SG_PYTHON_REFERENCE"])
from livekit import rtc
from livekit.agents import cli, WorkerOptions, JobContext, JobRequest
from latency_lab.config import LabConfig
from latency_lab.pipeline import ConversationPipeline
from latency_lab.trace import TraceRecorder
from latency_lab.livekit_worker import LiveKitPCMTransport


async def accept(request: JobRequest):
    if (
        request.job.metadata != "native-rvc-one-call"
        or not request.job.room.name.startswith("sg-rvc-")
    ):
        await request.reject()
        return
    await request.accept()


async def entrypoint(ctx: JobContext):
    if ctx.job.metadata != "native-rvc-one-call" or not ctx.job.room.name.startswith(
        "sg-rvc-"
    ):
        raise RuntimeError("unsupported test dispatch")
    raw = json.loads(Path(os.environ["SG_PRIVATE_CONFIG"]).read_text())
    keys = {f.name for f in dataclasses.fields(LabConfig)}
    config = LabConfig(**{k: v for k, v in raw.items() if k in keys})
    assert not any(
        v
        for k, v in dataclasses.asdict(config).items()
        if k.endswith("_token") or "secret" in k
    )
    trace = TraceRecorder(Path(os.environ["SG_TRACE_DIRECTORY"]), ctx.job.id)
    await ctx.connect()
    caller = await ctx.wait_for_participant(identity="sg-rvc-caller")
    source = rtc.AudioSource(24000, 1, queue_size_ms=240)
    track = rtc.LocalAudioTrack.create_audio_track("rvc-python-output", source)
    publication = await ctx.room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    stop = asyncio.Event()

    async def end_call(reason):
        # Same local room deletion as the pinned worker's adapter. This handler
        # is transport plumbing, not a replacement for the reference policy.
        deleted = ctx.delete_room()
        if hasattr(deleted, "__await__"):
            await deleted
        stop.set()

    transport = LiveKitPCMTransport(
        source,
        trace,
        sample_rate=24000,
        local_participant=ctx.room.local_participant,
        end_call_handler=end_call,
    )
    transport.user_identity = caller.identity
    transport.publication_sid = publication.sid
    pipeline = None
    ready = asyncio.Event()
    inputs = asyncio.Lock()

    async def status(body):
        await ctx.room.local_participant.publish_data(
            json.dumps(body),
            reliable=True,
            topic="sg-rvc.status",
            destination_identities=[caller.identity],
        )

    async def send(message):
        await transport.send(message)
        kind = message.get("type")
        if kind == "lab_ready":
            ready.set()
            await status({"type": "ready"})
        elif kind == "partial_user_request":
            await status({"type": "partial", "text": message.get("content")})
        elif kind == "final_user_request":
            await status({"type": "final", "text": message.get("content")})
        elif kind == "final_assistant_answer":
            await status({"type": "answer", "text": message.get("content")})
        elif kind == "tts_end":
            await status(
                {
                    "type": "complete",
                    "turn": getattr(pipeline, "turn_id", None) or "opening",
                }
            )

    pipeline = ConversationPipeline(config, trace, send, transport="livekit")
    assert (
        pipeline.system_prompt == raw["base_system_prompt"]
        and not pipeline.action_tools
    )
    transport.bind(pipeline)

    @ctx.room.on("participant_disconnected")
    def left(p):
        if p.identity == caller.identity:
            stop.set()

    @ctx.room.on("data_received")
    def data(packet):
        # Startup client_ready is sent once below, as in the pinned worker.
        pass

    task = None
    opening = None
    try:
        await pipeline.start()
        ready.set()
        stream = rtc.AudioStream.from_participant(
            participant=caller,
            track_source=rtc.TrackSource.SOURCE_MICROPHONE,
            sample_rate=16000,
            num_channels=1,
            frame_size_ms=100,
        )

        async def audio():
            async for item in stream:
                async with inputs:
                    await pipeline.audio(bytes(item.frame.data))

        task = asyncio.create_task(audio())
        await pipeline.client_event(
            "client_ready",
            {
                "mic_sample_rate": 16000,
                "playback_sample_rate": 24000,
                "playback_prebuffer_ms": 0,
                "livekit_output_queue_ms": 240,
            },
        )
        if config.opening_greeting_enabled:
            opening = asyncio.create_task(
                pipeline.speak_opening(pipeline.opening_greeting_text())
            )
        await stop.wait()
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if opening and not opening.done():
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
        await pipeline.close()
        await transport.close()
        await ctx.room.disconnect()
        trace.record("local_harness_close")


if __name__ == "__main__":
    name = os.environ["SG_AGENT_NAME"]
    assert name.startswith("sg-rust-")
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=accept,
            agent_name=name,
            ws_url="ws://127.0.0.1:17880",
            api_key="devkey",
            api_secret="secret",
            host="127.0.0.1",
            port=18084,
            num_idle_processes=0,
            load_threshold=float("inf"),
            session_end_timeout=10,
            drain_timeout=10,
        )
    )
