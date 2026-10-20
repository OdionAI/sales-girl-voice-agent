from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import aiohttp

from .agentic import AgentRuntimeContext
from .config import LabConfig
from .trace import TraceRecorder
from .banking_tools import bank_tool_definitions
from .provider_directory import definitions as directory_definitions
from .directory_email import definitions as directory_email_definitions


SUPPORTED_ACTION_TOOLS = {"create_ticket", "send_email", "end_call"}


def explicit_end_call_intent(text: str) -> bool:
    """Return true only for a caller utterance that clearly closes this call."""

    normalized = re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower())
    normalized = " ".join(normalized.split())
    if not normalized:
        return False

    # A negative instruction always wins, even if the sentence also contains
    # words such as "hang up" or "end the call".
    negative_patterns = (
        r"\b(?:do not|don't|dont|please don't|please dont|not yet)\s+(?:end|hang up|disconnect|terminate)\b",
        r"\b(?:do not|don't|dont)\s+say\s+goodbye\b",
        r"\bi\s+(?:do not|don't|dont)\s+want\s+to\s+(?:end|hang up|disconnect|terminate)\b",
    )
    if any(re.search(pattern, normalized) for pattern in negative_patterns):
        return False

    # Standalone farewells are explicit. Keep this bounded so explanatory
    # questions such as "what does goodbye mean" cannot terminate a call.
    farewell = re.fullmatch(
        r"(?:(?:ok(?:ay)?|alright|all right|well|thanks|thank you|cheers)[ ]+)*"
        r"(?:goodbye|good bye|bye|bye bye)"
        r"(?:[ ]+(?:thanks|thank you|for now|then|sarah|helen))?",
        normalized,
    )
    if farewell:
        return True

    combined_farewell_request = re.fullmatch(
        r"(?:goodbye|good bye|bye|bye bye)[ ]+(?:and[ ]+)?(?:please[ ]+)?"
        r"(?:end|terminate|disconnect)\s+(?:the|this|our)\s+call(?:[ ]+now)?",
        normalized,
    )
    if combined_farewell_request:
        return True

    direct_request_patterns = (
        r"^(?:(?:ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*"
        r"(?:please[ ]+)?(?:end|terminate|disconnect)\s+(?:the|this|our)\s+call(?:[ ]+now)?$",
        r"^(?:(?:ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*"
        r"(?:please[ ]+)?(?:hang up|disconnect)(?:[ ]+(?:the|this|our)\s+call)?(?:[ ]+now)?$",
        r"^(?:(?:ok(?:ay)?|alright|all right)[ ]+)*"
        r"(?:you|we)\s+can\s+(?:end|terminate|disconnect)\s+(?:the|this|our)\s+call(?:[ ]+now)?$",
        r"^(?:(?:ok(?:ay)?|alright|all right)[ ]+)*"
        r"you\s+can\s+hang up(?:[ ]+now)?$",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in direct_request_patterns):
        return True

    finished_patterns = (
        r"^(?:(?:no|ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*(?:that's|that is|thats)\s+all(?:[ ]+(?:thanks|thank you|for now))?$",
        r"^(?:(?:no|ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*nothing else(?:[ ]+(?:thanks|thank you))?$",
        r"^(?:(?:ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*(?:i am|i'm|im|we are|we're|were)\s+done$",
        r"^(?:(?:no|ok(?:ay)?|alright|all right)[ ]+)*i\s+(?:have|have got|haven't got|havent got)\s+no\s+more\s+questions(?:[ ]+(?:thanks|thank you))?$",
        r"^(?:(?:ok(?:ay)?|alright|all right|thanks|thank you)[ ]+)*(?:that|this)\s+will\s+be\s+all(?:[ ]+(?:thanks|thank you))?$",
    )
    return any(re.fullmatch(pattern, normalized) for pattern in finished_patterns)


@dataclass(frozen=True)
class ActionToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class PendingAction:
    call: ActionToolCall
    confirmation_prompt: str


@dataclass(frozen=True)
class ActionResult:
    status: str
    message: str
    data: dict[str, Any]
    elapsed_ms: float


def _tool_by_name(context: AgentRuntimeContext, name: str) -> dict[str, Any] | None:
    normalized = str(name or "").strip()
    for tool in context.tools:
        if str(tool.get("name") or "").strip() == normalized:
            return tool
    return None


def _is_enabled_internal_tool(tool: dict[str, Any] | None, name: str) -> bool:
    if not tool or name not in SUPPORTED_ACTION_TOOLS:
        return False
    url = str(tool.get("url") or "").strip().lower()
    expected = {
        "create_ticket": "internal://create-ticket",
        "send_email": "internal://send-email",
        "end_call": "builtin://end_call",
    }[name]
    return url == expected


def action_tool_definitions(context: AgentRuntimeContext) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    if _is_enabled_internal_tool(_tool_by_name(context, "create_ticket"), "create_ticket"):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "create_ticket",
                    "description": (
                        "Prepare a support ticket when the caller asks for human follow-up, "
                        "reports an unresolved issue, or explicitly asks for a ticket. The "
                        "application will request confirmation before execution."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short ticket title."},
                            "description": {
                                "type": "string",
                                "description": "Clear summary of the caller's issue and requested follow-up.",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "normal", "high", "urgent"],
                            },
                        },
                        "required": ["title", "description"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    if _is_enabled_internal_tool(_tool_by_name(context, "send_email"), "send_email"):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "send_email",
                    "description": (
                        "Prepare an email requested by the caller. Include the recipient, subject, "
                        "and complete message. The application will request confirmation before sending."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to_email": {"type": "string", "description": "Recipient email address."},
                            "subject": {"type": "string", "description": "Email subject."},
                            "body": {"type": "string", "description": "Complete plain-text email body."},
                        },
                        "required": ["to_email", "subject", "body"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    if _is_enabled_internal_tool(_tool_by_name(context, "end_call"), "end_call"):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "end_call",
                    "description": (
                        "End the live phone call only when the caller explicitly says goodbye, "
                        "asks to end or hang up, says there is nothing else, or confirms the "
                        "conversation is finished. The application speaks the final goodbye "
                        "before sending SIP BYE. Do not use this for a mere thank-you while the "
                        "conversation may continue."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }
        )
    return definitions + bank_tool_definitions(context) + directory_definitions(context) + directory_email_definitions(context)


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def uses_french(language_hint: str) -> bool:
    normalized = str(language_hint or "").strip().lower()
    return normalized.startswith(("fr", "french", "français", "francais"))


def localized_action_text(english: str, french: str, language_hint: str) -> str:
    return french if uses_french(language_hint) else english


def confirmation_prompt(call: ActionToolCall, *, language_hint: str = "") -> str:
    if uses_french(language_hint):
        if call.name == "create_ticket":
            title = str(call.arguments.get("title") or "ce problème").strip()
            return f"Pour confirmer, souhaitez-vous que je crée un ticket d’assistance au sujet de {title} ?"
        recipient = str(call.arguments.get("to_email") or "l’adresse e-mail indiquée").strip()
        subject = str(call.arguments.get("subject") or "les informations demandées").strip()
        return f"Pour confirmer, souhaitez-vous que j’envoie un e-mail à {recipient} avec pour objet : {subject} ?"
    if call.name == "create_ticket":
        title = str(call.arguments.get("title") or "this issue").strip()
        return f"Just to confirm, would you like me to create a support ticket about {title}?"
    recipient = str(call.arguments.get("to_email") or "the email address provided").strip()
    subject = str(call.arguments.get("subject") or "the requested information").strip()
    return f"Just to confirm, should I send an email to {recipient} with the subject {subject}?"


def confirmation_decision(text: str) -> str:
    raw = str(text or "").lower().replace("’", "'")
    normalized = unicodedata.normalize("NFKD", raw)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-z0-9' ]+", " ", normalized)
    normalized = " ".join(normalized.split())
    negative = (
        "no",
        "nope",
        "cancel",
        "don't",
        "do not",
        "never mind",
        "nevermind",
        "not anymore",
        "non",
        "non merci",
        "annule",
        "annulez",
        "pas maintenant",
        "je ne confirme pas",
        "je ne valide pas",
    )
    if any(normalized == item or normalized.startswith(f"{item} ") for item in negative):
        return "rejected"
    affirmative = (
        "yes",
        "yeah",
        "yep",
        "correct",
        "that's correct",
        "that is correct",
        "please do",
        "go ahead",
        "send it",
        "create it",
        "confirm",
        "okay create the ticket",
        "okay create ticket",
        "ok create the ticket",
        "ok create ticket",
        "create the ticket",
        "please create the ticket",
        "create a ticket",
        "oui",
        "d'accord",
        "allez y",
        "je confirme",
        "je valide",
        "fais le",
        "faites le",
        "creez le ticket",
        "cree le ticket",
        "oui creez le ticket",
        "oui allez y",
        "bien sur",
    )
    if any(normalized == item or normalized.startswith(f"{item} ") for item in affirmative):
        return "confirmed"
    return "unclear"


class ActionToolExecutor:
    def __init__(
        self,
        config: LabConfig,
        session: aiohttp.ClientSession,
        context: AgentRuntimeContext,
        trace: TraceRecorder,
    ) -> None:
        self.config = config
        self.session = session
        self.context = context
        self.trace = trace

    def available(self, name: str) -> bool:
        return bool(
            self.config.conversation_service_base_url
            and self.config.conversation_service_token
            and _is_enabled_internal_tool(_tool_by_name(self.context, name), name)
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Service-Token": self.config.conversation_service_token,
            "X-Service-Name": self.config.agent_client_id,
            "X-Business-ID": self.context.business_id,
        }

    async def execute(self, call: ActionToolCall) -> ActionResult:
        started_ns = time.perf_counter_ns()
        if not self.available(call.name):
            return ActionResult(
                status="failed",
                message=localized_action_text(
                    "That action is not available from this agent right now.",
                    "Cette action n’est pas disponible pour le moment.",
                    self.context.tts_language_hint,
                ),
                data={},
                elapsed_ms=0.0,
            )
        if call.name == "create_ticket":
            return await self._create_ticket(call, started_ns)
        if call.name == "send_email":
            return await self._send_email(call, started_ns)
        return ActionResult(
            "failed",
            localized_action_text(
                "That action is not supported.",
                "Cette action n’est pas prise en charge.",
                self.context.tts_language_hint,
            ),
            {},
            0.0,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=self.config.action_tool_timeout_seconds)
        async with self.session.post(
            f"{self.config.conversation_service_base_url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        ) as response:
            body = await response.json(content_type=None)
            return response.status, body if isinstance(body, dict) else {"data": body}

    async def _create_ticket(self, call: ActionToolCall, started_ns: int) -> ActionResult:
        title = str(call.arguments.get("title") or "").strip()
        description = str(call.arguments.get("description") or "").strip()
        if not title or not description:
            return ActionResult(
                "failed",
                localized_action_text(
                    "I still need the ticket details before I can create it.",
                    "Il me manque encore des détails pour créer le ticket.",
                    self.context.tts_language_hint,
                ),
                {},
                0.0,
            )
        payload: dict[str, Any] = {
            "agent_id": self.context.agent_id,
            "customer_contact": self.context.end_user_id or None,
            "title": title[:255],
            "description": description,
            "priority": str(call.arguments.get("priority") or "normal").strip(),
            "status": "open",
        }
        try:
            status, body = await self._post("/v1/tickets", payload)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return self._failed(exc, started_ns)
        elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000
        if status >= 400:
            return ActionResult(
                "failed",
                localized_action_text(
                    "I couldn't create the ticket right now.",
                    "Je n’ai pas pu créer le ticket pour le moment.",
                    self.context.tts_language_hint,
                ),
                body,
                elapsed,
            )
        code = str(body.get("ticket_code") or body.get("id") or "").strip()
        message = localized_action_text(
            "The support ticket has been created.",
            "Le ticket d’assistance a été créé.",
            self.context.tts_language_hint,
        )
        if code:
            message += localized_action_text(
                f" The reference is {code}.",
                f" La référence est {code}.",
                self.context.tts_language_hint,
            )
        return ActionResult("success", message, body, elapsed)

    async def _send_email(self, call: ActionToolCall, started_ns: int) -> ActionResult:
        recipient = str(call.arguments.get("to_email") or "").strip()
        if not recipient and "@" in self.context.end_user_id:
            recipient = self.context.end_user_id
        subject = str(call.arguments.get("subject") or "").strip()
        body_text = str(call.arguments.get("body") or "").strip()
        if not recipient or "@" not in recipient or not subject or not body_text:
            return ActionResult(
                "failed",
                localized_action_text(
                    "I still need a valid email address and message details.",
                    "Il me faut une adresse e-mail valide et le contenu du message.",
                    self.context.tts_language_hint,
                ),
                {},
                0.0,
            )
        try:
            status, body = await self._post(
                "/v1/tools/send-email",
                {"to_email": recipient, "subject": subject, "body": body_text},
            )
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return self._failed(exc, started_ns)
        elapsed = (time.perf_counter_ns() - started_ns) / 1_000_000
        if status >= 400 or bool(body.get("mocked")) or not bool(body.get("sent")):
            return ActionResult(
                "failed",
                localized_action_text(
                    str(body.get("message") or "I couldn't send the email right now."),
                    "Je n’ai pas pu envoyer l’e-mail pour le moment.",
                    self.context.tts_language_hint,
                ),
                body,
                elapsed,
            )
        return ActionResult(
            "success",
            localized_action_text(
                f"The email has been sent to {recipient}.",
                f"L’e-mail a été envoyé à {recipient}.",
                self.context.tts_language_hint,
            ),
            body,
            elapsed,
        )

    def _failed(self, exc: Exception, started_ns: int) -> ActionResult:
        return ActionResult(
            "failed",
            localized_action_text(
                "I couldn't complete that action right now.",
                "Je n’ai pas pu effectuer cette action pour le moment.",
                self.context.tts_language_hint,
            ),
            {"error": str(exc), "error_type": type(exc).__name__},
            (time.perf_counter_ns() - started_ns) / 1_000_000,
        )
