"""Serial, loopback-only LiveKit call comparison. Provider credentials stay in a private config.
Uses the exact same input file, caller, Opus transport, rates, provider config for both workers.
Records decoded receiver PCM onset, not a physical audio device's audible onset.
"""

import asyncio, collections, json, math, os, signal, sys, time, wave, hashlib
from array import array
from pathlib import Path
import psutil
from livekit import rtc, api

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:17880"


def now():
    return time.perf_counter_ns()


class Monitor:
    def __init__(self, pid):
        self.pid = pid
        self.cpu = {}
        self.samples = []
        self.stop = False

    async def run(self):
        while not self.stop:
            rows = []
            try:
                root = psutil.Process(self.pid)
                processes = [root] + root.children(recursive=True)
            except psutil.Error:
                processes = []
            for p in processes:
                try:
                    c = p.cpu_times()
                    self.cpu[p.pid] = max(self.cpu.get(p.pid, 0), c.user + c.system)
                    rows.append(
                        {
                            "pid": p.pid,
                            "rss": p.memory_info().rss,
                            "cpu_s": c.user + c.system,
                        }
                    )
                except psutil.Error:
                    pass
            self.samples.append({"mono_ns": now(), "processes": rows})
            await asyncio.sleep(0.05)

    def total_cpu(self):
        return sum(self.cpu.values())


async def wait_ready(proc, log, native):
    async def drain():
        async for line in proc.stdout:
            text = line.decode(errors="replace")
            log.write(text)
            log.flush()
            if ("worker_ready" if native else "registered worker") in text:
                ready.set()

    ready = asyncio.Event()
    task = asyncio.create_task(drain())
    await asyncio.wait_for(ready.wait(), 60)
    return task


async def one(kind, index, private, reference, output, binary):
    interruption = kind.endswith("-interrupt")
    idle_check = kind.endswith("-idle")
    label = kind
    kind = kind.split("-")[0]
    folder = output / f"{index:02d}-{label}"
    folder.mkdir(parents=True)
    agent = f"sg-rust-{kind}-{index}-{os.getpid()}"
    env = dict(
        os.environ,
        SG_PYTHON_REFERENCE=str(reference),
        SG_PRIVATE_CONFIG=str(private),
        SG_AGENT_NAME=agent,
        SG_TRACE_DIRECTORY=str(folder),
        PYTHONUNBUFFERED="1",
    )
    command = (
        [str(binary), str(private), str(folder), agent]
        if kind == "rust"
        else [sys.executable, str(ROOT / "bench/python_worker.py"), "start"]
    )
    log = (folder / "worker.log").open("w")
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    monitor = Monitor(proc.pid)
    sampling = asyncio.create_task(monitor.run())
    reader = None
    room = rtc.Room()
    lk = api.LiveKitAPI(URL, "devkey", "secret")
    tasks = []
    trace = []
    pcm = bytearray()
    status = asyncio.Queue()
    phase = "startup"
    active = False
    job = None
    source = None
    response_audio = asyncio.Event()
    disconnected = asyncio.Event()

    def event(name, **data):
        trace.append({"event": name, "mono_ns": now(), "phase": phase, **data})

    try:
        reader = await wait_ready(proc, log, kind == "rust")
        event("worker_registered")
        base = monitor.total_cpu()
        t0 = now()
        await asyncio.sleep(2)
        idle = {
            "wall_s": (now() - t0) / 1e9,
            "cpu_s": monitor.total_cpu() - base,
            "rss_bytes": sum(p["rss"] for p in monitor.samples[-1]["processes"]),
        }
        event("idle_complete")
        roomname = f"sg-rvc-{kind}-{index}-{os.getpid()}"
        await lk.room.create_room(
            api.CreateRoomRequest(name=roomname, empty_timeout=30)
        )
        token = (
            api.AccessToken("devkey", "secret")
            .with_identity("sg-rvc-caller")
            .with_grants(api.VideoGrants(room_join=True, room=roomname))
            .to_jwt()
        )

        @room.on("data_received")
        def data(packet):
            if packet.topic == "sg-rvc.status":
                item = json.loads(packet.data)
                event("status", body=item)
                status.put_nowait(item)

        @room.on("disconnected")
        def lost(*_):
            event("room_disconnected")
            disconnected.set()

        @room.on("track_subscribed")
        def track_subscribed(track, publication, participant):
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return

            async def receive():
                nonlocal active
                stream = rtc.AudioStream(
                    track, sample_rate=24000, num_channels=1, frame_size_ms=10
                )
                async for item in stream:
                    block = bytes(item.frame.data)
                    pcm.extend(block)
                    samples = array("h", block)
                    rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
                    current = rms >= 80
                    if current and phase == "response":
                        response_audio.set()
                    if current != active:
                        active = current
                        event(
                            "receiver_active" if current else "receiver_quiet",
                            rms=rms,
                            decoded_samples=len(pcm) // 2,
                        )
                await stream.aclose()

            tasks.append(asyncio.create_task(receive()))

        await room.connect("ws://127.0.0.1:17880", token)
        source = rtc.AudioSource(16000, 1, queue_size_ms=100)
        track = rtc.LocalAudioTrack.create_audio_track("test-input", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        blocks = collections.deque()
        sent = asyncio.Event()

        async def microphone():
            deadline = asyncio.get_running_loop().time()
            while True:
                if blocks:
                    block, tag = blocks.popleft()
                    if tag:
                        event(tag)
                    if not blocks:
                        sent.set()
                else:
                    block = bytes(3200)
                await source.capture_frame(rtc.AudioFrame(block, 16000, 1, 1600))
                deadline += 0.1
                await asyncio.sleep(
                    max(0, deadline - asyncio.get_running_loop().time())
                )

        tasks.append(asyncio.create_task(microphone()))
        event("dispatch_requested")
        job = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent, room=roomname, metadata="native-rvc-one-call"
            )
        )

        async def wait_type(kind, timeout=90):
            async with asyncio.timeout(timeout):
                while True:
                    item = await status.get()
                    if item["type"] == "error":
                        raise RuntimeError(item["error"])
                    if item["type"] == kind:
                        return item

        await wait_type("ready")
        event("session_ready")
        await room.local_participant.publish_data(
            json.dumps({"type": "client_ready"}), reliable=True, topic="sg-rvc.control"
        )
        await wait_type("complete")
        event("opening_complete")
        await asyncio.sleep(0.6)
        with wave.open(str(ROOT / "fixtures/input-neutral.wav"), "rb") as f:
            assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (
                16000,
                1,
                2,
            )
            raw = f.readframes(f.getnframes())
        phase = "response"
        active = False
        cpu_start = monitor.total_cpu()
        response_start = now()
        sent.clear()
        pieces = [
            raw[i : i + 3200].ljust(3200, b"\0") for i in range(0, len(raw), 3200)
        ]
        last_voice = max(
            i
            for i, b in enumerate(pieces)
            if math.sqrt(sum(s * s for s in array("h", b)) / 1600) >= 280
        )
        for i, b in enumerate(pieces):
            blocks.append(
                (
                    b,
                    (
                        "input_start"
                        if i == 0
                        else (
                            "input_last_voiced_block"
                            if i == last_voice
                            else ("input_last_block" if i == len(pieces) - 1 else None)
                        )
                    ),
                )
            )
        await sent.wait()
        if interruption:
            await asyncio.wait_for(response_audio.wait(), 60)
            await wait_type("final")
            await asyncio.sleep(0.2)
            phase = "interruption"
            event("interrupt_queued")
            sent.clear()
            with wave.open(str(ROOT / "fixtures/input-interrupt.wav"), "rb") as f:
                other = f.readframes(f.getnframes())
            for i in range(0, len(other), 3200):
                blocks.append(
                    (
                        other[i : i + 3200].ljust(3200, b"\0"),
                        "interrupt_input_start" if i == 0 else None,
                    )
                )
            await sent.wait()
            await wait_type("final")
        else:
            # An idle check-in after ASR failure is not an answered caller turn.
            await wait_type("final")
        await wait_type("complete")
        event("response_complete")
        if idle_check:
            await asyncio.wait_for(disconnected.wait(), 75)
            event("idle_close_observed")
        await asyncio.sleep(0.4)
        active_summary = {
            "wall_s": (now() - response_start) / 1e9,
            "cpu_s": monitor.total_cpu() - cpu_start,
        }
        phase = "teardown"
        await room.disconnect()
        event("caller_disconnected")
        await asyncio.sleep(1)
        if job:
            try:
                await lk.agent_dispatch.delete_dispatch(job.id, roomname)
            except Exception:
                if not idle_check:
                    raise
            job = None
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), 20)
        final_seen = any(
            e["event"] == "status"
            and e["phase"] == "response"
            and e["body"]["type"] == "final"
            for e in trace
        )
        summary = {
            "outcome": "completed" if final_seen else "failed-no-authoritative-final",
            "kind": kind,
            "mode": (
                "interruption" if interruption else "idle" if idle_check else "normal"
            ),
            "index": index,
            "idle": idle,
            "active": active_summary,
            "peak_rss_bytes": max(
                sum(p["rss"] for p in s["processes"]) for s in monitor.samples
            ),
            "peak_process_count": max(len(s["processes"]) for s in monitor.samples),
            "total_cpu_s": monitor.total_cpu(),
            "worker_exit": proc.returncode,
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "instrumentation": "Both synchronous JSONL per event; native trace schema is smaller than reference; full worker cost, not pure coordinator CPU",
        }
        (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await room.disconnect()
        if job:
            try:
                await lk.agent_dispatch.delete_dispatch(job.id, roomname)
            except Exception:
                pass
        await lk.aclose()
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        monitor.stop = True
        await sampling
        if reader:
            await reader
        log.close()
        (folder / "caller.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in trace)
        )
        (folder / "process-samples.json").write_text(json.dumps(monitor.samples))
        with wave.open(str(folder / "receiver.wav"), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(24000)
            f.writeframes(pcm)


async def main():
    private = Path(sys.argv[1])
    reference = Path(sys.argv[2])
    output = Path(sys.argv[3])
    binary = Path(sys.argv[4])
    order = sys.argv[5:] or ["python", "rust"]
    assert private.is_absolute() and output.is_absolute() and binary.is_absolute()
    output.mkdir(parents=True, exist_ok=True)
    for index, kind in enumerate(order):
        result = await one(kind, index, private, reference, output, binary)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
