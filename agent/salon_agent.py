import logging
import os

from livekit.agents import Agent, RunContext, function_tool

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
    unblock_card as unblock_card_api,
    update_customer_record as update_customer_record_api,
)

logger = logging.getLogger(__name__)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-en")


def _tool_metadata(ctx: RunContext) -> dict:
    session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    if not isinstance(session_userdata, dict):
        session_userdata = {}

    room_name = getattr(getattr(ctx, "room", None), "name", "")
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
    }


def _is_tool_enabled(ctx: RunContext, tool_name: str) -> bool:
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
    return str(tool_name or "").strip() in normalized_enabled


class SalonAgent(Agent):
    """
    Shared English customer support agent for business-specific use cases.
    """

    @function_tool()
    async def search_business_knowledge(
        self,
        ctx: RunContext,
        query: str,
        top_k: int = 4,
    ) -> dict:
        """Search the saved business knowledge base for policies, services, amenities, FAQs, and other documented facts before saying you do not know an answer."""
        result = await search_business_knowledge_api(
            query=query,
            top_k=top_k,
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
            logger.info(
                "[TOOL] create_ticket title=%s issue_type=%s", title, issue_type
            )
        return result

    @function_tool()
    async def create_booking(
        self,
        ctx: RunContext,
        room_type: str,
        check_in_date: str,
        check_out_date: str,
        guest_count: int = 1,
        guest_name: str | None = None,
        special_requests: str | None = None,
        price_snapshot: dict | str | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Create a hotel booking inside the platform for the current guest and business."""
        if not _is_tool_enabled(ctx, "create_booking"):
            return {
                "status": "failed",
                "message": "I can't create a booking from this agent right now.",
            }
        result = await create_booking_api(
            customer_identifier=customer_identifier,
            guest_name=guest_name,
            room_type=room_type,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            guest_count=guest_count,
            special_requests=special_requests,
            price_snapshot=price_snapshot,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info(
                "[TOOL] create_booking room_type=%s check_in=%s",
                room_type,
                check_in_date,
            )
        return result

    @function_tool()
    async def create_order(
        self,
        ctx: RunContext,
        item_name: str = "",
        quantity: int = 1,
        items: list[dict] | None = None,
        customer_name: str | None = None,
        notes: str | None = None,
        price_snapshot: dict | str | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Create a restaurant or fashion order inside the platform for the current customer and business.

        If the customer orders multiple different items (e.g. Rice and Chicken), you MUST use the `items` array.
        Format for items: [{"item_name": "Fried Rice", "quantity": 1, "price_snapshot": {"amount": 2500, "currency": "NGN"}}]

        For single items, you must provide `price_snapshot` in the format: {"amount": 2500, "currency": "NGN"}
        """
        # Ensure tool is currently enabled for this specific agent context
        if not _is_tool_enabled(ctx, "create_order"):
            return {
                "status": "failed",
                "message": "I can't create an order from this agent right now.",
            }
        result = await create_order_api(
            customer_identifier=customer_identifier,
            customer_name=customer_name,
            item_name=item_name,
            quantity=quantity,
            items=items,
            notes=notes,
            price_snapshot=price_snapshot,
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
