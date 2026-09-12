"""Session usage observations, independent of wallet charging and audio delivery."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class UsageCollector:
    def __init__(self) -> None:
        self.revision = 0
        self.llm_requests: dict[int, dict[str, int]] = {}
        self.stt_seconds = 0.0
        self.tts_characters = 0

    def llm_started(self) -> int:
        request_id = len(self.llm_requests) + 1
        self.llm_requests[request_id] = {}
        self.revision += 1
        return request_id

    def llm_usage(self, request_id: int, usage: Any) -> None:
        if not isinstance(usage, dict) or request_id not in self.llm_requests:
            return
        values = {}
        details = usage.get("prompt_tokens_details") or {}
        for target, value in (
            ("llm_input_tokens", usage.get("prompt_tokens")),
            ("llm_output_tokens", usage.get("completion_tokens")),
            ("llm_cached_tokens", details.get("cached_tokens") if isinstance(details, dict) else None),
        ):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[target] = value
        # Some servers send cumulative usage more than once for the same request.
        self.llm_requests[request_id].update(values)
        self.revision += 1

    def stt_submitted(self, pcm_bytes: int) -> None:
        self.stt_seconds += pcm_bytes / (16000 * 2)
        self.revision += 1

    def tts_accepted(self, text: str) -> None:
        self.tts_characters += len(text)
        self.revision += 1

    def snapshot(self, *, final: bool = False) -> dict[str, Any]:
        status = {"stt_seconds": "recorded", "tts_characters": "recorded"}
        result: dict[str, Any] = {
            "stt_seconds": round(self.stt_seconds, 6), "tts_characters": self.tts_characters,
        }
        for field in ("llm_input_tokens", "llm_output_tokens", "llm_cached_tokens"):
            observed = [r[field] for r in self.llm_requests.values() if field in r]
            result[field] = sum(observed) if observed or not self.llm_requests else None
            status[field] = ("recorded" if len(observed) == len(self.llm_requests)
                             else "partial" if observed else "unavailable")
        return {
            "revision": self.revision + int(final), "final": final, **result,
            "measurement_status": status, "llm_requests": len(self.llm_requests),
            "measurement_basis": {
                "llm": "provider_reported_tokens_including_tools_speculation_and_compaction",
                "stt": "pcm_audio_submitted_including_warmup_realtime_and_batch",
                "tts": "characters_accepted_including_interrupted_and_speculative_requests",
            },
        }


class UsageReporter:
    """Coalesce cumulative snapshots; never await billing in an audio callback."""

    INTERVAL_SECONDS = 10.0

    def __init__(self, config, session, conversations, collector, trace) -> None:
        self.config, self.session, self.conversations = config, session, conversations
        self.collector, self.trace = collector, trace
        self.task: asyncio.Task | None = None
        self.last_revision = -1

    def start(self) -> None:
        if self.config.billing_service_base_url and self.config.billing_service_token:
            self.task = asyncio.create_task(self._run())
        else:
            self.trace.record("usage_reporting_unavailable", reason="billing_service_not_configured")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL_SECONDS)
            await self.flush()

    async def flush(self, *, final: bool = False) -> None:
        if not self.conversations.session_id or not self.config.billing_service_base_url or not self.config.billing_service_token:
            return
        snapshot = self.collector.snapshot(final=final)
        if not final and snapshot["revision"] == self.last_revision:
            return
        payload = {"session_id": self.conversations.session_id,
                   "conversation_id": self.conversations.conversation_id, "usage": snapshot}
        try:
            async with self.session.post(
                self.config.billing_service_base_url.rstrip("/") + "/v1/internal/credits/observed-usage",
                json=payload, timeout=aiohttp.ClientTimeout(total=4),
                headers={"X-Service-Token": self.config.billing_service_token,
                         "X-Service-Name": self.config.agent_client_id,
                         "X-Business-ID": self.conversations._headers()["X-Business-ID"]},
            ) as response:
                response.raise_for_status()
                await response.read()
            self.last_revision = snapshot["revision"]
            self.trace.record("usage_persisted", reason="final" if final else "live",
                              canonical_session_id=self.conversations.session_id, usage=snapshot)
        except (aiohttp.ClientError, TimeoutError) as exc:
            self.trace.record("usage_persist_failed", reason=type(exc).__name__,
                              canonical_session_id=self.conversations.session_id, usage=snapshot)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for _ in range(2):
            await self.flush(final=True)
            if self.last_revision == self.collector.snapshot(final=True)["revision"]:
                break
