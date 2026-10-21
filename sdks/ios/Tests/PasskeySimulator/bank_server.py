"""Loopback-only bank simulator. Synthetic credentials; no banking API or money movement."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
import wave
from pathlib import Path

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from livekit import api, rtc

TOPIC = "odion.auth.passkey"
URL = "ws://127.0.0.1:7880"


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token(room: str, identity: str, *, agent: bool = False) -> str:
    value = api.AccessToken("devkey", "secret").with_identity(identity).with_grants(
        api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                        can_publish_data=True, can_update_own_metadata=agent)
    )
    if agent:
        value = value.with_kind("agent").with_attributes({"lk.agent.state": "listening"})
    return value.to_jwt()


class Bank:
    def __init__(self, greeting: Path, evidence: Path):
        self.greeting = greeting
        self.evidence = evidence
        self.room = None
        self.context = None
        self.active = None
        self.public_key = None
        self.stage = "Start the local call"
        self.events = []
        self.tasks = set()
        self.audio_frames = 0
        self.verifying = False

    def record(self, event: str, **safe_fields):
        entry = {"event": event, "time": time.time(), **safe_fields}
        self.events.append(entry)
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        with self.evidence.open("a") as file:
            file.write(json.dumps(entry) + "\n")
        print(json.dumps(entry), flush=True)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self, request):
        if self.room:
            for task in list(self.tasks):
                task.cancel()
            await self.room.disconnect()
        data = await request.json()
        self.public_key = serialization.load_der_public_key(base64.b64decode(data["publicKey"]))
        if not isinstance(self.public_key, ec.EllipticCurvePublicKey):
            raise web.HTTPBadRequest()
        name = "simulated-bank-" + uuid.uuid4().hex
        self.room = rtc.Room()
        self.context = None
        self.active = None
        self.verifying = False
        self.stage = "Local agent joining"
        self.room.on("data_received", lambda packet: self.task(self.receive(packet)))
        self.room.on("participant_connected", lambda participant: self.task(self.caller_ready(participant)))
        await self.room.connect(URL, token(name, "simulated-bank-agent", agent=True))
        self.record("local_agent_connected")
        return web.json_response({"serverUrl": URL, "roomName": name, "participantToken": token(name, "simulated-caller")})

    async def send(self, kind: str, **fields):
        payload = {"version": 1, "type": kind, "contextId": self.context, **fields}
        await self.room.local_participant.publish_data(json.dumps(payload), topic=TOPIC,
                                                      destination_identities=["simulated-caller"], reliable=True)

    async def caller_ready(self, participant):
        if participant.identity != "simulated-caller":
            return
        # Advertise readiness only after the worker can bind packets to this caller.
        await self.room.local_participant.set_attributes({"attentive.passkeys.version": "1"})
        await self.speak()

    async def receive(self, packet):
        if packet.topic != TOPIC or not packet.participant or packet.participant.identity != "simulated-caller":
            return
        value = json.loads(packet.data)
        if value.get("version") != 1:
            return
        kind = value.get("type")
        if kind == "session_context":
            self.context = value["contextId"]
            await self.send("session_context_ack")
            self.stage = "Context acknowledged; choose a test"
            self.record("context_acknowledged")
            return
        if value.get("contextId") != self.context or not self.active or value.get("authenticationId") != self.active["id"]:
            return
        if kind == "fido2_cancelled":
            self.stage = "Cancelled — no action authorized"
            self.active = None
            self.record("cancelled")
        elif kind == "fido2_failed":
            native = self.active["mode"] == "native"
            self.stage = "Native request rejected safely" if native else "SDK rejected request"
            self.record("native_rejected" if native else "sdk_failed", reason=value.get("reason"))
            self.active = None
        elif kind == "fido2_proof":
            active, self.active = self.active, None  # Single use, including rejected responses.
            self.verifying = True
            valid = self.verify(value.get("proof", {}), active)
            self.stage = "Proof received; verifying simulated bank result"
            self.record("proof_received", signature_valid=valid)
            await asyncio.sleep(2)
            verified = valid and active["mode"] == "approve" and time.time() < active["expires"]
            await self.send("fido2_result", authenticationId=active["id"],
                            status="verified" if verified else "failed", authenticated=verified)
            self.stage = "Verified — SIMULATED ONLY" if verified else "Rejected — no action authorized"
            self.record("verified" if verified else "rejected", signature_valid=valid)
            self.verifying = False

    def verify(self, proof, active) -> bool:
        try:
            response = proof["response"]
            client = unb64(response["clientDataJSON"])
            fields = json.loads(client)
            auth_data = unb64(response["authenticatorData"])
            if (fields["challenge"] != active["challenge"] or fields["type"] != "webauthn.get"
                    or fields["origin"] != "https://auth.bank.example"
                    or auth_data[:32] != hashlib.sha256(b"auth.bank.example").digest()
                    or len(auth_data) < 37 or auth_data[32] & 5 != 5):
                return False
            self.public_key.verify(unb64(response["signature"]), auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
            return True
        except (KeyError, ValueError, TypeError, InvalidSignature):
            return False

    async def scenario(self, request):
        if not self.context or self.active or self.verifying:
            raise web.HTTPConflict()
        mode = (await request.json()).get("mode")
        if mode not in {"approve", "reject", "native"}:
            raise web.HTTPBadRequest()
        challenge = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self.active = {"id": str(uuid.uuid4()), "challenge": challenge, "expires": time.time() + 60, "mode": mode}
        self.stage = "Native system check" if mode == "native" else "Awaiting simulated credential approval"
        await self.send("fido2_ceremony", authenticationId=self.active["id"], action=1, feature="SimulatedAction",
                        mode="authenticate", expiresAt=self.active["expires"],
                        options={"rpId": "auth.bank.example", "challenge": challenge, "userVerification": "required"})
        self.record("ceremony_started", mode=mode)
        return web.json_response({"ok": True})

    async def status(self, request):
        return web.json_response({"stage": self.stage, "events": self.events, "audioFrames": self.audio_frames})

    async def speak(self):
        await self.room.local_participant.set_attributes({"lk.agent.state": "speaking"})
        source = rtc.AudioSource(24000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("simulated-agent", source)
        await self.room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        await asyncio.sleep(1)
        with wave.open(str(self.greeting)) as audio:
            while frame := audio.readframes(240):
                await source.capture_frame(rtc.AudioFrame(frame, 24000, 1, len(frame) // 2))
                self.audio_frames += 1
        await source.wait_for_playout()
        self.record("agent_greeting_published", frames=self.audio_frames)
        await self.room.local_participant.set_attributes({"lk.agent.state": "listening"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--greeting", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    bank = Bank(args.greeting, args.evidence)
    application = web.Application()
    application.add_routes([web.post("/start", bank.start), web.post("/scenario", bank.scenario), web.get("/status", bank.status)])
    web.run_app(application, host="127.0.0.1", port=8765, access_log=None)


if __name__ == "__main__":
    main()
