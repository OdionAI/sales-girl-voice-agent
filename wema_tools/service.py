from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from .contracts import (
    BalanceInput,
    BankSearchInput,
    DataPlansInput,
    DataPurchaseInput,
    ExecuteInput,
    HistoryInput,
    ToolResult,
    TransferInput,
)
from .errors import WorkflowError


@dataclass(frozen=True)
class MockSession:
    business_id: str
    agent_id: str
    session_id: str
    caller_id: str
    customer_id: str = ""


class MockBank:
    """Synthetic read fixtures, not a live HTTP client or bank authentication."""

    mode = "mock"

    async def read(self, endpoint: str, **params):
        if endpoint == "A1":
            return [{
                "accountNumber": "0000000001",
                "accountName": "DEMO CUSTOMER - NOT A REAL ACCOUNT",
                "currency": "NGN",
                "availableBalance": "100000.00",
            }]
        if endpoint == "A2" and params["accountNumber"] == "0000000001":
            return {
                "accountNumber": "0000000001",
                "currency": "NGN",
                "availableBalance": "100000.00",
            }
        if endpoint == "A3" and params["AccountNumber"] == "0000000001":
            return [{
                "title": "MOCK purchase, not a real transaction",
                "date": "2026-09-01",
                "amount": "500.00",
            }][:params["Count"]]
        if endpoint == "D2":
            return [
                {
                    "networkProvider": "MTN",
                    "dataPackages": [{
                        "id": 1, "name": "MOCK 1GB", "amount": "500.00",
                        "dataPlan": "1GB", "validityPeriod": "30", "validityType": "Days",
                    }],
                },
                {
                    "networkProvider": "Airtel",
                    "dataPackages": [{
                        "id": 2, "name": "MOCK 2GB", "amount": "1000.00",
                        "dataPlan": "2GB", "validityPeriod": "30", "validityType": "Days",
                    }],
                },
            ]
        if endpoint == "T4":
            return [
                {"bankName": "Wema Bank", "bankCode": "035", "abbreviation": "WEMA"},
                {"bankName": "Access Bank", "bankCode": "044", "abbreviation": "ACCESS"},
            ]
        if endpoint == "T3":
            recipients = {
                ("035", "0000000002"): "DEMO WEMA RECIPIENT",
                ("044", "0000000003"): "DEMO ACCESS RECIPIENT",
            }
            name = recipients.get((params["bankCode"], params["accountNumber"]))
            if name:
                return {
                    "bankCode": params["bankCode"],
                    "accountNumber": params["accountNumber"],
                    "accountName": name,
                    "currency": "NGN",
                    "chargeFee": [{"charge": "10.00", "lower": "0", "upper": "50000"}],
                }
            raise WorkflowError("Recipient is not in the synthetic name-enquiry fixtures.")
        raise WorkflowError("This endpoint is not available in the read-only mock adapter.")


@dataclass
class PreparedOperation:
    session: MockSession
    workflow: str
    preview: dict
    expires_at: float


def result(status: str, message: str, *, mode: str = "mock", **data) -> ToolResult:
    return ToolResult(mode=mode, status=status, message=message, data=data)


class WemaWorkflows:
    """Composite reads and immutable previews. No method can submit a bank write."""

    def __init__(self, bank: MockBank | None = None, *, clock=time.monotonic):
        self.bank = bank if bank is not None else MockBank()
        self.mode = self.bank.mode
        self.clock = clock
        self.operations: dict[str, PreparedOperation] = {}

    def _result(self, status: str, message: str, **data) -> ToolResult:
        return result(status, message, mode=self.mode, **data)

    async def _account(self, session: MockSession, selection: str | None):
        # In live mode A1's customer ID must be resolved by a trusted session adapter.
        customer_id = session.customer_id or ("mock-customer" if self.mode == "mock" else "")
        if not customer_id:
            raise WorkflowError("Wema customer ID is not available in this session.")
        accounts = await self.bank.read("A1", customerID=customer_id)
        if not accounts:
            raise WorkflowError("No owned accounts are available.")
        if selection:
            matches = [a for a in accounts if a["accountNumber"] == selection]
            if not matches:
                raise WorkflowError("That source account is not owned by the demo customer.")
            return matches[0], None
        if len(accounts) > 1:
            choices = [{"source_account": a["accountNumber"], "currency": a["currency"]}
                       for a in accounts]
            return None, self._result("needs_input", "Which account should I use?", accounts=choices)
        return accounts[0], None

    def _prune(self):
        now = self.clock()
        self.operations = {k: v for k, v in self.operations.items() if v.expires_at > now}

    def _invalidate(self, session: MockSession):
        self._prune()
        # A correction or intent switch invalidates the previous transaction preview.
        self.operations = {k: v for k, v in self.operations.items() if v.session != session}

    def _prepare(self, session: MockSession, workflow: str, preview: dict) -> ToolResult:
        self._prune()
        if len(self.operations) >= 1000:
            return self._result("blocked", "Preview capacity reached; try again after expiry.")
        operation_id = str(uuid4())
        self.operations[operation_id] = PreparedOperation(
            session, workflow, copy.deepcopy(preview), self.clock() + 300,
        )
        return self._result(
            "prepared",
            "Prepared preview only. Nothing has been purchased or transferred. "
            "Confirm these details before transaction execution.",
            operation_id=operation_id,
            expires_in_seconds=300,
            preview=preview,
        )

    async def wema_get_balance(self, session: MockSession, args: BalanceInput) -> ToolResult:
        account, selection = await self._account(session, args.source_account)
        if selection:
            return selection
        details = await self.bank.read("A2", accountNumber=account["accountNumber"])
        if details["accountNumber"] != account["accountNumber"]:
            raise WorkflowError("Account details did not match the selected account.")
        message = "Synthetic balance only, not a real bank balance." if self.mode == "mock" else "Balance retrieved from Wema."
        return self._result("ok", message, **details)

    async def wema_get_transactions(self, session: MockSession, args: HistoryInput) -> ToolResult:
        account, selection = await self._account(session, args.source_account)
        if selection:
            return selection
        entries = await self.bank.read(
            "A3", Skip=0, Count=args.count, AccountNumber=account["accountNumber"], KeyWord="C",
        )
        message = "Synthetic transaction history only." if self.mode == "mock" else "Transaction history retrieved from Wema."
        return self._result("ok", message, transactions=entries,
                      filter_note="Uses the documented KeyWord=C; its bank meaning is unconfirmed.")

    async def wema_list_data_plans(self, session: MockSession, args: DataPlansInput) -> ToolResult:
        groups = await self.bank.read("D2")
        if args.network:
            groups = [g for g in groups if g["networkProvider"].casefold() == args.network.strip().casefold()]
        message = "Synthetic packages and prices only." if self.mode == "mock" else "Current data packages retrieved from Wema."
        return self._result("ok", message, networks=groups)

    async def wema_list_transfer_banks(self, session: MockSession, args: BankSearchInput) -> ToolResult:
        banks = await self.bank.read("T4")
        if args.query:
            query = args.query.strip().casefold()
            banks = [bank for bank in banks if query in {
                bank["bankName"].casefold(), bank["bankCode"],
                str(bank.get("abbreviation") or "").casefold(),
            } or query in bank["bankName"].casefold()]
        message = "Synthetic bank directory only." if self.mode == "mock" else "Current bank directory retrieved from Wema."
        return self._result("ok", message, banks=banks)

    async def wema_prepare_data_purchase(self, session: MockSession, args: DataPurchaseInput) -> ToolResult:
        self._invalidate(session)
        account, selection = await self._account(session, args.source_account)
        if selection:
            return selection
        groups = await self.bank.read("D2")
        matches = [(g, p) for g in groups for p in g["dataPackages"]
                   if g["networkProvider"].casefold() == args.network.strip().casefold()
                   and p["id"] == args.package_id]
        if len(matches) != 1:
            return self._result("needs_input", "Choose a package returned for that network.", networks=groups)
        group, package = matches[0]
        amount = Decimal(str(package["amount"]))
        if account["currency"] != "NGN" or not amount.is_finite() or amount <= 0:
            raise WorkflowError("Unsupported currency or invalid package price.")
        if amount > Decimal(str(account["availableBalance"])):
            raise WorkflowError("The selected account has insufficient funds for that package.")
        return self._prepare(session, "data", {
            "source_account": account["accountNumber"],
            "network": group["networkProvider"],
            "package_id": package["id"],
            "package_name": package["name"],
            "phone_number": args.phone_number,
            "amount": format(amount, ".2f"),
            "currency": "NGN",
            "validity": f'{package["validityPeriod"]} {package["validityType"]}',
            "execution_endpoint_id": "D3",
            "save_beneficiary": False,
            "auto_topup": False,
        })

    async def wema_prepare_transfer(self, session: MockSession, args: TransferInput) -> ToolResult:
        self._invalidate(session)
        amount = Decimal(args.amount)
        if amount <= 0:
            raise WorkflowError("Transfer amount must be greater than zero.")
        account, selection = await self._account(session, args.source_account)
        if selection:
            return selection
        if account["currency"] != "NGN":
            raise WorkflowError("Only NGN is supported by this prototype.")
        if amount > Decimal(str(account["availableBalance"])):
            raise WorkflowError("The selected account has insufficient funds for that amount.")
        banks = await self.bank.read("T4")
        query = args.bank.strip().casefold()
        matches = [b for b in banks if query in {
            b["bankName"].casefold(), b["bankCode"],
            str(b.get("abbreviation") or "").casefold(),
        }]
        if len(matches) != 1:
            return self._result("needs_input", "Which bank do you mean?", banks=banks)
        bank = matches[0]
        recipient = await self.bank.read(
            "T3", bankCode=bank["bankCode"], accountNumber=args.recipient_account,
        )
        if (recipient["bankCode"] != bank["bankCode"]
                or recipient["accountNumber"] != args.recipient_account
                or not recipient["accountName"].strip()
                or recipient["currency"] != "NGN"):
            raise WorkflowError("Name enquiry did not verify the selected recipient and currency.")
        return self._prepare(session, "transfer", {
            "source_account": account["accountNumber"],
            "recipient_account": recipient["accountNumber"],
            "recipient_name": recipient["accountName"],
            "bank_name": bank["bankName"],
            "bank_code": bank["bankCode"],
            "amount": format(amount, ".2f"),
            "currency": "NGN",
            "narration": args.narration,
            "fee_bands": recipient["chargeFee"],
            "fee_status": "unverified: bank fee calculation rules are not documented",
            "execution_endpoint_id": "T2" if bank["bankCode"] == "035" else "T1",
            "save_beneficiary": False,
        })

    async def wema_execute_prepared(self, session: MockSession, args: ExecuteInput) -> ToolResult:
        self._prune()
        operation = self.operations.get(args.operation_id)
        if not operation or operation.session != session:
            return self._result("blocked", "Preview is missing, expired, replaced, or belongs to another session.")
        return self._result(
            "blocked",
            "No bank transaction was submitted. The transaction executor is not connected on this branch.",
            operation_id=args.operation_id,
            reason="transaction_executor_not_configured",
        )
