import logging
import os

from livekit.agents import Agent, RunContext, function_tool

from .ops_api import (
    apply_billing_adjustment as apply_billing_adjustment_api,
    create_payment_plan as create_payment_plan_api,
    create_ticket as create_ticket_api,
    create_complaint_ticket as create_complaint_ticket_api,
    create_meter_request as create_meter_request_api,
    escalate_issue as escalate_issue_api,
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
        "agent_id": AGENT_NAME,
        "business_id": str(session_userdata.get("business_id") or ""),
        "conversation_id": conversation_id,
        "session_id": session_id,
        "end_user_id": str(session_userdata.get("end_user_id") or ""),
        "turn_index": int(session_userdata.get("turn_index", 0)),
        "last_user_transcript": str(session_userdata.get("last_user_transcript") or ""),
        "last_assistant_message": str(session_userdata.get("last_assistant_message") or ""),
        "timeline_event_index": int(session_userdata.get("timeline_event_index", 0)),
    }


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
