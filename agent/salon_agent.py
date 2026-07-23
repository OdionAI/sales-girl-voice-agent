import json
import logging
import os
import re
import uuid
from typing import Any

from livekit.agents import Agent, RunContext, function_tool, llm

from .tool_schema_compat import (
    CREATE_BOOKING_RAW_SCHEMA,
    CREATE_ORDER_RAW_SCHEMA,
    SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA,
    TRANSFER_TO_AICC_RAW_SCHEMA,
    normalize_order_items,
    normalize_price_snapshot,
    normalize_top_k,
)
from .ops_api import (
    apply_billing_adjustment as apply_billing_adjustment_api,
    block_card as block_card_api,
    check_transaction_status as check_transaction_status_api,
    create_payment_plan as create_payment_plan_api,
    create_booking as create_booking_api,
    create_order as create_order_api,
    create_ticket as create_ticket_api,
    create_complaint_ticket as create_complaint_ticket_api,
    create_meter_request as create_meter_request_api,
    escalate_issue as escalate_issue_api,
    fetch_menu_availability as fetch_menu_availability_api,
    fetch_product_availability as fetch_product_availability_api,
    fetch_room_availability as fetch_room_availability_api,
    get_account_overview as get_account_overview_api,
    get_payment_summary as get_payment_summary_api,
    get_recent_transactions as get_recent_transactions_api,
    get_tariff_profile as get_tariff_profile_api,
    get_vending_history as get_vending_history_api,
    lookup_customer_account as lookup_customer_account_api,
    report_outage as report_outage_api,
    refresh_meter_token_state as refresh_meter_token_state_api,
    reverse_failed_transaction as reverse_failed_transaction_api,
    search_business_knowledge as search_business_knowledge_api,
    send_email as send_email_api,
    transfer_to_aicc as transfer_to_aicc_api,
    unblock_card as unblock_card_api,
    update_customer_record as update_customer_record_api,
)

logger = logging.getLogger(__name__)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")
ALWAYS_ENABLED_RUNTIME_TOOLS = {"search_business_knowledge"}
_TEXTUAL_FUNCTION_CALL_PATTERN = re.compile(
    r"^\s*<function>\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\{.*\})\s*</function>\s*$",
    re.DOTALL | re.IGNORECASE,
)
_TEXTUAL_FUNCTION_MARKER = "<function>"


def _runtime_tool_name(tool: Any) -> str:
    return str(getattr(getattr(tool, "info", None), "name", "") or "").strip()


def select_enabled_runtime_tools(
    tools: list[Any], enabled_tool_names: list[str] | set[str] | tuple[str, ...]
) -> list[Any]:
    """Return only tools explicitly enabled for this configured agent."""
    enabled = {
        str(name or "").strip()
        for name in enabled_tool_names
        if str(name or "").strip()
    }
    enabled.update(ALWAYS_ENABLED_RUNTIME_TOOLS)
    return [tool for tool in tools if _runtime_tool_name(tool) in enabled]


def parse_textual_function_call(
    text: str, *, allowed_tool_names: set[str]
) -> tuple[str, str] | None:
    """Recover the exact textual tool-call form occasionally emitted by MaaS."""
    match = _TEXTUAL_FUNCTION_CALL_PATTERN.fullmatch(str(text or ""))
    if not match:
        return None
    tool_name = match.group(1).strip()
    if tool_name not in allowed_tool_names:
        return None
    try:
        arguments = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return tool_name, json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _text_from_llm_chunk(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, llm.ChatChunk) and chunk.delta is not None:
        return str(chunk.delta.content or "")
    return ""


def _is_possible_textual_function_prefix(text: str) -> bool:
    candidate = str(text or "").lstrip().lower()
    if not candidate:
        return True
    return _TEXTUAL_FUNCTION_MARKER.startswith(candidate) or candidate.startswith(
        _TEXTUAL_FUNCTION_MARKER
    )


def _tool_metadata(ctx: RunContext) -> dict:
    session_userdata = getattr(ctx, "userdata", None)
    if not isinstance(session_userdata, dict):
        session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    if not isinstance(session_userdata, dict):
        session_userdata = {}

    room_name = str(session_userdata.get("room_name") or "").strip()
    if not room_name:
        room_name = str(
            getattr(getattr(ctx, "room", None), "name", "")
            or getattr(getattr(getattr(ctx, "session", None), "room", None), "name", "")
        ).strip()
    conversation_id = str(session_userdata.get("conversation_id") or room_name)
    session_id = str(session_userdata.get("session_id") or conversation_id)
    return {
        "client_id": AGENT_CLIENT_ID,
        "agent_id": str(
            session_userdata.get("agent_config_id")
            or session_userdata.get("agent_id")
            or AGENT_NAME
        ),
        "business_id": str(session_userdata.get("business_id") or ""),
        "business_use_case": str(session_userdata.get("business_use_case") or ""),
        "knowledge_base_ids": list(session_userdata.get("knowledge_base_ids") or []),
        "live_data_endpoint": str(session_userdata.get("live_data_endpoint") or ""),
        "conversation_id": conversation_id,
        "session_id": session_id,
        "end_user_id": str(session_userdata.get("end_user_id") or ""),
        "enabled_tool_names": list(session_userdata.get("enabled_tool_names") or []),
        "turn_index": int(session_userdata.get("turn_index", 0)),
        "last_user_transcript": str(session_userdata.get("last_user_transcript") or ""),
        "last_assistant_message": str(
            session_userdata.get("last_assistant_message") or ""
        ),
        "timeline_event_index": int(session_userdata.get("timeline_event_index", 0)),
        "room_name": str(room_name or ""),
    }


def _session_userdata(ctx: RunContext) -> dict:
    session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    return session_userdata if isinstance(session_userdata, dict) else {}


def _recent_ticket_reuse_result(
    session_userdata: dict,
    *,
    title: str,
    customer_identifier: str | None = None,
) -> dict | None:
    last_result = session_userdata.get("last_create_ticket_result")
    if not isinstance(last_result, dict):
        return None

    last_turn = int(session_userdata.get("last_create_ticket_success_turn", -999))
    current_turn = int(session_userdata.get("turn_index", 0))
    if current_turn - last_turn > 1:
        return None

    requested_title = str(title or "").strip().lower()
    previous_title = str(last_result.get("title") or "").strip().lower()
    if not requested_title or requested_title != previous_title:
        return None

    reused = dict(last_result)
    if customer_identifier:
        reused["customer_identifier_hint"] = customer_identifier
    reused["status"] = "success"
    reused["reused_existing_ticket"] = True
    reused["message"] = "A recent ticket already exists for this conversation."
    return reused


def _is_tool_enabled(ctx: RunContext, tool_name: str) -> bool:
    normalized_tool_name = str(tool_name or "").strip()
    if normalized_tool_name in ALWAYS_ENABLED_RUNTIME_TOOLS:
        return True
    session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    if not isinstance(session_userdata, dict):
        return True
    enabled_tool_names = session_userdata.get("enabled_tool_names")
    if not isinstance(enabled_tool_names, list):
        return True
    normalized_enabled = {
        str(item or "").strip()
        for item in enabled_tool_names
        if str(item or "").strip()
    }
    if not normalized_enabled:
        return False
    return normalized_tool_name in normalized_enabled


class SalonAgent(Agent):
    """
    Shared English customer support agent for business-specific use cases.
    """

    async def llm_node(self, chat_ctx, tools, model_settings):
        """Convert MaaS textual function markup into a real LiveKit tool call.

        GLM normally returns OpenAI-compatible structured tool calls. If it emits
        the fallback ``<function>name{...}</function>`` representation, hold that
        response out of TTS and recover it only for a tool enabled in this turn.
        """
        pending: list[Any] = []
        buffered_text = ""
        probing = True
        last_chunk_id = ""

        async for chunk in Agent.default.llm_node(
            self, chat_ctx, tools, model_settings
        ):
            if isinstance(chunk, llm.ChatChunk):
                last_chunk_id = chunk.id or last_chunk_id
                if chunk.delta is not None and chunk.delta.tool_calls:
                    # While probing, pending content can only be whitespace or
                    # a textual-function prefix. Never send that prefix to TTS
                    # when the provider follows it with a structured call.
                    pending.clear()
                    probing = False
                    yield chunk
                    continue

            if not probing:
                yield chunk
                continue

            pending.append(chunk)
            buffered_text += _text_from_llm_chunk(chunk)
            if _is_possible_textual_function_prefix(buffered_text):
                continue

            for buffered_chunk in pending:
                yield buffered_chunk
            pending.clear()
            probing = False

        if not probing:
            return

        allowed_tool_names = {
            name for tool in tools if (name := _runtime_tool_name(tool))
        }
        recovered = parse_textual_function_call(
            buffered_text, allowed_tool_names=allowed_tool_names
        )
        if recovered:
            tool_name, arguments = recovered
            logger.warning(
                "Recovered textual MaaS function call as a structured tool call: tool=%s",
                tool_name,
            )
            yield llm.ChatChunk(
                id=last_chunk_id or f"textual-tool-{uuid.uuid4().hex}",
                delta=llm.ChoiceDelta(
                    role="assistant",
                    tool_calls=[
                        llm.FunctionToolCall(
                            name=tool_name,
                            arguments=arguments,
                            call_id=f"call_textual_{uuid.uuid4().hex}",
                        )
                    ],
                ),
            )
            for buffered_chunk in pending:
                if isinstance(buffered_chunk, llm.ChatChunk) and buffered_chunk.usage:
                    yield llm.ChatChunk(id=buffered_chunk.id, usage=buffered_chunk.usage)
            return

        if buffered_text.lstrip().lower().startswith(_TEXTUAL_FUNCTION_MARKER):
            logger.error(
                "Suppressed malformed or unauthorized textual MaaS function call."
            )
            activity = getattr(self, "_activity", None)
            session = getattr(activity, "session", None)
            userdata = getattr(session, "userdata", None)
            language = (
                str(userdata.get("language") or "")
                if isinstance(userdata, dict)
                else ""
            )
            if language.lower() == "fr":
                yield "Je suis désolée, je n’ai pas pu terminer cette action. Pourriez-vous réessayer ?"
            else:
                yield "I'm sorry, I couldn't complete that action. Could you try again?"
            return

        for buffered_chunk in pending:
            yield buffered_chunk

    @function_tool(raw_schema=SEARCH_BUSINESS_KNOWLEDGE_RAW_SCHEMA)
    async def search_business_knowledge(
        self,
        ctx: RunContext,
        raw_arguments: dict | None = None,
    ) -> dict:
        args = raw_arguments if isinstance(raw_arguments, dict) else {}
        query = str(args.get("query") or "").strip()
        if not query:
            return {
                "status": "failed",
                "message": "Please provide what you want me to look up.",
            }
        result = await search_business_knowledge_api(
            query=query,
            top_k=normalize_top_k(args.get("top_k")),
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] search_business_knowledge query=%s", query[:120])
        return result

    @function_tool()
    async def lookup_customer_account(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Look up the caller's electricity account profile. You can provide an account number, phone number, or email, but if omitted the caller email from the session is used automatically."""
        return await lookup_customer_account_api(
            customer_identifier=customer_identifier,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def get_tariff_profile(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Retrieve the caller's tariff band, meter type, feeder, and service-area details. If no identifier is provided, use the current caller automatically."""
        return await get_tariff_profile_api(
            customer_identifier=customer_identifier,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def get_payment_summary(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Retrieve the caller's recent bill and payment history. If no identifier is provided, use the current caller automatically."""
        return await get_payment_summary_api(
            customer_identifier=customer_identifier,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def get_vending_history(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Retrieve the caller's recent token vending and meter interaction history. If no identifier is provided, use the current caller automatically."""
        return await get_vending_history_api(
            customer_identifier=customer_identifier,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def create_complaint_ticket(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        customer_identifier: str | None = None,
        priority: str = "high",
        case_reference: str | None = None,
    ) -> dict:
        """Create a complaint ticket for the caller for billing, technical, or account issues. If no identifier is provided, use the current caller automatically."""
        result = await create_complaint_ticket_api(
            customer_identifier=customer_identifier,
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] create_complaint_ticket title=%s case_reference=%s",
                title,
                case_reference,
            )
        return result

    @function_tool()
    async def create_ticket(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        issue_type: str = "general",
        customer_identifier: str | None = None,
        priority: str = "high",
        requires_human: bool = True,
        case_reference: str | None = None,
    ) -> dict:
        """Create a human follow-up ticket for issues the agent cannot fully resolve. If no identifier is provided, use the current caller automatically. (Créer un ticket de suivi humain pour les problèmes que l'agent ne peut pas résoudre entièrement)."""
        if not _is_tool_enabled(ctx, "create_ticket"):
            return {
                "status": "failed",
                "message": "I can't create a support ticket from this agent right now.",
            }
        session_userdata = _session_userdata(ctx)
        reused = _recent_ticket_reuse_result(
            session_userdata,
            title=title,
            customer_identifier=customer_identifier,
        )
        if reused:
            logger.info("[TOOL] create_ticket reused existing ticket title=%s", title)
            return reused

        result = await create_ticket_api(
            customer_identifier=customer_identifier,
            title=title,
            description=description,
            issue_type=issue_type,
            priority=priority,
            requires_human=requires_human,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            session_userdata["last_create_ticket_success_turn"] = int(
                session_userdata.get("turn_index", 0)
            )
            session_userdata["last_create_ticket_result"] = result
            logger.info(
                "[TOOL] create_ticket title=%s issue_type=%s", title, issue_type
            )
        return result

    @function_tool(raw_schema=TRANSFER_TO_AICC_RAW_SCHEMA)
    async def transfer_to_aicc(
        self,
        ctx: RunContext,
        raw_arguments: dict | None = None,
    ) -> dict:
        """Transfer the current live caller to the configured Huawei AICC human-agent route when the caller asks for a human or the AI cannot resolve the request safely."""
        if not _is_tool_enabled(ctx, "transfer_to_aicc"):
            return {
                "status": "failed",
                "message": "I can't transfer this call to a human agent from this agent right now.",
            }
        args = raw_arguments if isinstance(raw_arguments, dict) else {}
        reason_summary = str(args.get("reason_summary") or "").strip()
        result = await transfer_to_aicc_api(
            reason_summary=reason_summary,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] transfer_to_aicc reason=%s", reason_summary or "")
        return result

    @function_tool(raw_schema=CREATE_BOOKING_RAW_SCHEMA)
    async def create_booking(
        self,
        ctx: RunContext,
        raw_arguments: dict | None = None,
    ) -> dict:
        if not _is_tool_enabled(ctx, "create_booking"):
            return {
                "status": "failed",
                "message": "I can't create a booking from this agent right now.",
            }
        args = raw_arguments if isinstance(raw_arguments, dict) else {}
        room_type = str(args.get("room_type") or "").strip()
        check_in_date = str(args.get("check_in_date") or "").strip()
        check_out_date = str(args.get("check_out_date") or "").strip()
        if not room_type or not check_in_date or not check_out_date:
            return {
                "status": "failed",
                "message": (
                    "I need the room type, check-in date, and check-out date "
                    "before I can create the booking."
                ),
            }
        try:
            guest_count = max(1, int(args.get("guest_count") or 1))
        except (TypeError, ValueError):
            guest_count = 1
        guest_name = str(args.get("guest_name") or "").strip() or None
        special_requests = str(args.get("special_requests") or "").strip() or None
        customer_identifier = (
            str(args.get("customer_identifier") or "").strip() or None
        )
        result = await create_booking_api(
            customer_identifier=customer_identifier,
            guest_name=guest_name,
            room_type=room_type,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            guest_count=guest_count,
            special_requests=special_requests,
            price_snapshot=normalize_price_snapshot(args.get("price_snapshot")),
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] create_booking room_type=%s check_in=%s",
                room_type,
                check_in_date,
            )
        return result

    @function_tool(raw_schema=CREATE_ORDER_RAW_SCHEMA)
    async def create_order(
        self,
        ctx: RunContext,
        raw_arguments: dict | None = None,
    ) -> dict:
        if not _is_tool_enabled(ctx, "create_order"):
            return {
                "status": "failed",
                "message": "I can't create an order from this agent right now.",
            }
        args = raw_arguments if isinstance(raw_arguments, dict) else {}
        item_name = str(args.get("item_name") or "").strip()
        try:
            quantity = max(1, int(args.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        items = normalize_order_items(args.get("items"))
        customer_name = str(args.get("customer_name") or "").strip() or None
        notes = str(args.get("notes") or "").strip() or None
        customer_identifier = (
            str(args.get("customer_identifier") or "").strip() or None
        )
        result = await create_order_api(
            customer_identifier=customer_identifier,
            customer_name=customer_name,
            item_name=item_name,
            quantity=quantity,
            items=items,
            notes=notes,
            price_snapshot=normalize_price_snapshot(args.get("price_snapshot")),
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] create_order item_name=%s quantity=%s items=%s",
                item_name,
                quantity,
                items,
            )
        return result

    @function_tool()
    async def fetch_room_availability(
        self,
        ctx: RunContext,
        endpoint_url: str | None = None,
        room_type: str | None = None,
        check_in_date: str | None = None,
        check_out_date: str | None = None,
        guest_count: int | None = None,
    ) -> dict:
        """Fetch current room availability and prices. Use this for broad questions like what rooms are available or how much they cost, even if the guest has not given dates yet."""
        if not _is_tool_enabled(ctx, "fetch_room_availability"):
            return {
                "status": "failed",
                "message": "I can't check current room availability from this agent right now.",
            }
        result = await fetch_room_availability_api(
            endpoint_url=endpoint_url,
            room_type=room_type,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            guest_count=guest_count,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] fetch_room_availability room_type=%s", room_type)
        return result

    @function_tool()
    async def fetch_menu_availability(
        self,
        ctx: RunContext,
        endpoint_url: str | None = None,
        item_name: str | None = None,
        party_size: int | None = None,
    ) -> dict:
        """Fetch the current menu and prices. Use this for broad questions like what is available or how much items cost, even if the customer has not named a specific item yet."""
        if not _is_tool_enabled(ctx, "fetch_menu_availability"):
            return {
                "status": "failed",
                "message": "I can't check the current menu or prices from this agent right now.",
            }
        result = await fetch_menu_availability_api(
            endpoint_url=endpoint_url,
            item_name=item_name,
            party_size=party_size,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] fetch_menu_availability item_name=%s", item_name)
        return result

    @function_tool()
    async def fetch_product_availability(
        self,
        ctx: RunContext,
        endpoint_url: str | None = None,
        product_name: str | None = None,
        size: str | None = None,
        color: str | None = None,
    ) -> dict:
        """Fetch current product availability and prices. Use this for broad questions about what is available or how much items cost, even before the customer narrows down the request."""
        if not _is_tool_enabled(ctx, "fetch_product_availability"):
            return {
                "status": "failed",
                "message": "I can't check current product availability from this agent right now.",
            }
        result = await fetch_product_availability_api(
            endpoint_url=endpoint_url,
            product_name=product_name,
            size=size,
            color=color,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] fetch_product_availability product_name=%s", product_name
            )
        return result

    @function_tool()
    async def report_outage(
        self,
        ctx: RunContext,
        summary: str,
        customer_identifier: str | None = None,
        priority: str = "high",
    ) -> dict:
        """Report a power outage or low-voltage issue for the caller. If no identifier is provided, use the current caller automatically."""
        result = await report_outage_api(
            customer_identifier=customer_identifier,
            summary=summary,
            priority=priority,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] report_outage priority=%s", priority)
        return result

    @function_tool()
    async def create_meter_request(
        self,
        ctx: RunContext,
        summary: str,
        customer_identifier: str | None = None,
        priority: str = "normal",
    ) -> dict:
        """Create a meter-related request for the caller. If no identifier is provided, use the current caller automatically."""
        result = await create_meter_request_api(
            customer_identifier=customer_identifier,
            summary=summary,
            priority=priority,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_meter_request priority=%s", priority)
        return result

    @function_tool()
    async def apply_billing_adjustment(
        self,
        ctx: RunContext,
        amount: float,
        reason: str,
        customer_identifier: str | None = None,
    ) -> dict:
        """Apply a billing adjustment when the account already qualifies for a straightforward correction."""
        result = await apply_billing_adjustment_api(
            customer_identifier=customer_identifier,
            amount=amount,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] apply_billing_adjustment amount=%s", amount)
        return result

    @function_tool()
    async def refresh_meter_token_state(
        self,
        ctx: RunContext,
        reason: str,
        customer_identifier: str | None = None,
    ) -> dict:
        """Refresh meter token state after a token delivery or loading issue so the customer can retry."""
        result = await refresh_meter_token_state_api(
            customer_identifier=customer_identifier,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] refresh_meter_token_state")
        return result

    @function_tool()
    async def update_customer_record(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        service_address: str | None = None,
    ) -> dict:
        """Update the caller's contact details or service address when the change can be completed immediately."""
        result = await update_customer_record_api(
            customer_identifier=customer_identifier,
            email=email,
            phone=phone,
            service_address=service_address,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] update_customer_record")
        return result

    @function_tool()
    async def create_payment_plan(
        self,
        ctx: RunContext,
        plan_name: str,
        installment_count: int,
        monthly_amount: float,
        reason: str,
        customer_identifier: str | None = None,
    ) -> dict:
        """Create a payment plan for an eligible customer and return the plan terms."""
        result = await create_payment_plan_api(
            customer_identifier=customer_identifier,
            plan_name=plan_name,
            installment_count=installment_count,
            monthly_amount=monthly_amount,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_payment_plan plan_name=%s", plan_name)
        return result

    @function_tool()
    async def escalate_issue(
        self,
        ctx: RunContext,
        title: str,
        description: str,
        customer_identifier: str | None = None,
        priority: str = "high",
        case_reference: str | None = None,
    ) -> dict:
        """Escalate a human-handled issue for the caller. If no identifier is provided, use the current caller automatically."""
        result = await escalate_issue_api(
            customer_identifier=customer_identifier,
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] escalate_issue title=%s case_reference=%s",
                title,
                case_reference,
            )
        return result

    @function_tool()
    async def get_account_overview(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Retrieve the caller's bank account summary, balances, cards, and headline recent transactions."""
        return await get_account_overview_api(
            customer_identifier=customer_identifier,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def get_recent_transactions(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
        limit: int = 5,
    ) -> dict:
        """Retrieve the caller's recent bank transactions. If no identifier is provided, use the current caller automatically."""
        return await get_recent_transactions_api(
            customer_identifier=customer_identifier,
            limit=limit,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def check_transaction_status(
        self,
        ctx: RunContext,
        transaction_reference: str | None = None,
        amount_naira: float | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Check whether a bank transfer is pending, failed, reversed, or completed."""
        return await check_transaction_status_api(
            customer_identifier=customer_identifier,
            transaction_reference=transaction_reference,
            amount_naira=amount_naira,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def block_card(
        self,
        ctx: RunContext,
        reason: str,
        last4: str | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Block the caller's debit card when card protection is required."""
        return await block_card_api(
            customer_identifier=customer_identifier,
            last4=last4,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def unblock_card(
        self,
        ctx: RunContext,
        reason: str,
        last4: str | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Unblock the caller's debit card when the backend confirms automated unblocking is allowed."""
        return await unblock_card_api(
            customer_identifier=customer_identifier,
            last4=last4,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )

    @function_tool()
    async def send_email(
        self,
        ctx: RunContext,
        to_email: str,
        subject: str,
        body_text: str,
    ) -> dict:
        """Send an email to the caller. Use this tool when you need to send links, documents, or written instructions. (Envoyer un email à l'appelant. Utilisez cet outil lorsque vous devez envoyer des liens, des documents ou des instructions écrites)."""
        if not _is_tool_enabled(ctx, "send_email"):
            return {
                "status": "failed",
                "message": "I can't send an email from this agent right now.",
            }
        result = await send_email_api(
            to_email=to_email,
            subject=subject,
            body_text=body_text,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] send_email to=%s subject=%s", to_email, subject)
        return result

    @function_tool()
    async def reverse_failed_transaction(
        self,
        ctx: RunContext,
        transaction_reference: str,
        reason: str,
        customer_identifier: str | None = None,
    ) -> dict:
        """Reverse a failed but debited bank transaction when the backend confirms it is eligible."""
        return await reverse_failed_transaction_api(
            customer_identifier=customer_identifier,
            transaction_reference=transaction_reference,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
