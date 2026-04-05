import logging
import os

from livekit.agents import Agent, RunContext, function_tool

from .ops_api import (
    apply_billing_adjustment as apply_billing_adjustment_api,
    create_booking as create_booking_api,
    create_order as create_order_api,
    create_payment_plan as create_payment_plan_api,
    create_ticket as create_ticket_api,
    create_complaint_ticket as create_complaint_ticket_api,
    create_meter_request as create_meter_request_api,
    escalate_issue as escalate_issue_api,
    fetch_menu_availability as fetch_menu_availability_api,
    fetch_product_availability as fetch_product_availability_api,
    fetch_room_availability as fetch_room_availability_api,
    get_payment_summary as get_payment_summary_api,
    get_tariff_profile as get_tariff_profile_api,
    get_vending_history as get_vending_history_api,
    lookup_customer_account as lookup_customer_account_api,
    report_outage as report_outage_api,
    refresh_meter_token_state as refresh_meter_token_state_api,
    update_customer_record as update_customer_record_api,
)

logger = logging.getLogger(__name__)
AGENT_CLIENT_ID = os.getenv("AGENT_CLIENT_ID", "sales-girl-internal")
AGENT_NAME = os.getenv("AGENT_NAME", "sales-girl-agent-fr")


def _tool_metadata(ctx: RunContext) -> dict:
    session_userdata = getattr(getattr(ctx, "session", None), "userdata", None)
    if not isinstance(session_userdata, dict):
        session_userdata = {}

    room_name = getattr(getattr(ctx, "room", None), "name", "")
    conversation_id = str(session_userdata.get("conversation_id") or room_name)
    session_id = str(session_userdata.get("session_id") or conversation_id)
    return {
        "client_id": AGENT_CLIENT_ID,
        "agent_id": str(session_userdata.get("agent_config_id") or session_userdata.get("agent_id") or AGENT_NAME),
        "business_id": str(session_userdata.get("business_id") or ""),
        "business_use_case": str(session_userdata.get("business_use_case") or ""),
        "live_data_endpoint": str(session_userdata.get("live_data_endpoint") or ""),
        "conversation_id": conversation_id,
        "session_id": session_id,
        "end_user_id": str(session_userdata.get("end_user_id") or ""),
        "enabled_tool_names": list(session_userdata.get("enabled_tool_names") or []),
        "turn_index": int(session_userdata.get("turn_index", 0)),
        "last_user_transcript": str(session_userdata.get("last_user_transcript") or ""),
        "last_assistant_message": str(session_userdata.get("last_assistant_message") or ""),
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


class SalonAgentFR(Agent):
    """
    Agent de support client EKEDC (francais).
    """

    @function_tool()
    async def lookup_customer_account(
        self,
        ctx: RunContext,
        customer_identifier: str | None = None,
    ) -> dict:
        """Recuperer le profil du client a partir du numero de compte, du telephone ou de l'email. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Recuperer la bande tarifaire, le type de compteur et les details du service a partir du numero de compte, du telephone ou de l'email. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Recuperer l'historique recent des factures et paiements a partir du numero de compte, du telephone ou de l'email. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Recuperer l'historique recent des achats de token et du compteur a partir du numero de compte, du telephone ou de l'email. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Creer un ticket de plainte pour un probleme de facturation, technique ou de compte. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
        result = await create_complaint_ticket_api(
            customer_identifier=customer_identifier,
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_complaint_ticket title=%s case_reference=%s", title, case_reference)
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
        """Creer un ticket unique pour les problemes qui necessitent un suivi humain."""
        if not _is_tool_enabled(ctx, "create_ticket"):
            return {"status": "failed", "message": "Je ne peux pas créer de ticket de suivi depuis cet agent pour le moment."}
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
            logger.info("[TOOL] create_ticket title=%s issue_type=%s", title, issue_type)
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
        """Creer une reservation d'hotel dans la plateforme pour l'invite et le business courant."""
        if not _is_tool_enabled(ctx, "create_booking"):
            return {"status": "failed", "message": "Je ne peux pas créer de réservation depuis cet agent pour le moment."}
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
            logger.info("[TOOL] create_booking room_type=%s check_in=%s", room_type, check_in_date)
        return result

    @function_tool()
    async def create_order(
        self,
        ctx: RunContext,
        item_name: str,
        quantity: int = 1,
        customer_name: str | None = None,
        notes: str | None = None,
        price_snapshot: dict | str | None = None,
        customer_identifier: str | None = None,
    ) -> dict:
        """Creer une commande restaurant ou mode dans la plateforme pour le client courant."""
        if not _is_tool_enabled(ctx, "create_order"):
            return {"status": "failed", "message": "Je ne peux pas créer de commande depuis cet agent pour le moment."}
        result = await create_order_api(
            customer_identifier=customer_identifier,
            customer_name=customer_name,
            item_name=item_name,
            quantity=quantity,
            notes=notes,
            price_snapshot=price_snapshot,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] create_order item_name=%s quantity=%s", item_name, quantity)
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
        """Recuperer la disponibilite et les prix actuels depuis l'endpoint hotel lorsqu'il est configure."""
        if not _is_tool_enabled(ctx, "fetch_room_availability"):
            return {"status": "failed", "message": "Je ne peux pas vérifier la disponibilité actuelle des chambres depuis cet agent pour le moment."}
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
        """Recuperer les articles de menu disponibles et leurs prix depuis l'endpoint configure."""
        if not _is_tool_enabled(ctx, "fetch_menu_availability"):
            return {"status": "failed", "message": "Je ne peux pas vérifier le menu actuel ni les prix depuis cet agent pour le moment."}
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
        """Recuperer les produits disponibles et leurs prix depuis l'endpoint configure."""
        if not _is_tool_enabled(ctx, "fetch_product_availability"):
            return {"status": "failed", "message": "Je ne peux pas vérifier la disponibilité actuelle des produits depuis cet agent pour le moment."}
        result = await fetch_product_availability_api(
            endpoint_url=endpoint_url,
            product_name=product_name,
            size=size,
            color=color,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] fetch_product_availability product_name=%s", product_name)
        return result

    @function_tool()
    async def report_outage(
        self,
        ctx: RunContext,
        summary: str,
        customer_identifier: str | None = None,
        priority: str = "high",
    ) -> dict:
        """Signaler une panne de courant ou un probleme de basse tension. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Creer une demande liee au compteur, a l'installation ou a la programmation. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
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
        """Appliquer un ajustement de facturation quand une correction simple est autorisee."""
        result = await apply_billing_adjustment_api(
            customer_identifier=customer_identifier,
            amount=amount,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
        return result

    @function_tool()
    async def refresh_meter_token_state(
        self,
        ctx: RunContext,
        reason: str,
        customer_identifier: str | None = None,
    ) -> dict:
        """Rafraichir l'etat du compteur ou du token pour permettre une nouvelle tentative."""
        result = await refresh_meter_token_state_api(
            customer_identifier=customer_identifier,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
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
        """Mettre a jour l'email, le telephone ou l'adresse du client."""
        result = await update_customer_record_api(
            customer_identifier=customer_identifier,
            email=email,
            phone=phone,
            service_address=service_address,
            metadata=_tool_metadata(ctx),
        )
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
        """Creer un plan de paiement pour un client eligible."""
        result = await create_payment_plan_api(
            customer_identifier=customer_identifier,
            plan_name=plan_name,
            installment_count=installment_count,
            monthly_amount=monthly_amount,
            reason=reason,
            metadata=_tool_metadata(ctx),
        )
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
        """Escalader les problemes qui exigent une intervention humaine ou terrain. Si aucun identifiant n'est fourni, utiliser automatiquement l'email de l'appelant."""
        result = await escalate_issue_api(
            customer_identifier=customer_identifier,
            title=title,
            description=description,
            priority=priority,
            case_reference=case_reference,
            metadata=_tool_metadata(ctx),
        )
        if result.get("status") != "failed":
            logger.info("[TOOL] escalate_issue title=%s case_reference=%s", title, case_reference)
        return result
