from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


AccountNumber = Annotated[str, Field(pattern=r"^[0-9]{10}$")]
PhoneNumber = Annotated[str, Field(pattern=r"^(?:0[0-9]{10}|\+234[0-9]{10})$")]
Money = Annotated[str, Field(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,2})?$")]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BalanceInput(ToolInput):
    source_account: AccountNumber | None = Field(
        None, description="Owned account selected by the caller; omit to list/select."
    )


class HistoryInput(BalanceInput):
    count: int = Field(5, ge=1, le=20)


class DataPlansInput(ToolInput):
    network: str | None = Field(None, min_length=1, max_length=40)


class BankSearchInput(ToolInput):
    query: str | None = Field(None, min_length=1, max_length=100)


class DataPurchaseInput(BalanceInput):
    network: str = Field(min_length=1, max_length=40)
    package_id: int = Field(gt=0, description="ID returned by wema_list_data_plans.")
    phone_number: PhoneNumber


class TransferInput(BalanceInput):
    bank: str = Field(min_length=1, max_length=100, description="Bank name or code.")
    recipient_account: AccountNumber
    amount: Money = Field(description="NGN amount as a decimal string, e.g. 2500.00.")
    narration: str = Field("", max_length=140)


class ExecuteInput(ToolInput):
    operation_id: str = Field(min_length=1, max_length=64)


class ToolResult(BaseModel):
    mode: Literal["mock", "live"] = "mock"
    status: Literal["ok", "needs_input", "prepared", "blocked", "failed"]
    message: str
    data: dict = Field(default_factory=dict)


# The HTTP adapter and dashboard export share the same validated input models.
TOOL_SPECS = {
    "wema_get_balance": (
        BalanceInput,
        "Check the caller's Wema account balance through the configured connector.",
    ),
    "wema_get_transactions": (
        HistoryInput,
        "Read recent Wema transactions for an account owned by the caller.",
    ),
    "wema_list_data_plans": (
        DataPlansInput,
        "List current data packages before offering or selecting a package.",
    ),
    "wema_list_transfer_banks": (
        BankSearchInput,
        "List or search the current destination banks returned by Wema before transfer preparation.",
    ),
    "wema_prepare_data_purchase": (
        DataPurchaseInput,
        "Prepare, but do not buy, a data package using a returned package ID. "
        "Price comes from the catalogue. Ask the caller to confirm the preview.",
    ),
    "wema_prepare_transfer": (
        TransferInput,
        "Prepare, but do not send, a bank transfer. Resolve bank and recipient "
        "before asking the caller to confirm recipient and amount.",
    ),
    "wema_execute_prepared": (
        ExecuteInput,
        "Request execution only after the caller confirms the latest preview. "
        "This branch blocks execution until a transaction executor is merged.",
    ),
}


def tool_definitions(base_url: str) -> list[dict]:
    return [
        {
            "name": name,
            "description": description,
            "method": "POST",
            "url": f"{base_url.rstrip('/')}/v1/tools/{name}",
            "request_schema": model.model_json_schema(),
            "is_active": True,
        }
        for name, (model, description) in TOOL_SPECS.items()
    ]
