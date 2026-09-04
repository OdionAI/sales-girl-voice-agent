from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent.dynamic_tools import invoke_dynamic_http_tool
from wema_tools.api import create_app
from wema_tools.contracts import ToolResult, tool_definitions
from wema_tools.live_bank import DOCUMENTED_BASE_URLS, LiveBank
from wema_tools.service import MockBank, WemaWorkflows, WorkflowError


TOKEN = "unit-test-only-not-a-real-service-secret"
HEADERS = {
    "X-Service-Token": TOKEN,
    "X-Business-Id": "demo-business",
    "X-Agent-Id": "demo-wema-agent",
    "X-Session-Id": "demo-session",
    "X-End-User-Id": "demo-caller",
}
TRANSFER = {"bank": "Wema", "recipient_account": "0000000002", "amount": "2500.00"}
DATA = {"network": "MTN", "package_id": 1, "phone_number": "08000000000"}


class RecordingBank(MockBank):
    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.fail_on = None

    async def read(self, endpoint, **params):
        self.calls.append((endpoint, params))
        if endpoint == self.fail_on:
            raise WorkflowError("Mock bank is unavailable.")
        if endpoint in self.overrides:
            return self.overrides[endpoint]
        return await super().read(endpoint, **params)


class RecordingLiveBank(RecordingBank):
    mode = "live"


class WemaToolsTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.bank = RecordingBank()
        self.workflows = WemaWorkflows(self.bank, clock=lambda: self.now)
        self.client = TestClient(create_app(token=TOKEN, workflows=self.workflows))
        self.addCleanup(self.client.close)

    def invoke(self, tool, payload, headers=None):
        response = self.client.post(f"/v1/tools/{tool}", json=payload, headers=headers or HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        ToolResult.model_validate(value)
        self.assertEqual(value["mode"], "mock")
        return value

    def test_token_is_required_at_startup(self):
        with self.assertRaises(RuntimeError):
            create_app(token="")

    def test_health_explicitly_disables_bank_writes(self):
        self.assertEqual(self.client.get("/health").json(), {
            "status": "ok", "mode": "mock", "bank_writes_enabled": False,
        })

    def test_service_auth_and_metadata_required(self):
        for headers, expected in [({}, 401), ({"X-Service-Token": TOKEN}, 400)]:
            with self.subTest(headers=headers):
                response = self.client.post("/v1/tools/wema_get_balance", json={}, headers=headers)
                self.assertEqual(response.status_code, expected)
        self.assertEqual(self.bank.calls, [])

    def test_unknown_tool_has_no_bank_access(self):
        response = self.client.post("/v1/tools/raw_bank_request", json={}, headers=HEADERS)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.bank.calls, [])

    def test_catalog_matches_dashboard_contract_and_excludes_credentials(self):
        response = self.client.get("/v1/tool-definitions", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        tools = response.json()["tools"]
        self.assertEqual(len(tools), 7)
        self.assertNotIn(TOKEN, response.text)
        for tool in tools:
            self.assertEqual(tool["method"], "POST")
            self.assertFalse(tool["request_schema"]["additionalProperties"])
            self.assertNotIn("customer_id", tool["request_schema"]["properties"])
            self.assertNotIn("fido2CredentialProof", tool["request_schema"]["properties"])

    def test_balance_composes_ownership_then_account_details(self):
        value = self.invoke("wema_get_balance", {})
        self.assertEqual(value["data"]["availableBalance"], "100000.00")
        self.assertEqual([x[0] for x in self.bank.calls], ["A1", "A2"])

    def test_foreign_account_is_rejected_before_details_lookup(self):
        value = self.invoke("wema_get_balance", {"source_account": "9999999999"})
        self.assertEqual(value["status"], "failed")
        self.assertEqual([x[0] for x in self.bank.calls], ["A1"])

    def test_multiple_accounts_require_customer_selection(self):
        self.bank.overrides["A1"] = [
            {"accountNumber": "0000000001", "currency": "NGN"},
            {"accountNumber": "0000000004", "currency": "NGN"},
        ]
        value = self.invoke("wema_get_balance", {})
        self.assertEqual(value["status"], "needs_input")
        self.assertEqual(len(value["data"]["accounts"]), 2)
        self.assertEqual([x[0] for x in self.bank.calls], ["A1"])

    def test_history_retains_documented_query_and_count_bound(self):
        value = self.invoke("wema_get_transactions", {"count": 3})
        self.assertEqual(value["status"], "ok")
        self.assertEqual(self.bank.calls[-1], ("A3", {
            "Skip": 0, "Count": 3, "AccountNumber": "0000000001", "KeyWord": "C",
        }))
        self.assertEqual(self.invoke("wema_get_transactions", {"count": 500})["status"], "failed")

    def test_data_plan_enquiry_filters_without_inventing_packages(self):
        value = self.invoke("wema_list_data_plans", {"network": "mtn"})
        self.assertEqual(len(value["data"]["networks"]), 1)
        unknown = self.invoke("wema_list_data_plans", {"network": "made-up network"})
        self.assertEqual(unknown["data"]["networks"], [])

    def test_bank_directory_can_be_searched_without_an_account_lookup(self):
        value = self.invoke("wema_list_transfer_banks", {"query": "access"})
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["data"]["banks"][0]["bankCode"], "044")
        self.assertEqual([x[0] for x in self.bank.calls], ["T4"])

    def test_data_prepare_uses_catalog_price_and_never_purchases(self):
        value = self.invoke("wema_prepare_data_purchase", DATA)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(value["data"]["preview"]["amount"], "500.00")
        self.assertEqual(value["data"]["preview"]["execution_endpoint_id"], "D3")
        self.assertEqual([x[0] for x in self.bank.calls], ["A1", "D2"])
        self.assertFalse(value["data"]["preview"]["auto_topup"])

    def test_package_cannot_be_used_for_wrong_network(self):
        value = self.invoke("wema_prepare_data_purchase", {**DATA, "network": "Airtel"})
        self.assertEqual(value["status"], "needs_input")
        self.assertEqual(value["data"]["missing_fields"], ["package_id"])
        self.assertIn("saved account and phone number", value["message"])
        self.assertEqual(self.workflows.operations, {})

    def test_package_price_is_not_accepted_as_package_id(self):
        value = self.invoke("wema_prepare_data_purchase", {**DATA, "package_id": 500})
        self.assertEqual(value["status"], "needs_input")
        self.assertEqual(value["data"]["missing_fields"], ["package_id"])
        self.assertEqual(value["data"]["network"], "MTN")
        self.assertEqual(value["data"]["packages"][0]["id"], 1)
        self.assertIn("do not use the package price", value["message"])
        self.assertEqual(self.workflows.operations, {})

    def test_price_customer_and_auth_cannot_be_injected(self):
        for field, value in [("amount", "1"), ("cif", "victim"), ("voice_auth_passed", True)]:
            with self.subTest(field=field):
                response = self.invoke("wema_prepare_data_purchase", {**DATA, field: value})
                self.assertEqual(response["status"], "failed")
        self.assertEqual(self.bank.calls, [])

    def test_transfer_composes_account_bank_and_recipient_checks(self):
        value = self.invoke("wema_prepare_transfer", TRANSFER)
        self.assertEqual(value["status"], "prepared")
        preview = value["data"]["preview"]
        self.assertEqual(preview["recipient_name"], "DEMO WEMA RECIPIENT")
        self.assertEqual(preview["execution_endpoint_id"], "T2")
        self.assertEqual(preview["amount"], "2500.00")
        self.assertFalse(preview["save_beneficiary"])
        self.assertEqual([x[0] for x in self.bank.calls], ["A1", "T4", "T3"])

    def test_other_bank_uses_interbank_route_in_preview(self):
        value = self.invoke("wema_prepare_transfer", {
            **TRANSFER, "bank": "Access", "recipient_account": "0000000003",
        })
        self.assertEqual(value["data"]["preview"]["execution_endpoint_id"], "T1")

    def test_unknown_bank_returns_choices_without_name_enquiry(self):
        value = self.invoke("wema_prepare_transfer", {**TRANSFER, "bank": "Unknown"})
        self.assertEqual(value["status"], "needs_input")
        self.assertEqual([x[0] for x in self.bank.calls], ["A1", "T4"])

    def test_invalid_amounts_and_account_formats_fail_closed(self):
        for amount in ["0", "-1", "NaN", "1.001", "1e3", "200000", 1, True]:
            with self.subTest(amount=amount):
                value = self.invoke("wema_prepare_transfer", {**TRANSFER, "amount": amount})
                self.assertEqual(value["status"], "failed")
        value = self.invoke("wema_prepare_transfer", {**TRANSFER, "recipient_account": "123"})
        self.assertEqual(value["status"], "failed")
        self.assertEqual(self.workflows.operations, {})

    def test_unknown_recipient_or_bank_failure_does_not_prepare(self):
        value = self.invoke("wema_prepare_transfer", {**TRANSFER, "recipient_account": "9999999999"})
        self.assertEqual(value["status"], "failed")
        self.bank.fail_on = "T3"
        self.assertEqual(self.invoke("wema_prepare_transfer", TRANSFER)["status"], "failed")
        self.assertEqual(self.workflows.operations, {})

    def test_bank_name_enquiry_must_match_selected_recipient(self):
        self.bank.overrides["T3"] = {
            "bankCode": "044", "accountNumber": "9999999999",
            "accountName": "WRONG RECIPIENT", "currency": "NGN",
        }
        self.assertEqual(self.invoke("wema_prepare_transfer", TRANSFER)["status"], "failed")

    def test_execute_always_blocks_without_live_authorization(self):
        prepared = self.invoke("wema_prepare_transfer", TRANSFER)
        operation_id = prepared["data"]["operation_id"]
        before = list(self.bank.calls)
        for _ in range(2):
            value = self.invoke("wema_execute_prepared", {"operation_id": operation_id})
            self.assertEqual(value["status"], "blocked")
            self.assertEqual(value["data"]["reason"], "transaction_executor_not_configured")
        self.assertEqual(self.bank.calls, before)

    def test_no_model_confirmation_or_payload_override_can_bypass_block(self):
        operation_id = self.invoke("wema_prepare_transfer", TRANSFER)["data"]["operation_id"]
        value = self.invoke("wema_execute_prepared", {
            "operation_id": operation_id, "confirmed": True, "amount": "1",
            "fido2CredentialProof": "pretend-proof",
        })
        self.assertEqual(value["status"], "failed")
        self.assertNotIn("pretend-proof", str(value))

    def test_operation_is_bound_to_all_context_dimensions(self):
        operation_id = self.invoke("wema_prepare_transfer", TRANSFER)["data"]["operation_id"]
        for key in ("X-Business-Id", "X-Agent-Id", "X-Session-Id", "X-End-User-Id"):
            with self.subTest(key=key):
                value = self.invoke("wema_execute_prepared", {"operation_id": operation_id},
                                    {**HEADERS, key: "different"})
                self.assertEqual(value["status"], "blocked")
                self.assertNotIn("reason", value["data"])

    def test_operation_expires_and_corrections_invalidate_prior_preview(self):
        old = self.invoke("wema_prepare_transfer", TRANSFER)["data"]["operation_id"]
        current = self.invoke("wema_prepare_transfer", {**TRANSFER, "amount": "5000"})["data"]["operation_id"]
        self.assertNotIn(old, self.workflows.operations)
        self.now = 301
        value = self.invoke("wema_execute_prepared", {"operation_id": current})
        self.assertEqual(value["status"], "blocked")
        self.assertEqual(self.workflows.operations, {})

    def test_intent_switch_invalidates_transfer_preview(self):
        operation_id = self.invoke("wema_prepare_transfer", TRANSFER)["data"]["operation_id"]
        self.invoke("wema_prepare_data_purchase", DATA)
        self.assertNotIn(operation_id, self.workflows.operations)

    def test_incomplete_input_and_malformed_json_are_safe_failures(self):
        value = self.invoke("wema_prepare_transfer", {"amount": "50"})
        self.assertEqual(value["status"], "failed")
        response = self.client.post("/v1/tools/wema_prepare_transfer", content="{bad", headers=HEADERS)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(self.bank.calls, [])

    def test_live_mode_requires_server_supplied_customer_id_for_account_reads(self):
        bank = RecordingLiveBank()
        client = TestClient(create_app(token=TOKEN, workflows=WemaWorkflows(bank)))
        self.addCleanup(client.close)
        missing = client.post("/v1/tools/wema_get_balance", json={}, headers=HEADERS).json()
        self.assertEqual(missing["mode"], "live")
        self.assertEqual(missing["status"], "failed")
        self.assertEqual(bank.calls, [])
        response = client.post(
            "/v1/tools/wema_get_balance", json={},
            headers={**HEADERS, "X-Wema-Customer-Id": "sandbox-customer"},
        ).json()
        self.assertEqual(response["status"], "ok")
        self.assertEqual(bank.calls[0], ("A1", {"customerID": "sandbox-customer"}))


class LiveBankAdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, handler, *, account_api_key="test-key"):
        origins = {name: f"https://{name}.example.test" for name in DOCUMENTED_BASE_URLS}
        return LiveBank(
            base_urls=origins, account_api_key=account_api_key,
            transport=httpx.MockTransport(handler),
        )

    async def test_data_plans_uses_documented_route_and_unwraps_envelope(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://airtime_data.example.test/api/Data/GetDataPlans")
            return httpx.Response(200, json={
                "isSuccess": True,
                "value": {"result": [{"networkProvider": "MTN", "dataPackages": []}],
                          "hasError": False},
            })
        value = await self.adapter(handler).read("D2")
        self.assertEqual(value[0]["networkProvider"], "MTN")

    async def test_data_plans_accepts_current_live_top_level_result_shape(self):
        def handler(request):
            return httpx.Response(200, json={
                "result": [{"networkProvider": "Glo", "dataPackages": []}],
                "hasError": False,
                "errorCode": None,
            })
        value = await self.adapter(handler).read("D2")
        self.assertEqual(value[0]["networkProvider"], "Glo")

    async def test_account_routes_add_key_and_encoded_query_parameters(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/accounts"):
                return httpx.Response(200, json=[])
            if request.url.path.endswith("/account_details"):
                return httpx.Response(200, json={
                    "accountNumber": "0000000001", "currency": "NGN",
                    "availableBalance": 1000,
                })
            return httpx.Response(200, json=[])
        bank = self.adapter(handler, account_api_key="secret-test-key")
        await bank.read("A1", customerID="customer one")
        await bank.read("A2", accountNumber="0000000001")
        await bank.read("A3", Skip=0, Count=3, AccountNumber="0000000001", KeyWord="C")
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(r.headers["x-api-key"] == "secret-test-key" for r in requests))
        self.assertEqual(requests[0].url.params["customerID"], "customer one")
        self.assertEqual(requests[2].url.params["Count"], "3")

    async def test_transfer_routes_are_fixed_and_path_values_are_encoded(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith("GetAllBanks"):
                return httpx.Response(200, json={"result": [], "hasError": False})
            return httpx.Response(200, json={
                "result": {
                    "bankCode": "035", "accountNumber": "0000000002",
                    "accountName": "TEST RECIPIENT", "currency": "NGN", "chargeFee": [],
                },
                "hasError": False,
            })
        bank = self.adapter(handler)
        self.assertEqual(await bank.read("T4"), [])
        await bank.read("T3", bankCode="035", accountNumber="0000000002")
        self.assertEqual(requests[1].url.path, "/api/Shared/AccountNameEnquiry/035/0000000002")

    async def test_live_bank_directory_allows_omitted_documented_abbreviation(self):
        def handler(request):
            return httpx.Response(200, json={
                "result": [{"bankName": "Wema Bank", "bankCode": "035", "bankLogo": ""}],
                "hasError": False,
            })
        value = await self.adapter(handler).read("T4")
        self.assertEqual(value[0]["bankCode"], "035")
        self.assertNotIn("abbreviation", value[0])

    async def test_missing_account_key_and_write_endpoint_fail_before_network(self):
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})
        bank = self.adapter(handler, account_api_key="")
        with self.assertRaisesRegex(WorkflowError, "API key"):
            await bank.read("A1", customerID="customer")
        with self.assertRaisesRegex(WorkflowError, "not enabled"):
            await bank.read("D3")
        self.assertEqual(calls, 0)

    async def test_bill_reads_and_bank_prediction_use_fixed_contracts(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/score":
                return httpx.Response(200, json={"predictions": [{"bank": "Wema Bank"}]})
            return httpx.Response(200, json={"data": [], "hasError": False})
        bank = self.adapter(handler)
        self.assertEqual(await bank.read("B1", customerId="sandbox-customer"), [])
        prediction = await bank.predict_bank("0000000002")
        self.assertEqual(prediction[0]["bank"], "Wema Bank")
        self.assertEqual(requests[0].url.path, "/api/SharedBillsPayment/GetBillerCategories/sandbox-customer")
        self.assertEqual(requests[1].method, "POST")
        self.assertEqual(json.loads(requests[1].content), {"account_number": "0000000002"})

    async def test_fixed_write_transport_is_internal_executor_handoff(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"result": {}, "hasError": False})
        bank = self.adapter(handler)
        payload = {"sourceAccountNumber": "0000000001", "amount": 2500}
        result = await bank.submit("T2", payload)
        self.assertFalse(result["hasError"])
        self.assertEqual(requests[0].url.path, "/api/IntrabankTransfer/VBSendMoney")
        self.assertEqual(json.loads(requests[0].content), payload)
        with self.assertRaisesRegex(WorkflowError, "not a Wema transaction"):
            await bank.submit("T4", payload)
        with self.assertRaisesRegex(WorkflowError, "payload is missing"):
            await bank.submit("T2", {})
        self.assertEqual(len(requests), 1)

    async def test_upstream_failures_are_safe_and_do_not_echo_response_body(self):
        cases = [
            (lambda request: httpx.Response(503, text="sensitive upstream detail"), "HTTP 503"),
            (lambda request: httpx.Response(200, text="not json"), "invalid JSON"),
            (lambda request: httpx.Response(200, json={"isSuccess": False}), "could not return"),
            (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)), "timed out"),
        ]
        for handler, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(WorkflowError, message) as raised:
                    await self.adapter(handler).read("D2")
                self.assertNotIn("sensitive upstream detail", str(raised.exception))


class WemaDynamicToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_http_tool_adapter_reaches_composite_and_preserves_blocked_status(self):
        app = create_app(token=TOKEN)
        original_client = httpx.AsyncClient

        def local_client(**kwargs):
            return original_client(transport=httpx.ASGITransport(app=app), **kwargs)

        specs = {t["name"]: t for t in tool_definitions("http://wema.local")}
        for spec in specs.values():
            spec["headers"] = {"X-Service-Token": TOKEN}
        metadata = {"business_id": "demo-business", "agent_id": "demo-agent",
                    "session_id": "demo-session", "end_user_id": "demo-caller"}
        with patch("agent.dynamic_tools.httpx.AsyncClient", side_effect=local_client):
            preview = await invoke_dynamic_http_tool(
                tool=specs["wema_prepare_transfer"], raw_arguments=TRANSFER, metadata=metadata,
            )
            self.assertEqual(preview["status"], "prepared")
            execution = await invoke_dynamic_http_tool(
                tool=specs["wema_execute_prepared"],
                raw_arguments={"operation_id": preview["data"]["operation_id"]}, metadata=metadata,
            )
            self.assertEqual(execution["status"], "blocked")
            self.assertEqual(execution["mode"], "mock")
