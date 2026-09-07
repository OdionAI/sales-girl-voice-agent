from __future__ import annotations

import asyncio
import base64
import math
import re
import struct
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from .config import LabConfig
from .agentic import (
    AgentRuntimeContext,
    KnowledgeClient,
    KnowledgeResult,
    requests_knowledge_lookup,
    spoken_knowledge_acknowledgement,
)
from .action_tools import (
    ActionToolCall,
    ActionToolExecutor,
    PendingAction,
    action_tool_definitions,
    confirmation_decision,
    confirmation_prompt,
    explicit_end_call_intent,
)
from .providers import RLLMClient, RSTTClient, RTTSClient
from .trace import TraceRecorder


class Outbound(Protocol):
    async def __call__(self, message: dict[str, Any]) -> None: ...


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def rms_pcm16(pcm: bytes) -> float:
    count = len(pcm) // 2
    if not count:
        return 0.0
    values = struct.unpack(f"<{count}h", pcm[: count * 2])
    return math.sqrt(sum(value * value for value in values) / count)


class PhraseTokenizer:
    """Stable phrase boundary: punctuation, or a bounded long clause."""

    _boundary = re.compile(r"^(.+?[.!?](?:[\"']|\s|$))", re.S)

    def __init__(self, max_words: int = 24) -> None:
        self.buffer = ""
        self.max_words = max_words

    def push(self, delta: str) -> list[str]:
        self.buffer += delta
        phrases: list[str] = []
        while True:
            match = self._boundary.match(self.buffer)
            if match:
                phrase = match.group(1).strip()
                self.buffer = self.buffer[match.end() :]
                if phrase:
                    phrases.append(phrase)
                continue
            words = self.buffer.split()
            if len(words) >= self.max_words:
                phrase = " ".join(words[: self.max_words]).strip()
                self.buffer = self.buffer[len(phrase) :]
                phrases.append(phrase)
                continue
            break
        return phrases

    def finish(self) -> list[str]:
        phrase = self.buffer.strip()
        self.buffer = ""
        return [phrase] if phrase else []


@dataclass
class Attempt:
    id: str
    hypothesis: str
    speculative: bool
    authorized: bool = False
    task: asyncio.Task[None] | None = None
    cancelled: bool = False
    answer: str = ""
    held_audio: list[tuple[int, bytes]] = field(default_factory=list)
    audio_complete: bool = False
    response_committed: bool = False
    authorization_event: asyncio.Event = field(default_factory=asyncio.Event)
    initial_answer: str = ""
    spoken_knowledge_acknowledgement: str = ""
    knowledge_query: str = ""
    knowledge_gate_reason: str = ""
    knowledge_task: asyncio.Task[KnowledgeResult] | None = None
    knowledge_result: KnowledgeResult | None = None
    tool_calls: list[ActionToolCall] = field(default_factory=list)
    end_call_requested: bool = False


class ConversationPipeline:
    """Single owner for turn state, speculation validity, and playback release."""

    def __init__(
        self,
        config: LabConfig,
        trace: TraceRecorder,
        send: Outbound,
        *,
        transport: str = "browser",
        agent_context: AgentRuntimeContext | None = None,
    ) -> None:
        self.config = config
        self.trace = trace
        self.send = send
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        self.llm = RLLMClient(config, self.http)
        self.tts = RTTSClient(config, self.http)
        self.stt = RSTTClient(config, self.on_partial, self.on_final, self._provider_event)
        self.agent_context = agent_context or AgentRuntimeContext()
        self.system_prompt = self.agent_context.system_prompt(config.system_prompt)
        self.knowledge = KnowledgeClient(config, self.http, self.agent_context)
        self.action_tools = action_tool_definitions(self.agent_context)
        self.action_executor = ActionToolExecutor(
            config, self.http, self.agent_context, self.trace
        )
        self.pending_action: PendingAction | None = None
        self.transport = transport
        self.state = "idle"
        self.turn_id: str | None = None
        self.turn_number = 0
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.last_partial = ""
        self.final_text = ""
        self.attempt: Attempt | None = None
        self.stability_task: asyncio.Task[None] | None = None
        self.history: list[dict[str, str]] = []
        self.closed_turns: set[str] = set()
        self.last_closed_turn_id: str | None = None
        self.silence_candidate = False
        self.last_client_network_ms: float | None = None
        self.speech_candidate_active = False
        self.candidate_turn_id: str | None = None
        self.candidate_client_sent_ms: float | None = None
        self.candidate_client_network_ms: float | None = None
        self.preconfirm_partial: tuple[str, str] | None = None
        self.final_timeout_task: asyncio.Task[None] | None = None
        self.turn_pcm16 = bytearray()

    async def start(self) -> None:
        self.trace.record(
            "session_open",
            reason=f"{self.transport}_transport_accepted",
            transport=self.transport,
            end_silence_ms=self.config.end_silence_ms,
            tts_mode=(
                "cached_icl_voice"
                if self.config.tts_voice
                else "icl_base"
                if self.config.tts_ref_audio
                else "voice_design"
            ),
            tts_reference=(
                self.config.tts_voice
                or (Path(self.config.tts_ref_audio).name if self.config.tts_ref_audio else None)
            ),
            initial_codec_chunk_frames=self.config.tts_initial_codec_chunk_frames,
            agent_config_loaded=self.agent_context.loaded,
            configured_agent_id=self.agent_context.agent_id or None,
            configured_agent_name=self.agent_context.name or None,
            business_id=self.agent_context.business_id or None,
            knowledge_base_count=len(self.agent_context.knowledge_base_ids),
            configured_tool_count=len(self.agent_context.tools),
            supported_action_tool_count=len(self.action_tools),
            action_service_configured=bool(
                self.config.conversation_service_base_url
                and self.config.conversation_service_token
            ),
            system_prompt_chars=len(self.system_prompt),
            stt_final_authority="qwen_batch",
            stt_realtime_role="optional_speculation_only",
            stt_batch_url=self.config.stt_batch_url,
            stt_recovery_buffer_seconds=self.config.stt_recovery_buffer_seconds,
            stt_rotate_after_final=self.config.stt_rotate_after_final,
        )
        await self.stt.connect()
        await self.send({"type": "lab_status", "content": "RSTT connected; lab ready"})
        await self.send({"type": "lab_ready", "content": "ready"})

    async def speak_opening(self, text: str) -> bool:
        """Speak one proactive greeting without manufacturing a caller turn."""
        greeting = str(text or "").strip()
        if not greeting or self.state != "idle" or self.turn_id is not None:
            return False

        opening_turn_id = "opening-0000"
        attempt = Attempt(
            id=f"opening-{uuid.uuid4().hex[:8]}",
            hypothesis="",
            speculative=False,
            authorized=True,
        )
        attempt.authorization_event.set()
        self.turn_id = opening_turn_id
        self.attempt = attempt
        self.trace.record(
            "opening_greeting_start",
            turn_id=opening_turn_id,
            attempt_id=attempt.id,
            reason="agent_ready_before_first_caller_turn",
            text=greeting,
        )
        self._transition("generating", "proactive_opening_greeting")
        try:
            await self._speak_text(attempt, greeting, phase="opening_greeting")
            attempt.audio_complete = True
            await self.send(
                {"type": "final_assistant_answer", "content": attempt.answer.strip()}
            )
            self.history.append(
                {"role": "assistant", "content": attempt.answer.strip()}
            )
            self.trace.record(
                "opening_greeting_complete",
                turn_id=opening_turn_id,
                attempt_id=attempt.id,
                reason="proactive_greeting_audio_ready_to_drain",
            )
            await self.send({"type": "tts_end"})
            return True
        except Exception as exc:
            self.trace.record(
                "opening_greeting_error",
                turn_id=opening_turn_id,
                attempt_id=attempt.id,
                reason=type(exc).__name__,
                error=str(exc),
            )
            await self.send({"type": "stop_tts"})
            return False
        finally:
            # LiveKit normally closes this synthetic opening lifecycle when its
            # audio queue drains. Keep non-playback/error paths usable too.
            if self.turn_id == opening_turn_id:
                self.closed_turns.add(opening_turn_id)
                self.last_closed_turn_id = opening_turn_id
                self.trace.record(
                    "turn_close",
                    turn_id=opening_turn_id,
                    reason="opening_greeting_dispatch_complete",
                )
                self._transition("idle", "opening_greeting_completed")
                self.turn_id = None
                self.attempt = None

    def _provider_event(self, event: str, data: dict[str, Any]) -> None:
        details = dict(data)
        provider_reason = details.pop("reason", None)
        if provider_reason is not None and "provider_reason" not in details:
            details["provider_reason"] = provider_reason
        self.trace.record(
            event,
            turn_id=self.turn_id,
            reason="provider_protocol_event",
            **details,
        )

    def _transition(self, target: str, reason: str, **data: Any) -> None:
        previous = self.state
        self.state = target
        self.trace.record(
            "state_transition",
            turn_id=self.turn_id,
            attempt_id=self.attempt.id if self.attempt else None,
            state_from=previous,
            state_to=target,
            reason=reason,
            **data,
        )

    async def audio(
        self,
        pcm: bytes,
        *,
        client_sent_ms: float | None = None,
        client_network_ms: float | None = None,
    ) -> None:
        if self.state == "recovering_stt":
            return
        await self.stt.append(pcm)
        self.last_client_network_ms = client_network_ms
        duration_ms = len(pcm) / 2 / 16000 * 1000
        level = rms_pcm16(pcm)
        speaking = level >= self.config.speech_rms
        if speaking:
            if self.state in {"idle", "playing", "releasing"}:
                if not self.speech_candidate_active:
                    self.turn_pcm16.clear()
                    self.speech_candidate_active = True
                    self.candidate_turn_id = f"turn-{self.turn_number + 1:04d}"
                    self.candidate_client_sent_ms = client_sent_ms
                    self.candidate_client_network_ms = client_network_ms
                    self.speech_ms = 0.0
                    self.trace.record(
                        "user_speech_candidate_start",
                        turn_id=self.candidate_turn_id,
                        reason="first_voiced_frame_requires_minimum_duration_confirmation",
                        rms=round(level, 2),
                        client_sent_ms=client_sent_ms,
                        client_network_ms=client_network_ms,
                    )
                self.turn_pcm16.extend(pcm)
                self.speech_ms += duration_ms
                if self.speech_ms >= self.config.min_speech_ms:
                    await self._speech_started(level, client_sent_ms, client_network_ms)
                return
            self.turn_pcm16.extend(pcm)
            if self.silence_candidate:
                self.trace.record(
                    "speech_end_candidate_cancel",
                    turn_id=self.turn_id,
                    reason="voice_resumed_before_eou_silence_elapsed",
                    accumulated_silence_ms=round(self.silence_ms, 2),
                )
                self.silence_candidate = False
            self.speech_ms += duration_ms
            self.silence_ms = 0.0
            return
        if self.speech_candidate_active and self.state in {"idle", "playing", "releasing"}:
            self.turn_pcm16.extend(pcm)
            self.trace.record(
                "user_speech_candidate_cancel",
                turn_id=self.candidate_turn_id,
                reason="voiced_audio_ended_before_minimum_speech_duration",
                voiced_ms=round(self.speech_ms, 2),
                required_ms=self.config.min_speech_ms,
            )
            self.turn_pcm16.clear()
            self._clear_speech_candidate()
            return
        if self.state in {"speaking", "speculating"}:
            self.turn_pcm16.extend(pcm)
            if not self.silence_candidate:
                self.silence_candidate = True
                self.trace.record(
                    "user_speech_audio_end_candidate",
                    turn_id=self.turn_id,
                    reason="first_non_speech_frame_after_voice",
                    client_network_ms=client_network_ms,
                )
            self.silence_ms += duration_ms
            if self.silence_ms >= self.config.end_silence_ms:
                await self._speech_ended()
        elif self.state == "idle":
            self.speech_ms = 0.0

    async def _speech_started(
        self,
        level: float,
        client_sent_ms: float | None,
        client_network_ms: float | None,
    ) -> None:
        confirmed_turn_id = self.candidate_turn_id or f"turn-{self.turn_number + 1:04d}"
        first_client_sent_ms = self.candidate_client_sent_ms
        first_client_network_ms = self.candidate_client_network_ms
        buffered_partial = self.preconfirm_partial
        if self.state in {"playing", "releasing"}:
            interrupted_turn = self.turn_id
            await self._cancel_attempt("user_barge_in")
            if interrupted_turn:
                self.closed_turns.add(interrupted_turn)
                self.last_closed_turn_id = interrupted_turn
                self.trace.record(
                    "turn_close",
                    turn_id=interrupted_turn,
                    reason="user_speech_interrupted_pending_or_active_response",
                )
        self.turn_number += 1
        self.turn_id = confirmed_turn_id
        self.last_partial = ""
        self.final_text = ""
        self.silence_candidate = False
        self.closed_turns.discard(self.turn_id)
        self.trace.record(
            "user_speech_start",
            turn_id=self.turn_id,
            reason="server_energy_vad_detected_first_voiced_frame",
            rms=round(level, 2),
            client_sent_ms=first_client_sent_ms or client_sent_ms,
            client_network_ms=first_client_network_ms or client_network_ms,
            confirmation_ms=round(self.speech_ms, 2),
        )
        self._clear_speech_candidate()
        self._transition("speaking", "first_voiced_frame_detected")
        if buffered_partial:
            await self.on_partial(*buffered_partial)

    def _clear_speech_candidate(self) -> None:
        self.speech_candidate_active = False
        self.candidate_turn_id = None
        self.candidate_client_sent_ms = None
        self.candidate_client_network_ms = None
        self.preconfirm_partial = None
        self.speech_ms = 0.0

    async def _speech_ended(self) -> None:
        if self.state not in {"speaking", "speculating"}:
            return
        self.trace.record(
            "user_speech_end",
            turn_id=self.turn_id,
            reason="server_energy_vad_silence_window_elapsed",
            silence_ms=round(self.silence_ms, 2),
        )
        self._transition("awaiting_final", "application_turn_owner_requested_stt_final")
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.silence_candidate = False
        committed_audio = bytes(self.turn_pcm16)
        self.turn_pcm16.clear()
        self.trace.record(
            "stt_local_turn_audio_committed",
            turn_id=self.turn_id,
            reason="local_vad_owned_utterance_sent_to_batch_final",
            pcm_bytes=len(committed_audio),
            audio_ms=round(len(committed_audio) / 2 / 16000 * 1000, 1),
        )
        await self.stt.commit(committed_audio)
        if self.final_timeout_task:
            self.final_timeout_task.cancel()
        expected_turn = self.turn_id
        self.final_timeout_task = asyncio.create_task(self._final_timeout(expected_turn))

    async def _final_timeout(self, expected_turn: str | None) -> None:
        try:
            await asyncio.sleep(self.config.stt_batch_timeout_seconds + 1.0)
            if self.turn_id != expected_turn or self.state != "awaiting_final":
                return
            self.trace.record(
                "stt_final_timeout",
                turn_id=self.turn_id,
                reason="no_authoritative_batch_transcript_before_deadline",
            )
            await self._cancel_attempt("stt_final_timeout")
            self._transition("recovering_stt", "stt_final_timeout_restarting_provider")
            timed_out_turn = self.turn_id
            recovered = False
            try:
                await self.stt.recover("final_timeout")
            except Exception as exc:
                self.trace.record(
                    "stt_recovery_unavailable",
                    turn_id=timed_out_turn,
                    reason="provider_reconnect_failed_after_final_timeout",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            else:
                recovered = True
            self._transition(
                "idle",
                "stt_recovery_completed"
                if recovered
                else "stt_recovery_failed_retry_on_next_audio",
            )
            if timed_out_turn:
                self.closed_turns.add(timed_out_turn)
                self.last_closed_turn_id = timed_out_turn
                self.trace.record("turn_close", turn_id=timed_out_turn, reason="stt_final_timeout")
            self.turn_id = None
            self.final_timeout_task = None
        except asyncio.CancelledError:
            return

    async def on_partial(self, text: str, raw: str) -> None:
        if not self.turn_id:
            if self.speech_candidate_active:
                self.preconfirm_partial = (text, raw)
                self.trace.record(
                    "stt_partial_before_speech_confirmation",
                    turn_id=self.candidate_turn_id,
                    reason="buffered_until_minimum_speech_duration",
                    text=text,
                    raw=raw[:240],
                )
                return
            if self.last_closed_turn_id:
                self.trace.record(
                    "invariant_violation",
                    turn_id=self.last_closed_turn_id,
                    reason="stt_partial_received_after_turn_close",
                    text=text,
                )
            return
        if self.state == "awaiting_final":
            self.trace.record(
                "stt_post_commit_partial_ignored",
                turn_id=self.turn_id,
                attempt_id=self.attempt.id if self.attempt else None,
                reason="decoder_replay_cannot_revise_turn_after_final_requested",
                text=text,
                raw=raw[:240],
            )
            return
        if self.state not in {"speaking", "speculating"}:
            return
        if self.turn_id in self.closed_turns:
            self.trace.record(
                "invariant_violation",
                turn_id=self.turn_id,
                reason="partial_received_after_turn_close",
                text=text,
            )
            return
        changed = text != self.last_partial
        self.last_partial = text
        self.trace.record(
            "stt_partial",
            turn_id=self.turn_id,
            reason="rstt_transcription_delta",
            text=text,
            raw=raw[:240],
            changed=changed,
        )
        await self.send({"type": "partial_user_request", "content": text})
        if not changed or len(normalize(text)) < self.config.stable_min_chars:
            return
        if self.attempt and normalize(self.attempt.hypothesis) != normalize(text):
            similarity = SequenceMatcher(None, normalize(self.attempt.hypothesis), normalize(text)).ratio()
            await self._cancel_attempt("stt_partial_revision", similarity=round(similarity, 3))
            self.trace.record(
                "speculation_restart_scheduled",
                turn_id=self.turn_id,
                reason="partial_changed_after_speculation_started",
                similarity=round(similarity, 3),
            )
        if self.stability_task:
            self.stability_task.cancel()
        self.stability_task = asyncio.create_task(self._stable_after_delay(text))

    async def _stable_after_delay(self, text: str) -> None:
        try:
            await asyncio.sleep(self.config.stable_partial_ms / 1000)
            if text != self.last_partial or self.state not in {"speaking", "speculating"}:
                return
            self.trace.record(
                "stt_stable_partial",
                turn_id=self.turn_id,
                reason="partial_unchanged_for_stability_window",
                text=text,
                stable_ms=self.config.stable_partial_ms,
            )
            await self._start_attempt(text, speculative=True, authorized=False)
            self._transition("speculating", "stable_partial_started_prepare_early")
        except asyncio.CancelledError:
            return

    async def on_final(self, text: str) -> None:
        if not self.turn_id:
            if self.last_closed_turn_id:
                self.trace.record(
                    "invariant_violation",
                    turn_id=self.last_closed_turn_id,
                    reason="stt_final_received_after_turn_close",
                    text=text,
                )
            return
        if self.state != "awaiting_final":
            self.trace.record(
                "stt_provider_final_ignored",
                turn_id=self.turn_id,
                reason="application_turn_owner_had_not_requested_final",
                text=text,
                state=self.state,
            )
            return
        if self.final_text:
            self.trace.record(
                "invariant_violation",
                turn_id=self.turn_id,
                reason="second_final_for_same_turn",
                first=self.final_text,
                second=text,
            )
            return
        if self.final_timeout_task:
            self.final_timeout_task.cancel()
            self.final_timeout_task = None
        self.final_text = text
        self.trace.record(
            "stt_final",
            turn_id=self.turn_id,
            reason="qwen_batch_transcript_authoritative",
            text=text,
        )
        await self.send({"type": "final_user_request", "content": text})
        if self.stability_task:
            self.stability_task.cancel()
            self.stability_task = None
        if self.attempt:
            similarity = SequenceMatcher(None, normalize(self.attempt.hypothesis), normalize(text)).ratio()
            valid = normalize(self.attempt.hypothesis) == normalize(text) or similarity >= 0.92
            self.trace.record(
                "speculation_validation",
                turn_id=self.turn_id,
                attempt_id=self.attempt.id,
                reason="final_transcript_compared_with_speculative_input",
                valid=valid,
                similarity=round(similarity, 3),
                hypothesis=self.attempt.hypothesis,
                final=text,
            )
            if valid:
                self.attempt.authorized = True
                self.attempt.authorization_event.set()
                self._transition("releasing", "final_validated_prepared_response")
                await self._release_held(self.attempt)
                if self.attempt.audio_complete:
                    await self._commit_attempt_text(self.attempt)
                return
            await self._cancel_attempt("final_transcript_changed", similarity=round(similarity, 3))
        await self._start_attempt(text, speculative=False, authorized=True)
        self._transition("releasing", "final_required_fresh_generation")

    async def _start_attempt(self, text: str, *, speculative: bool, authorized: bool) -> None:
        if self.attempt:
            await self._cancel_attempt("new_attempt_replaced_existing")
        attempt = Attempt(
            id=f"attempt-{uuid.uuid4().hex[:8]}",
            hypothesis=text,
            speculative=speculative,
            authorized=authorized,
            spoken_knowledge_acknowledgement=spoken_knowledge_acknowledgement(
                self.turn_number
            ),
        )
        if authorized:
            attempt.authorization_event.set()
        attempt.knowledge_query = text
        attempt.knowledge_gate_reason = "awaiting_explicit_llm_knowledge_request"
        self.attempt = attempt
        self.trace.record(
            "speculation_start" if speculative else "generation_start",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason="stable_partial" if speculative else "committed_final",
            text=text,
            authorized=authorized,
        )
        if self.pending_action:
            self.trace.record(
                "tool_confirmation_evidence_start",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason="pending_action_requires_committed_confirmation_turn",
                tool_name=self.pending_action.call.name,
                speculative=speculative,
            )
            attempt.task = asyncio.create_task(
                self._handle_pending_action(attempt), name=attempt.id
            )
            return
        self.trace.record(
            "knowledge_retrieval_deferred",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason="llm_must_explicitly_request_lookup_after_prompt_first_answer",
            query=text,
            speculative=speculative,
            knowledge_base_count=len(self.agent_context.knowledge_base_ids),
        )
        attempt.task = asyncio.create_task(self._generate(attempt), name=attempt.id)

    async def _generate(self, attempt: Attempt) -> None:
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self.history[-8:],
            {"role": "user", "content": attempt.hypothesis},
        ]
        try:
            attempt.tool_calls = await self._stream_llm_and_tts(
                attempt,
                messages,
                phase="prompt_first",
                tools=self.action_tools,
            )
            attempt.initial_answer = attempt.answer.strip()
            if attempt.tool_calls:
                call = attempt.tool_calls[0]
                self.trace.record(
                    "tool_call_prepared",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason="llm_selected_configured_action_tool",
                    tool_name=call.name,
                    argument_keys=sorted(call.arguments),
                    speculative=attempt.speculative,
                    extra_tool_call_count=max(0, len(attempt.tool_calls) - 1),
                )
                if not attempt.authorized:
                    self.trace.record(
                        "tool_call_waiting_for_final",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason=(
                            "end_call_requires_committed_closing_turn"
                            if call.name == "end_call"
                            else "no_action_confirmation_before_committed_turn_authority"
                        ),
                        tool_name=call.name,
                    )
                    await attempt.authorization_event.wait()
                if attempt.cancelled:
                    return
                if call.name == "end_call":
                    committed_text = self.final_text if attempt.authorized else ""
                    closing_intent = explicit_end_call_intent(committed_text)
                    self.trace.record(
                        "end_call_semantic_validation",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason=(
                            "explicit_committed_closing_intent"
                            if closing_intent
                            else "committed_transcript_has_no_explicit_closing_intent"
                        ),
                        valid=closing_intent,
                    )
                    if not closing_intent:
                        self.trace.record(
                            "end_call_rejected",
                            turn_id=self.turn_id,
                            attempt_id=attempt.id,
                            reason="deterministic_closing_intent_gate_vetoed_llm_tool_call",
                        )
                        attempt.tool_calls = []
                        attempt.held_audio.clear()
                        attempt.answer = ""
                        retry_messages = [
                            {
                                "role": "system",
                                "content": (
                                    f"{self.system_prompt}\n\n"
                                    "The caller's latest committed message does not explicitly end "
                                    "the conversation. Answer that message normally and continue the "
                                    "call. Do not end or offer to end the call in this response."
                                ),
                            },
                            *self.history[-8:],
                            {"role": "user", "content": committed_text},
                        ]
                        # Recovery is deliberately response-only. A mistaken
                        # hang-up must not cascade into a different action tool.
                        await self._stream_llm_and_tts(
                            attempt,
                            retry_messages,
                            phase="end_call_recovery",
                            tools=None,
                        )
                        attempt.initial_answer = attempt.answer.strip()
                    else:
                        attempt.held_audio.clear()
                        attempt.answer = ""
                        attempt.end_call_requested = True
                        self.trace.record(
                            "end_call_authorized",
                            turn_id=self.turn_id,
                            attempt_id=attempt.id,
                            reason="committed_caller_closing_turn_authorized_hangup",
                        )
                        await self._speak_text(
                            attempt,
                            "Thank you for calling. Goodbye.",
                            phase="end_call_goodbye",
                        )
                else:
                    prompt = confirmation_prompt(call)
                    self.pending_action = PendingAction(
                        call=call, confirmation_prompt=prompt
                    )
                    attempt.held_audio.clear()
                    attempt.answer = ""
                    self.trace.record(
                        "tool_confirmation_requested",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="separate_confirmation_required_before_side_effect",
                        tool_name=call.name,
                    )
                    await self._speak_text(
                        attempt, prompt, phase="tool_confirmation"
                    )
            elif requests_knowledge_lookup(attempt.initial_answer):
                await self.send(
                    {
                        "type": "partial_assistant_answer",
                        "content": attempt.spoken_knowledge_acknowledgement,
                    }
                )
                available, availability_reason = self.knowledge.availability()
                self.trace.record(
                    "knowledge_lookup_requested",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason="llm_explicitly_requested_business_knowledge",
                    query=attempt.hypothesis,
                    speculative=attempt.speculative,
                    available=available,
                    availability_reason=availability_reason,
                    spoken_acknowledgement=attempt.spoken_knowledge_acknowledgement,
                )
                if not attempt.authorized:
                    self.trace.record(
                        "knowledge_lookup_waiting_for_final",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="no_retrieval_before_committed_turn_authority",
                        query=attempt.hypothesis,
                    )
                    await attempt.authorization_event.wait()
                if attempt.cancelled:
                    return
                if available:
                    attempt.knowledge_query = self.final_text or attempt.hypothesis
                    self.trace.record(
                        "knowledge_retrieval_start",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="authorized_llm_requested_lookup",
                        query=attempt.knowledge_query,
                        speculative=False,
                        knowledge_base_count=len(self.agent_context.knowledge_base_ids),
                    )
                    attempt.knowledge_task = asyncio.create_task(
                        self.knowledge.search(attempt.knowledge_query),
                        name=f"{attempt.id}-knowledge",
                    )
                    attempt.knowledge_result = await attempt.knowledge_task
                else:
                    attempt.knowledge_result = KnowledgeResult(
                        query=self.final_text or attempt.hypothesis,
                        status="failed",
                        error=availability_reason,
                    )
                result = attempt.knowledge_result
                self.trace.record(
                    "knowledge_retrieval_complete",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason=(
                        "scoped_knowledge_matches_ready"
                        if result.status == "success" and result.matches
                        else "scoped_knowledge_empty"
                        if result.status == "success"
                        else "scoped_knowledge_unavailable"
                    ),
                    query=result.query,
                    status=result.status,
                    elapsed_ms=result.elapsed_ms,
                    cache_hit=result.cache_hit,
                    match_count=len(result.matches),
                    sources=[match.source_name for match in result.matches[:3]],
                    scores=[round(match.score, 4) for match in result.matches[:3]],
                    error=result.error or None,
                )
                knowledge_prompt = self.agent_context.knowledge_followup_prompt(
                    self.config.system_prompt,
                    result.prompt_context(),
                )
                followup_messages = [
                    {"role": "system", "content": knowledge_prompt},
                    *self.history[-8:],
                    {"role": "user", "content": self.final_text or attempt.hypothesis},
                ]
                attempt.answer = ""
                await self._stream_llm_and_tts(
                    attempt, followup_messages, phase="knowledge_followup"
                )
            attempt.audio_complete = True
            if attempt.cancelled:
                return
            self.trace.record(
                "generation_complete",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason="llm_and_all_phrase_tts_completed",
                answer=attempt.answer,
                initial_answer=attempt.initial_answer or None,
                used_knowledge=attempt.knowledge_result is not None,
                prepared_tool=(attempt.tool_calls[0].name if attempt.tool_calls else None),
                awaiting_tool_confirmation=bool(
                    attempt.tool_calls
                    and attempt.tool_calls[0].name != "end_call"
                ),
                end_call_requested=attempt.end_call_requested,
            )
            if attempt.authorized:
                await self.send({"type": "tts_end"})
                await self._commit_attempt_text(attempt)
                if attempt.end_call_requested:
                    self.trace.record(
                        "end_call_playout_complete",
                        turn_id=self.last_closed_turn_id or self.turn_id,
                        attempt_id=attempt.id,
                        reason="final_goodbye_drained_before_room_delete",
                    )
                    await self.send(
                        {
                            "type": "end_call",
                            "reason": "configured_end_call_after_goodbye",
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.trace.record(
                "generation_error",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason=type(exc).__name__,
                error=str(exc),
            )
            llm_temporarily_unavailable = (
                isinstance(exc, aiohttp.ClientResponseError)
                and exc.status in {502, 503, 504}
            ) or isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError))
            if attempt.authorized and llm_temporarily_unavailable and not attempt.cancelled:
                try:
                    attempt.held_audio.clear()
                    attempt.answer = ""
                    self.trace.record(
                        "generation_degraded_response_start",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="llm_unavailable_after_bounded_retries",
                    )
                    await self._speak_text(
                        attempt,
                        "I'm sorry, my response service is temporarily unavailable. "
                        "Please try that again in a moment.",
                        phase="llm_unavailable_fallback",
                    )
                    attempt.audio_complete = True
                    self.trace.record(
                        "generation_complete",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="spoken_llm_unavailable_fallback_completed",
                        answer=attempt.answer,
                        initial_answer=None,
                        used_knowledge=False,
                        prepared_tool=None,
                        awaiting_tool_confirmation=False,
                        end_call_requested=False,
                    )
                    await self.send({"type": "tts_end"})
                    await self._commit_attempt_text(attempt)
                    return
                except Exception as fallback_exc:
                    self.trace.record(
                        "generation_degraded_response_error",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason=type(fallback_exc).__name__,
                        error=str(fallback_exc),
                    )
            await self.send({"type": "lab_status", "content": f"Pipeline error: {exc}"})

    async def _handle_pending_action(self, attempt: Attempt) -> None:
        pending = self.pending_action
        if not pending:
            return
        try:
            if not attempt.authorized:
                await attempt.authorization_event.wait()
            if attempt.cancelled:
                return
            evidence = self.final_text or attempt.hypothesis
            decision = confirmation_decision(evidence)
            self.trace.record(
                "tool_confirmation_decision",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason="committed_caller_confirmation_classified",
                tool_name=pending.call.name,
                decision=decision,
            )
            attempt.answer = ""
            if decision == "rejected":
                self.pending_action = None
                await self._speak_text(
                    attempt,
                    "No problem. I won't carry out that action.",
                    phase="tool_confirmation_rejected",
                )
            elif decision == "unclear":
                await self._speak_text(
                    attempt,
                    pending.confirmation_prompt,
                    phase="tool_confirmation_repeated",
                )
            else:
                self.pending_action = None
                acknowledgement = (
                    "Okay, one moment while I create that ticket."
                    if pending.call.name == "create_ticket"
                    else "Okay, one moment while I send that email."
                )
                await self._speak_text(
                    attempt, acknowledgement, phase="tool_execution_acknowledgement"
                )
                self.trace.record(
                    "tool_execution_start",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason="committed_explicit_confirmation_received",
                    tool_name=pending.call.name,
                )
                result = await self.action_executor.execute(pending.call)
                self.trace.record(
                    "tool_execution_complete",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason=(
                        "configured_action_api_succeeded"
                        if result.status == "success"
                        else "configured_action_api_failed"
                    ),
                    tool_name=pending.call.name,
                    status=result.status,
                    elapsed_ms=round(result.elapsed_ms, 3),
                    result_keys=sorted(result.data),
                )
                await self._speak_text(
                    attempt, result.message, phase="tool_execution_feedback"
                )
            attempt.audio_complete = True
            if attempt.cancelled:
                return
            self.trace.record(
                "generation_complete",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason="tool_confirmation_or_execution_feedback_completed",
                answer=attempt.answer,
                used_knowledge=False,
                tool_name=pending.call.name,
                confirmation_decision=decision,
            )
            if attempt.authorized:
                await self.send({"type": "tts_end"})
                await self._commit_attempt_text(attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.trace.record(
                "generation_error",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason=type(exc).__name__,
                error=str(exc),
                phase="tool_confirmation_or_execution",
            )
            await self.send({"type": "lab_status", "content": f"Pipeline error: {exc}"})

    async def _stream_llm_and_tts(
        self,
        attempt: Attempt,
        messages: list[dict[str, str]],
        *,
        phase: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[ActionToolCall]:
        tokenizer = PhraseTokenizer()
        tool_calls: list[ActionToolCall] = []
        phrase_queue: asyncio.Queue[str | None] = asyncio.Queue()
        tts_task = asyncio.create_task(
            self._synthesize_phrases(attempt, phrase_queue, phase=phase)
        )
        first_token = True
        try:
            async for event in self.llm.stream(messages, tools=tools):
                if attempt.cancelled:
                    raise asyncio.CancelledError
                if isinstance(event, ActionToolCall):
                    tool_calls.append(event)
                    continue
                token = event
                if first_token:
                    first_token = False
                    self.trace.record(
                        "llm_first_token",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="first_stream_delta",
                        phase=phase,
                    )
                attempt.answer += token
                await self.send({"type": "partial_assistant_answer", "content": attempt.answer})
                for phrase in tokenizer.push(token):
                    self.trace.record(
                        "tokenizer_phrase_ready",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="punctuation_or_max_clause_boundary",
                        text=phrase,
                        phase=phase,
                    )
                    await phrase_queue.put(phrase)
            for phrase in tokenizer.finish():
                self.trace.record(
                    "tokenizer_phrase_ready",
                    turn_id=self.turn_id,
                    attempt_id=attempt.id,
                    reason="llm_stream_finished",
                    text=phrase,
                    phase=phase,
                )
                await phrase_queue.put(phrase)
            await phrase_queue.put(None)
            await tts_task
            return tool_calls
        except BaseException:
            tts_task.cancel()
            await asyncio.gather(tts_task, return_exceptions=True)
            raise

    async def _speak_text(self, attempt: Attempt, text: str, *, phase: str) -> None:
        clean = str(text or "").strip()
        if not clean or attempt.cancelled:
            return
        attempt.answer = f"{attempt.answer.strip()} {clean}".strip()
        await self.send(
            {"type": "partial_assistant_answer", "content": attempt.answer}
        )
        self.trace.record(
            "tokenizer_phrase_ready",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason="application_generated_action_phrase",
            text=clean,
            phase=phase,
        )
        phrase_queue: asyncio.Queue[str | None] = asyncio.Queue()
        await phrase_queue.put(clean)
        await phrase_queue.put(None)
        await self._synthesize_phrases(attempt, phrase_queue, phase=phase)

    async def _synthesize_phrases(
        self,
        attempt: Attempt,
        queue: asyncio.Queue[str | None],
        *,
        phase: str,
    ) -> None:
        phrase_index = 0
        while True:
            phrase = await queue.get()
            if phrase is None or attempt.cancelled:
                return
            model_phrase = phrase
            if phase == "prompt_first" and requests_knowledge_lookup(phrase):
                phrase = attempt.spoken_knowledge_acknowledgement
            phrase_index += 1
            self.trace.record(
                "tts_request_start",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason=(
                    "dynamic_knowledge_acknowledgement_dispatched"
                    if phrase != model_phrase
                    else "stable_phrase_dispatched_immediately"
                ),
                phrase_index=phrase_index,
                text=phrase,
                model_text=model_phrase if phrase != model_phrase else None,
                held=not attempt.authorized,
                initial_codec_chunk_frames=self.config.tts_initial_codec_chunk_frames,
                tts_voice=self.config.tts_voice or None,
                phase=phase,
            )
            sample_rate, chunks = await self.tts.stream(phrase)
            first = True
            async for chunk in chunks:
                if attempt.cancelled:
                    return
                if first:
                    first = False
                    self.trace.record(
                        "tts_first_audio_chunk",
                        turn_id=self.turn_id,
                        attempt_id=attempt.id,
                        reason="first_nonempty_provider_chunk",
                        phrase_index=phrase_index,
                        sample_rate=sample_rate,
                        chunk_bytes=len(chunk),
                        chunk_audio_ms=round(len(chunk) / (sample_rate * 2) * 1000, 3),
                        held=not attempt.authorized,
                        phase=phase,
                    )
                if attempt.authorized:
                    await self._send_audio(sample_rate, chunk, attempt)
                else:
                    attempt.held_audio.append((sample_rate, chunk))

    async def _release_held(self, attempt: Attempt) -> None:
        self.trace.record(
            "playback_release_authorized",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason="application_turn_final_and_speculation_valid",
            held_chunks=len(attempt.held_audio),
            held_bytes=sum(len(chunk) for _, chunk in attempt.held_audio),
        )
        for sample_rate, chunk in attempt.held_audio:
            await self._send_audio(sample_rate, chunk, attempt)
        attempt.held_audio.clear()
        if attempt.audio_complete:
            await self.send({"type": "tts_end"})

    async def _commit_attempt_text(self, attempt: Attempt) -> None:
        if attempt.response_committed:
            return
        attempt.response_committed = True
        await self.send({"type": "final_assistant_answer", "content": attempt.answer.strip()})
        self.history.extend(
            [
                {"role": "user", "content": self.final_text or attempt.hypothesis},
                {"role": "assistant", "content": attempt.answer.strip()},
            ]
        )

    async def _send_audio(self, sample_rate: int, chunk: bytes, attempt: Attempt) -> None:
        await self.send(
            {
                "type": "tts_chunk",
                "content": base64.b64encode(chunk).decode("ascii"),
                "sample_rate": sample_rate,
            }
        )
        self.trace.record(
            "audio_chunk_sent",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason=f"authorized_audio_forwarded_to_{self.transport}",
            bytes=len(chunk),
            sample_rate=sample_rate,
        )

    async def _cancel_attempt(self, reason: str, **data: Any) -> None:
        attempt = self.attempt
        if not attempt:
            return
        attempt.cancelled = True
        released = attempt.authorized
        knowledge_was_pending = bool(
            attempt.knowledge_task and not attempt.knowledge_task.done()
        )
        if attempt.task and attempt.task is not asyncio.current_task():
            attempt.task.cancel()
            await asyncio.gather(attempt.task, return_exceptions=True)
        if attempt.knowledge_task and not attempt.knowledge_task.done():
            attempt.knowledge_task.cancel()
            await asyncio.gather(attempt.knowledge_task, return_exceptions=True)
        if knowledge_was_pending:
            self.trace.record(
                "knowledge_retrieval_cancel",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason=reason,
                query=attempt.knowledge_query,
                gate_reason=attempt.knowledge_gate_reason,
            )
        elif attempt.knowledge_result is not None:
            self.trace.record(
                "knowledge_retrieval_discard",
                turn_id=self.turn_id,
                attempt_id=attempt.id,
                reason=reason,
                query=attempt.knowledge_query,
                status=attempt.knowledge_result.status,
                match_count=len(attempt.knowledge_result.matches),
            )
        self.trace.record(
            "speculation_cancel" if attempt.speculative else "generation_cancel",
            turn_id=self.turn_id,
            attempt_id=attempt.id,
            reason=reason,
            discarded_chunks=len(attempt.held_audio),
            discarded_bytes=sum(len(chunk) for _, chunk in attempt.held_audio),
            **data,
        )
        if released:
            await self.send({"type": "stop_tts"})
        self.attempt = None

    async def client_event(self, kind: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        if kind == "client_ready":
            self.trace.record(
                f"{self.transport}_audio_ready",
                reason=(
                    "audio_contexts_initialized"
                    if self.transport == "browser"
                    else "livekit_input_and_output_tracks_ready"
                ),
                **data,
            )
            return
        if kind == "tts_start":
            self.trace.record(
                f"{self.transport}_playback_start",
                turn_id=self.turn_id,
                attempt_id=self.attempt.id if self.attempt else None,
                reason=(
                    "audio_worklet_rendered_first_sample"
                    if self.transport == "browser"
                    else "first_audio_frame_enqueued_to_livekit_source"
                ),
            )
            self._transition("playing", f"{self.transport}_confirmed_playback")
        elif kind == "tts_stop":
            self.trace.record(
                f"{self.transport}_playback_stop",
                turn_id=self.turn_id,
                attempt_id=self.attempt.id if self.attempt else None,
                reason=(
                    "audio_worklet_buffer_drained"
                    if self.transport == "browser"
                    else "livekit_audio_source_queue_drained"
                ),
            )
            if self.turn_id:
                self.closed_turns.add(self.turn_id)
                self.last_closed_turn_id = self.turn_id
                self.trace.record(
                    "turn_close",
                    turn_id=self.turn_id,
                    reason=f"{self.transport}_playback_completed",
                )
            self._transition("idle", "turn_completed")
            self.turn_id = None
            self.attempt = None
        elif kind == "clear_history":
            self.history.clear()
            self.trace.record("history_cleared", reason="browser_request")

    async def close(self) -> None:
        if self.stability_task:
            self.stability_task.cancel()
        if self.final_timeout_task:
            self.final_timeout_task.cancel()
        if self.attempt:
            # The browser socket is already gone; cleanup must not try to send a stop message.
            self.attempt.authorized = False
        await self._cancel_attempt("session_disconnect")
        await self.stt.close()
        await self.http.close()
        self.trace.record("session_close", reason=f"{self.transport}_transport_disconnected")
