from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .config import LabConfig
from .trace import TraceRecorder


KNOWLEDGE_ACKNOWLEDGEMENT = "Let me check that for you."
SPOKEN_KNOWLEDGE_ACKNOWLEDGEMENTS = (
    "One moment, please, while I confirm that.",
    "Okay, give me a second to check that.",
    "Let me confirm that for you.",
    "Sure, one moment while I look that up.",
)


_KNOWLEDGE_LOOKUP_VERB = (
    r"(?:check|confirm|verify|look\s+(?:that|this|it)\s+up|look\s+up|"
    r"search|consult|find\s+out)"
)
_SELF_DIRECTED_KNOWLEDGE_LOOKUP_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\b(?:let\s+me|allow\s+me\s+to)\s+(?:just\s+|quickly\s+)?{_KNOWLEDGE_LOOKUP_VERB}\b",
        rf"\bi\s+(?:(?:will|ll|need\s+to|must|am\s+going\s+to|want\s+to|"
        rf"would\s+like\s+to)(?:\s+just|\s+quickly)?\s+){_KNOWLEDGE_LOOKUP_VERB}\b",
        rf"\bi\s+(?:would|d)\s+be\s+(?:happy|glad)\s+to\s+{_KNOWLEDGE_LOOKUP_VERB}\b",
        rf"\bwhile\s+i\s+(?:just\s+|quickly\s+)?{_KNOWLEDGE_LOOKUP_VERB}\b",
        rf"\b(?:may|can|could)\s+i\s+(?:just\s+|quickly\s+)?{_KNOWLEDGE_LOOKUP_VERB}\b",
        rf"\b(?:may|can|could)\s+i\b.{{0,40}}\b(?:moment|second|minute)\b"
        rf".{{0,50}}\b{_KNOWLEDGE_LOOKUP_VERB}\b",
    )
)


def requests_knowledge_lookup(answer: str) -> bool:
    normalized = str(answer or "").lower().replace("’", "'")
    normalized = re.sub(r"\bi'll\b", "i will", normalized)
    normalized = re.sub(r"\bi'd\b", "i would", normalized)
    normalized = re.sub(r"\bi'm\b", "i am", normalized)
    normalized = re.sub(r"[^a-z0-9' ]+", " ", normalized)
    normalized = normalized.replace("'", "")
    normalized = " ".join(normalized.split())
    if normalized == "let me check that for you":
        return True
    return any(
        pattern.search(normalized)
        for pattern in _SELF_DIRECTED_KNOWLEDGE_LOOKUP_PATTERNS
    )


def spoken_knowledge_acknowledgement(turn_number: int) -> str:
    index = max(0, int(turn_number) - 1) % len(SPOKEN_KNOWLEDGE_ACKNOWLEDGEMENTS)
    return SPOKEN_KNOWLEDGE_ACKNOWLEDGEMENTS[index]


def _decode_room_token(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw.startswith("h") and re.fullmatch(r"h[0-9a-fA-F]+", raw):
        try:
            return bytes.fromhex(raw[1:]).decode("utf-8").strip()
        except (UnicodeDecodeError, ValueError):
            return ""
    try:
        padding = "=" * ((4 - len(raw) % 4) % 4)
        return base64.urlsafe_b64decode(raw + padding).decode("utf-8").strip()
    except (UnicodeDecodeError, ValueError):
        return ""


@dataclass(frozen=True)
class RoomAgentContext:
    business_id: str = ""
    agent_id: str = ""
    configured_name: str = ""
    end_user_id: str = ""


def session_routing_context(data: dict[str, Any] | None) -> RoomAgentContext:
    payload = data if isinstance(data, dict) else {}
    return RoomAgentContext(
        business_id=str(payload.get("business_id") or "").strip(),
        agent_id=str(
            payload.get("config_agent_id") or payload.get("agent_id") or ""
        ).strip(),
        configured_name=str(
            payload.get("agent_name") or payload.get("configured_agent_name") or ""
        ).strip(),
        end_user_id=str(
            payload.get("end_user_email") or payload.get("end_user_id") or ""
        ).strip(),
    )


def room_agent_context(room_name: str) -> RoomAgentContext:
    prefix = "voice_assistant_room_"
    raw = str(room_name or "").strip()
    if not raw.startswith(prefix):
        return RoomAgentContext()
    match = re.match(
        r"^eid(?P<eid>.+?)(?:_bid(?P<bid>.+?))?"
        r"(?:_aid(?P<aid>.+?))?(?:_nid(?P<nid>.+?))?"
        r"(?:_uid(?P<uid>.+?))?_(?P<rand>\d+)$",
        raw[len(prefix) :],
    )
    if not match:
        return RoomAgentContext()
    return RoomAgentContext(
        business_id=_decode_room_token(str(match.group("bid") or "")),
        agent_id=_decode_room_token(str(match.group("aid") or "")),
        configured_name=_decode_room_token(str(match.group("nid") or "")),
        end_user_id=_decode_room_token(str(match.group("eid") or "")),
    )


@dataclass(frozen=True)
class AgentRuntimeContext:
    business_id: str = ""
    agent_id: str = ""
    name: str = ""
    end_user_id: str = ""
    instructions: str = ""
    knowledge_base_ids: tuple[str, ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    loaded: bool = False

    def _capability_rules(self) -> str:
        enabled = {
            str(tool.get("name") or "").strip()
            for tool in self.tools
            if isinstance(tool, dict)
        }
        lines = [
            "- Keep the spoken answer to one or two short sentences unless the caller asks for more detail."
        ]
        supported = sorted(enabled.intersection({"create_ticket", "send_email"}))
        if supported:
            lines.extend(
                [
                    f"- Configured action tools: {', '.join(supported)}.",
                    "- When the caller requests one of those actions, call the matching function with complete arguments. The application will ask for confirmation and execute only after a committed confirmation turn.",
                    "- Never claim an action succeeded before its function result confirms success.",
                ]
            )
        else:
            lines.append(
                "- No action tool is enabled. Do not claim to have sent, created, changed, transferred, or completed anything."
            )
        if "end_call" in enabled:
            lines.extend(
                [
                    "- end_call is enabled for this live call. Use it only after the caller explicitly says goodbye, asks to end or hang up, says there is nothing else, or otherwise clearly finishes the conversation.",
                    "- A simple thank-you is not enough by itself. Never end the call while the caller is speaking or while an answer or action is incomplete.",
                    "- Call end_call with no arguments. The application will wait for the committed closing turn, speak one short goodbye, drain playback, and then hang up.",
                ]
            )
        else:
            lines.append(
                "- end_call is not enabled. Do not claim that you can hang up the call."
            )
        lines.append("- Transfers and other unsupported actions remain unavailable.")
        return "\n".join(lines)

    def base_prompt(self, fallback: str) -> str:
        prompt = self.instructions.strip() or fallback
        if self.loaded:
            prompt += (
                "\n\nCurrent RVC capability boundary:\n"
                f"{self._capability_rules()}"
            )
        return prompt

    def system_prompt(self, fallback: str) -> str:
        prompt = self.instructions.strip() or fallback
        if self.loaded:
            prompt += (
                "\n\nCRITICAL RVC KNOWLEDGE ROUTING RULE — follow this before answering:\n"
                "1. Answer greetings, small talk, acknowledgements, whether you can hear the "
                "caller, and facts explicitly written in this prompt directly.\n"
            )
            if self.knowledge_base_ids:
                prompt += (
                    "2. For any bank-specific code, product, policy, fee, rate, eligibility rule, "
                    "requirement, contact detail, procedure, or service detail that is not explicitly "
                    "written in this prompt, do not answer from memory, inference, or general banking "
                    f"knowledge. Prefer this exact lookup acknowledgement: {KNOWLEDGE_ACKNOWLEDGEMENT}\n"
                    "3. A natural variation is allowed only if it is a brief first-person hold message "
                    "that explicitly says you will check, confirm, verify, or look up the missing fact. "
                    "Do not include an answer and never tell the caller to perform the check. Use a "
                    "lookup acknowledgement only for case 2. The application will perform the "
                    "read-only lookup after the caller's turn is final, then give you the retrieved "
                    "facts for a follow-up answer. For example, a greeting or 'Can you hear me?' must "
                    "be answered directly, while a question about an unstated USSD sub-code requires "
                    "the exact lookup sentence.\n"
                )
            else:
                prompt += (
                    "2. No business knowledge is attached. Never promise to check it; be transparent "
                    "when a business-specific fact cannot be verified.\n"
                )
            prompt += f"\n{self._capability_rules()}"
        return prompt

    def knowledge_followup_prompt(
        self,
        fallback: str,
        knowledge_context: str,
    ) -> str:
        return (
            f"{self.base_prompt(fallback)}\n\n"
            "KNOWLEDGE RETRIEVAL IS COMPLETE. The caller has already heard a short "
            "acknowledgement. Answer the original question now using the validated context below. "
            "Do not say that you will check, do not request another lookup, and do not repeat the "
            "acknowledgement. If the context does not contain the answer, say that the information "
            "was not found and offer the appropriate official support channel.\n\n"
            f"{knowledge_context}"
        )


async def load_agent_runtime_context(
    *,
    room_name: str,
    config: LabConfig,
    trace: TraceRecorder,
    routing_context: RoomAgentContext | None = None,
) -> AgentRuntimeContext:
    room_context = routing_context or room_agent_context(room_name)
    base = AgentRuntimeContext(
        business_id=room_context.business_id,
        agent_id=room_context.agent_id,
        name=room_context.configured_name,
        end_user_id=room_context.end_user_id,
    )
    if not room_context.business_id or not room_context.agent_id:
        trace.record(
            "agent_config_load_skipped",
            reason=(
                "session_missing_business_or_agent_scope"
                if not str(room_name or "").strip()
                else "room_missing_business_or_agent_scope"
            ),
            business_id=room_context.business_id,
            agent_id=room_context.agent_id,
        )
        return base
    if not config.agent_config_api_base_url or not config.agent_config_service_token:
        trace.record(
            "agent_config_load_skipped",
            reason="agent_config_service_not_configured",
            business_id=room_context.business_id,
            agent_id=room_context.agent_id,
        )
        return base

    trace.record(
        "agent_config_load_start",
        reason=(
            "voice_lab_session_scoped_runtime_config"
            if not str(room_name or "").strip()
            else "livekit_room_scoped_runtime_config"
        ),
        business_id=room_context.business_id,
        agent_id=room_context.agent_id,
    )
    url = (
        f"{config.agent_config_api_base_url}/v1/agents/internal/agents/"
        f"{room_context.agent_id}/runtime-config"
    )
    headers = {
        "X-Service-Token": config.agent_config_service_token,
        "X-Service-Name": config.agent_client_id,
        "X-Business-ID": room_context.business_id,
    }
    started_ns = time.perf_counter_ns()
    try:
        timeout = aiohttp.ClientTimeout(total=config.agent_config_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400 or not isinstance(payload, dict):
                    raise RuntimeError(f"agent config returned HTTP {response.status}")
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
        trace.record(
            "agent_config_load_failed",
            reason="runtime_config_request_failed",
            business_id=room_context.business_id,
            agent_id=room_context.agent_id,
            elapsed_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return base

    raw_knowledge_base_ids = payload.get("knowledge_base_ids")
    knowledge_base_ids = tuple(
        str(item).strip()
        for item in (
            raw_knowledge_base_ids if isinstance(raw_knowledge_base_ids, list) else []
        )
        if str(item).strip()
    )
    raw_tools = payload.get("tools")
    tools = tuple(
        item for item in (raw_tools if isinstance(raw_tools, list) else []) if isinstance(item, dict)
    )
    context = AgentRuntimeContext(
        business_id=room_context.business_id,
        agent_id=room_context.agent_id,
        name=str(payload.get("name") or room_context.configured_name).strip(),
        end_user_id=room_context.end_user_id,
        instructions=str(payload.get("instructions") or "").strip(),
        knowledge_base_ids=knowledge_base_ids,
        tools=tools,
        loaded=True,
    )
    trace.record(
        "agent_config_load_complete",
        reason="runtime_config_scoped_to_livekit_room",
        business_id=context.business_id,
        agent_id=context.agent_id,
        configured_name=context.name,
        end_user_id_present=bool(context.end_user_id),
        elapsed_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
        instructions_chars=len(context.instructions),
        knowledge_base_count=len(context.knowledge_base_ids),
        configured_tool_count=len(context.tools),
    )
    return context


@dataclass(frozen=True)
class KnowledgeMatch:
    source_name: str
    text: str
    score: float


@dataclass(frozen=True)
class KnowledgeResult:
    query: str
    status: str
    matches: tuple[KnowledgeMatch, ...] = ()
    elapsed_ms: float = 0.0
    cache_hit: bool = False
    error: str = ""

    def prompt_context(self) -> str:
        if self.status == "success" and self.matches:
            snippets = "\n".join(
                f"- {match.source_name}: {match.text[:900]}" for match in self.matches[:3]
            )
            return (
                "Validated business knowledge for the caller's current request:\n"
                "Use only relevant facts from these snippets. If they do not answer the "
                f"question, say that the information was not found.\n{snippets}"
            )
        if self.status == "success":
            return (
                "The scoped business knowledge search returned no matching information. "
                "Do not invent a business-specific answer."
            )
        return (
            "The scoped business knowledge search was unavailable. Be transparent that the "
            "information could not be verified and do not invent it."
        )


class KnowledgeClient:
    def __init__(
        self,
        config: LabConfig,
        session: aiohttp.ClientSession,
        context: AgentRuntimeContext,
    ) -> None:
        self.config = config
        self.session = session
        self.context = context
        self._cache: dict[str, KnowledgeResult] = {}

    def availability(self) -> tuple[bool, str]:
        if not self.context.knowledge_base_ids:
            return False, "agent_has_no_attached_knowledge_bases"
        if not self.config.knowledge_service_base_url or not self.config.knowledge_service_token:
            return False, "knowledge_service_not_configured"
        return True, "attached_knowledge_available_on_explicit_llm_request"

    async def search(self, query: str) -> KnowledgeResult:
        normalized = " ".join(str(query or "").lower().split()).strip()
        cached = self._cache.get(normalized)
        if cached is not None:
            return KnowledgeResult(
                query=query,
                status=cached.status,
                matches=cached.matches,
                elapsed_ms=0.0,
                cache_hit=True,
                error=cached.error,
            )

        started_ns = time.perf_counter_ns()
        url = f"{self.config.knowledge_service_base_url}/v1/knowledge/search"
        headers = {
            "Content-Type": "application/json",
            "X-Service-Token": self.config.knowledge_service_token,
            "X-Service-Name": self.config.agent_client_id,
            "X-Business-ID": self.context.business_id,
            "X-Agent-ID": self.context.agent_id,
        }
        body = {
            "query": str(query or "").strip(),
            "top_k": self.config.knowledge_top_k,
            "knowledge_base_ids": list(self.context.knowledge_base_ids),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.knowledge_timeout_seconds)
            async with self.session.post(url, json=body, headers=headers, timeout=timeout) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"knowledge service returned HTTP {response.status}")
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            return KnowledgeResult(
                query=query,
                status="failed",
                elapsed_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )

        raw_matches = payload.get("matches", []) if isinstance(payload, dict) else []
        if not isinstance(raw_matches, list):
            raw_matches = []
        matches: list[KnowledgeMatch] = []
        for item in raw_matches[: self.config.knowledge_top_k]:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split()).strip()
            if not text:
                continue
            matches.append(
                KnowledgeMatch(
                    source_name=str(item.get("source_name") or "Knowledge").strip(),
                    text=text,
                    score=float(item.get("score") or 0.0),
                )
            )
        result = KnowledgeResult(
            query=query,
            status="success",
            matches=tuple(matches),
            elapsed_ms=round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
        )
        self._cache[normalized] = result
        return result
