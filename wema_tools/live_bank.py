from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .errors import WorkflowError


DOCUMENTED_BASE_URLS = {
    "airtime_data": "https://airtimeanddataplatformservice-alat-two.apps.alatarodev.westeurope.aroapp.io",
    "bills": "https://billspayment-alat-two.apps.alatarodev.westeurope.aroapp.io",
    "transfers": "https://transferplatformservice-route-alat-two.apps.alatarodev.westeurope.aroapp.io",
    "savings": "https://savings-alat-two.apps.alatarodev.westeurope.aroapp.io",
    "accounts": "https://accountmaintenance-route-alat-two.apps.alatarodev.westeurope.aroapp.io",
    "bank_prediction": "https://bankprediction.westeurope.inference.ml.azure.com",
    "voice_storage": "https://voicebankingsetup-route-alat-two.apps.alatarodev.westeurope.aroapp.io",
}

ENDPOINTS = {
    "D1": ("airtime_data", "POST", "/api/Airtime/PurchaseAirtimeV2"),
    "D2": ("airtime_data", "GET", "/api/Data/GetDataPlans"),
    "D3": ("airtime_data", "POST", "/api/Data/PurchaseDataV2"),
    "B1": ("bills", "GET", "/api/SharedBillsPayment/GetBillerCategories/{customerId}"),
    "B2": ("bills", "GET", "/api/SharedBillsPayment/GetBillerPackages/{billerId}"),
    "B3": ("bills", "POST", "/api/SharedBillsPayment/PayBillV2"),
    "B4": ("bills", "GET", "/api/Beneficiaries/GetSavedBeneficiaries/{customerId}"),
    "T1": ("transfers", "POST", "/api/InterbankTransfer/VBSendMoneyToOtherBank"),
    "T2": ("transfers", "POST", "/api/IntrabankTransfer/VBSendMoney"),
    "T3": ("transfers", "GET", "/api/Shared/AccountNameEnquiry/{bankCode}/{accountNumber}"),
    "T4": ("transfers", "GET", "/api/Shared/GetAllBanks"),
    "S1": ("savings", "POST", "/api/goal/create"),
    "A1": ("accounts", "GET", "/api/account_maintenance/accounts"),
    "A2": ("accounts", "GET", "/api/account_maintenance/account_details"),
    "A3": ("accounts", "GET", "/api/account_maintenance/transaction_history"),
    "P1": ("bank_prediction", "POST", "/score"),
    "V1": ("voice_storage", "GET", "/api/voicebanking/customersamplevoices/{customerId}"),
}
SAFE_READ_ENDPOINTS = {"A1", "A2", "A3", "D2", "B1", "B2", "B4", "T3", "T4", "V1"}
WRITE_ENDPOINTS = {"D1", "D3", "B3", "T1", "T2", "S1"}


def _base_url(name: str) -> str:
    env_name = f"WEMA_{name.upper()}_BASE_URL"
    value = str(os.getenv(env_name) or DOCUMENTED_BASE_URLS[name]).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError(f"{env_name} must be an HTTPS origin without a path.")
    return value


class LiveBank:
    """Fixed-route HTTP adapter for documented Wema services.

    This adapter owns no authentication workflow and exposes no arbitrary URL/path.
    It currently supports safe reads used by the composite preparation tools.
    """

    mode = "live"

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_urls: dict[str, str] | None = None,
        account_api_key: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds or float(os.getenv("WEMA_BANK_TIMEOUT_SECONDS", "12"))
        self.transport = transport
        self.base_urls = base_urls or {name: _base_url(name) for name in DOCUMENTED_BASE_URLS}
        self.account_api_key = (
            account_api_key if account_api_key is not None
            else str(os.getenv("WEMA_ACCOUNT_MAINTENANCE_API_KEY") or "").strip()
        )

    async def read(self, endpoint: str, **params):
        if endpoint not in SAFE_READ_ENDPOINTS:
            raise WorkflowError("That downstream operation is not enabled for live preparation.")
        return await self._request(endpoint, params=params)

    async def predict_bank(self, account_number: str):
        """Return non-authoritative P1 suggestions; callers must still use T3."""
        return await self._request("P1", body={"account_number": account_number})

    async def submit(self, endpoint: str, body: dict[str, Any]):
        """Fixed write transport for an authorized executor merged from another branch.

        This method is intentionally not exposed by the current FastAPI tool routes.
        It does no identity or authorization work itself.
        """
        if endpoint not in WRITE_ENDPOINTS:
            raise WorkflowError("That downstream operation is not a Wema transaction write.")
        if not isinstance(body, dict) or not body:
            raise WorkflowError("The Wema transaction payload is missing.")
        return await self._request(endpoint, body=body)

    async def _request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ):
        service, method, path_template = ENDPOINTS[endpoint]
        values = dict(params or {})
        path_fields = {
            field for field in ("customerId", "billerId", "bankCode", "accountNumber")
            if "{" + field + "}" in path_template
        }
        for field in path_fields:
            value = str(values.pop(field, "")).strip()
            if not value or len(value) > 128:
                raise WorkflowError(f"Invalid {field} for the Wema request.")
            path_template = path_template.replace("{" + field + "}", quote(value, safe=""))

        headers = {"Accept": "application/json"}
        if service == "accounts":
            if not self.account_api_key:
                raise WorkflowError("Wema account-maintenance API key is not configured.")
            headers["x-api-key"] = self.account_api_key

        try:
            async with httpx.AsyncClient(
                base_url=self.base_urls[service],
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, path_template, params=values or None, json=body, headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise WorkflowError("The Wema service timed out.") from exc
        except httpx.HTTPError as exc:
            raise WorkflowError("The Wema service is unavailable.") from exc

        if response.status_code >= 400:
            raise WorkflowError(f"The Wema service returned HTTP {response.status_code}.")
        if len(response.content) > 2_000_000:
            raise WorkflowError("The Wema service response was unexpectedly large.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WorkflowError("The Wema service returned invalid JSON.") from exc
        return self._unwrap(endpoint, payload)

    @staticmethod
    def _unwrap(endpoint: str, payload):
        if endpoint == "A1":
            if not isinstance(payload, list):
                raise WorkflowError("The Wema account service returned an unexpected response.")
            if not all(isinstance(item, dict) and all(
                field in item for field in ("accountNumber", "currency", "availableBalance")
            ) for item in payload):
                raise WorkflowError("The Wema account service returned incomplete account data.")
            return payload
        if endpoint == "A3":
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise WorkflowError("The Wema account service returned an unexpected response.")
            return payload
        if endpoint == "A2":
            if not isinstance(payload, dict) or not all(
                field in payload for field in ("accountNumber", "currency", "availableBalance")
            ):
                raise WorkflowError("The Wema account service returned an unexpected response.")
            return payload
        if endpoint == "D2":
            value = payload.get("value") if isinstance(payload, dict) else None
            documented_result = value.get("result") if isinstance(value, dict) else None
            # The August PDF shows {isSuccess, value: {result}}, while the current
            # development service returns the result at the top level.
            live_result = payload.get("result") if isinstance(payload, dict) else None
            selected = documented_result if isinstance(documented_result, list) else live_result
            failed = (
                not isinstance(payload, dict)
                or payload.get("hasError") is True
                or (isinstance(value, dict) and value.get("hasError") is True)
                or not isinstance(selected, list)
            )
            if failed:
                raise WorkflowError("Wema could not return data packages.")
            if not all(
                isinstance(group, dict)
                and isinstance(group.get("networkProvider"), str)
                and isinstance(group.get("dataPackages"), list)
                and all(isinstance(package, dict) and all(
                    field in package for field in ("id", "name", "amount")
                ) for package in group["dataPackages"])
                for group in selected
            ):
                raise WorkflowError("Wema returned incomplete data-package details.")
            return selected
        if endpoint in {"T3", "T4"}:
            if (not isinstance(payload, dict) or payload.get("hasError") is True
                    or not isinstance(payload.get("result"), dict if endpoint == "T3" else list)):
                raise WorkflowError("Wema could not return transfer details.")
            selected = payload["result"]
            if endpoint == "T3" and not all(
                field in selected for field in (
                    "bankCode", "accountNumber", "accountName", "currency", "chargeFee"
                )
            ):
                raise WorkflowError("Wema returned incomplete recipient details.")
            if endpoint == "T4" and not all(
                isinstance(bank, dict) and all(
                    field in bank for field in ("bankName", "bankCode")
                ) for bank in selected
            ):
                raise WorkflowError("Wema returned incomplete bank-directory details.")
            return selected
        if endpoint in {"B1", "B2", "B4"}:
            if (not isinstance(payload, dict) or payload.get("hasError") is True
                    or not isinstance(payload.get("data"), list)):
                raise WorkflowError("Wema could not return bill-payment details.")
            return payload["data"]
        if endpoint == "V1":
            if (not isinstance(payload, dict) or payload.get("status") != 200
                    or not isinstance(payload.get("data"), list)):
                raise WorkflowError("Wema could not return voice-storage metadata.")
            return payload["data"]
        if endpoint == "P1":
            if not isinstance(payload, dict) or not isinstance(payload.get("predictions"), list):
                raise WorkflowError("Wema could not return bank predictions.")
            return payload["predictions"]
        if endpoint in {"D1", "D3"}:
            value = payload.get("value") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("isSuccess") is not True
                    or not isinstance(value, dict) or value.get("hasError") is True
                    or not isinstance(value.get("result"), dict)):
                raise WorkflowError("Wema rejected the airtime or data transaction.")
            return payload
        if endpoint == "B3":
            if (not isinstance(payload, dict) or payload.get("hasError") is True
                    or not isinstance(payload.get("data"), dict)):
                raise WorkflowError("Wema rejected the bill payment.")
            return payload
        if endpoint in {"T1", "T2"}:
            if (not isinstance(payload, dict) or payload.get("hasError") is True
                    or not isinstance(payload.get("result"), dict)):
                raise WorkflowError("Wema rejected the transfer submission.")
            return payload
        if endpoint == "S1":
            if (not isinstance(payload, dict) or payload.get("status") != "Success"
                    or not isinstance(payload.get("value"), dict)):
                raise WorkflowError("Wema rejected the savings-goal request.")
            return payload
        raise WorkflowError("Unsupported Wema response contract.")
