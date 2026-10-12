import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from latency_lab import directory_email as m


class Response:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs): return {"data": [{"code": "EM_104"}], "request_id": "mail-test"}


class HTTP:
    def __init__(self): self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


def source(address="Fresh Road", count=1):
    return {"status": "ok", "data": {"source": "live_google_sheet", "network": "PROVIDER",
        "filters": {"intent": "lookup", "network": "PROVIDER", "plan": "Plus", "town": "Ikeja"},
        "total_matches": count, "read_completed_at": "2026-10-09T13:00:00Z",
        "matches": [{"provider": "Example Hospital", "address": address, "plan": "PLUS & HIGHER"}]}}


class EmailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"contacts.sqlite3"
        m.save_contact(self.path, "biz", "2348112812216", "caller@example.test")
        self.context = SimpleNamespace(business_id="biz", agent_id="agent", tools=[
            {"name": m.provider_directory.TOOL_NAME, "url": "builtin://provider-directory"},
            {"name": m.TOOL_NAME, "url": m.TOOL_URL}])
        self.identity = {"entry_surface": "aicc_inbound", "sip_trunk_id": "trunk", "sip_caller_verified": True,
            "business_id": "biz", "config_agent_id": "agent", "caller_phone": "08112812216"}
        self.mail = m.DirectoryEmail(self.context, self.identity, "session-1", self.path)
        self.mail.remember(source("Old Road"))
        self.env = patch.dict(m.os.environ, {"ZEPTO_API_KEY": "test-key", "ZEPTO_FROM_ADDRESS": "notifications@example.test"})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_known_verified_caller_is_named_in_the_proactive_opening(self):
        from latency_lab.pipeline import ConversationPipeline

        fake = SimpleNamespace(
            agent_context=SimpleNamespace(
                instructions="SPOKEN_OPENING: Hello, I'm Amaka calling from Leadway Health.",
                name="Amaka",
            ),
            config=SimpleNamespace(opening_greeting_text="", tts_language="en"),
            directory_email=SimpleNamespace(caller_name=lambda: "Woron"),
        )
        self.assertEqual(
            ConversationPipeline.opening_greeting_text(fake),
            "Hello Woron, I'm Amaka calling from Leadway Health.",
        )

    def test_number_formats_and_invalid_inputs(self):
        for raw in ["08112812216", "2348112812216", "+2348112812216", "002348112812216", "+234 811 281 2216"]:
            self.assertEqual(m.normalize_phone(raw), "+2348112812216")
        for raw in ["anonymous", "sip_08112812216", "caller@example.test", "123", "number 08112812216"]:
            self.assertEqual(m.normalize_phone(raw), "")

    def test_web_unmapped_and_cross_business_rejected(self):
        self.assertEqual(self.mail.recipient()[1], "caller@example.test")
        self.assertEqual(self.mail.caller_name(), "")
        for change in [{"entry_surface": "public_web"}, {"sip_caller_verified": False},
                       {"business_id": "other"}, {"config_agent_id": "other"}, {"caller_phone": "08000000000"}]:
            mail = m.DirectoryEmail(self.context, {**self.identity, **change}, "x", self.path)
            self.assertIsNone(mail.recipient())
            self.assertEqual(mail.caller_name(), "")

    def test_verified_caller_profile_can_be_named_without_saved_email(self):
        m.save_contact(self.path, "biz", "+2349040760651", full_name="Ife")
        identity = {**self.identity, "caller_phone": "+2349040760651"}
        mail = m.DirectoryEmail(self.context, identity, "x", self.path)
        self.assertEqual(mail.caller_name(), "Ife")
        self.assertIsNone(mail.recipient())

    def test_name_only_contact_update_preserves_existing_email(self):
        m.save_contact(self.path, "biz", "08112812216", "caller@example.test", full_name="Woron")
        m.save_contact(self.path, "biz", "08112812216", full_name="Woron A.")
        with m.connect(self.path) as db:
            row = db.execute("SELECT email,full_name FROM caller_contacts WHERE phone_e164=?",
                             ("+2348112812216",)).fetchone()
        self.assertEqual(tuple(row), ("caller@example.test", "Woron A."))

    async def test_no_send_without_preparation_or_reassigned_recipient(self):
        http = HTTP()
        self.assertEqual((await self.mail.send(http))["status"], "failed")
        self.assertEqual(self.mail.prepare({"to_email": "attacker@example.test"})["status"], "failed")
        self.mail.prepare({})
        m.save_contact(self.path, "biz", "08112812216", "new@example.test")
        self.assertEqual((await self.mail.send(http))["status"], "failed")
        self.assertEqual(http.calls, [])

    async def test_live_refresh_exact_recipient_and_duplicate_protection(self):
        http = HTTP()
        self.assertEqual(self.mail.prepare({})["status"], "ready")
        self.assertEqual(http.calls, [])
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=source())) as read:
            out = await self.mail.send(http)
            self.assertEqual(out["status"], "success")
            read.assert_awaited_once_with(http, self.mail.filters, limit=7)
            payload = http.calls[0][1]["json"]
            self.assertEqual(payload["to"], [{"email_address": {"address": "caller@example.test"}}])
            self.assertIn("Fresh Road", payload["textbody"])
            self.assertNotIn("Old Road", payload["textbody"])
            self.mail.prepare({})
            self.assertTrue((await self.mail.send(http))["data"]["deduplicated"])
            self.assertEqual(len(http.calls), 1)

    async def test_alternate_email_is_read_back_and_requires_confirmation(self):
        http = HTTP()
        self.mail.filters = None
        self.assertEqual(self.mail.prepare({"recipient_email": "other@example.test"})["status"], "needs_input")
        self.mail.remember(source())
        prepared = self.mail.prepare({"recipient_email": "other@example.test"})
        self.assertEqual(prepared["status"], "needs_confirmation")
        self.assertIn("other at example dot test", prepared["message"])
        self.assertEqual((await self.mail.send(http))["status"], "failed")
        self.assertEqual(http.calls, [])
        self.mail.prepare({"recipient_email": "other@example.test"})
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=source())):
            sent = await self.mail.send(http, confirmed_alternate=True)
        self.assertEqual(sent["status"], "success")
        self.assertEqual(http.calls[0][1]["json"]["to"], [{"email_address": {"address": "other@example.test"}}])

    async def test_alternate_email_can_be_used_when_contact_has_no_default(self):
        m.save_contact(self.path, "biz", "08112812216", full_name="Woron")
        self.mail.remember(source())
        prepared = self.mail.prepare({"recipient_email": "other@example.test"})
        self.assertEqual(prepared["status"], "needs_confirmation")
        self.assertEqual(self.mail.prepared["full_name"], "Woron")

    async def test_saved_email_default_needs_no_alternate_confirmation(self):
        http = HTTP()
        self.mail.remember(source())
        self.assertEqual(self.mail.prepare({})["status"], "ready")
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=source())):
            sent = await self.mail.send(http)
        self.assertEqual(sent["status"], "success")
        self.assertEqual(http.calls[0][1]["json"]["to"], [{"email_address": {"address": "caller@example.test"}}])

    async def test_source_failure_and_empty_results_prevent_send(self):
        http = HTTP()
        for body in [{"status": "failed", "data": {}}, {"status": "ok", "data": {"matches": []}}]:
            self.mail.prepare({})
            with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=body)):
                self.assertNotEqual((await self.mail.send(http))["status"], "success")
        self.assertEqual(http.calls, [])

    async def test_email_is_capped_at_seven_ranked_results(self):
        http = HTTP()
        self.mail.prepare({})
        body = source(count=75)
        body["data"]["matches"] = [
            {"provider": f"Clinic {index}", "address": f"Street {index}", "town": "Ikeja", "plan": "PLUS & HIGHER"}
            for index in range(1, 16)
        ]
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=body)) as read:
            result = await self.mail.send(http)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["count"], 7)
        read.assert_awaited_once_with(http, self.mail.filters, limit=7)
        payload = http.calls[0][1]["json"]
        self.assertEqual(payload["textbody"].count("Clinic "), 7)
        with m.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT result_count FROM directory_email_outbox").fetchone()[0], 7)

    async def test_uncertain_submission_is_not_retried(self):
        http = HTTP()
        def timeout(*args, **kwargs): raise TimeoutError()
        http.post = timeout
        self.mail.prepare({})
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=source())):
            self.assertEqual((await self.mail.send(http))["status"], "unknown")
            self.mail.prepare({})
            http = HTTP()
            self.assertEqual((await self.mail.send(http))["status"], "unknown")
            self.assertEqual(http.calls, [])

    def test_latest_failed_search_invalidates_selection(self):
        self.mail.remember({"status": "failed", "data": {}})
        self.assertEqual(self.mail.prepare({})["status"], "needs_input")

    async def test_named_contact_receives_personal_welcoming_email(self):
        m.save_contact(self.path, "biz", "08112812216", "caller@example.test", full_name="Gideon Anumba")
        self.mail.prepare({});http=HTTP()
        with patch.object(m.provider_directory, "lookup", AsyncMock(return_value=source())):
            result=await self.mail.send(http)
        self.assertEqual(result["status"],"success")
        payload=http.calls[0][1]["json"]
        self.assertIn("Hello Gideon Anumba,",payload["textbody"])
        self.assertIn("pleasure helping you",payload["textbody"])
        self.assertIn("Hello Gideon Anumba,",payload["htmlbody"])
        m.save_contact(self.path,"biz","08112812216","caller@example.test")
        with m.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT full_name FROM caller_contacts").fetchone()[0],"Gideon Anumba")

    def test_old_contacts_database_migrates_without_losing_mapping(self):
        path=Path(self.tmp.name)/"legacy.sqlite3"
        with m.connect(path) as db:
            db.execute("CREATE TABLE caller_contacts (business_id TEXT,phone_e164 TEXT,email TEXT,active INTEGER DEFAULT 1,updated_at TEXT,PRIMARY KEY(business_id,phone_e164))")
            db.execute("INSERT INTO caller_contacts(business_id,phone_e164,email) VALUES ('biz','+2348000000001','old@example.test')")
        m.initialize(path)
        with m.connect(path) as db:
            row=db.execute("SELECT email,full_name FROM caller_contacts").fetchone()
        self.assertEqual(tuple(row),("old@example.test",""))

    def test_plain_and_html_safe_content_and_spoken_symbols(self):
        data = source("<script>bad</script>")["data"]
        plain, markup = m.render_email(data, full_name="<b>Gideon</b>")
        self.assertNotIn("<b>Gideon</b>", markup)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertEqual(m.speech_text("Visit odion.ai or use *123#. A & B."), "Visit odion dot ai or use  star 123 hash . A  and  B.")


if __name__ == "__main__": unittest.main()
