from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from livekit import api

from agent import livekit_recording as recording


BUSINESS_ID = "a10c3e32-9834-4ab1-993e-75cd8f95ad67"
STARTED_AT = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)
RELATIVE_PATH = f"livekit-recordings/{BUSINESS_ID}/20260910T123000Z-room-1-session-1.mp3"
LOCAL_PATH = f"/recordings/{RELATIVE_PATH}"
LOCAL_URI = f"local-recording:///{RELATIVE_PATH}"


class RecordingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.multiple(
            recording,
            RECORDING_ENABLED=True,
            RECORDING_STORAGE_PROVIDER="local",
            RECORDING_LOCAL_DIRECTORY="/recordings",
            RECORDING_PREFIX="livekit-recordings",
            RECORDING_FORMAT="mp3",
            RECORDING_BUCKET="",
            RECORDING_GCP_CREDENTIALS="",
            RECORDING_S3_ACCESS_KEY="",
            RECORDING_S3_SECRET_KEY="",
            RECORDING_S3_SESSION_TOKEN="",
            RECORDING_S3_REGION="af-south-1",
            RECORDING_S3_ENDPOINT="",
            RECORDING_S3_FORCE_PATH_STYLE=False,
            RECORDING_PUBLIC_BASE_URL="",
            RECORDING_POLL_TIMEOUT_SECONDS=0.05,
            RECORDING_POLL_INTERVAL_SECONDS=0.001,
            RECORDING_STOP_TIMEOUT_SECONDS=0.01,
        ))
        self.client = SimpleNamespace(
            egress=SimpleNamespace(
                start_room_composite_egress=AsyncMock(return_value=api.EgressInfo(egress_id="EG_test")),
                stop_egress=AsyncMock(return_value=api.EgressInfo(status=api.EGRESS_ENDING)),
                list_egress=AsyncMock(return_value=api.ListEgressResponse()),
            ),
            aclose=AsyncMock(),
        )
        self.api_client = self.enterContext(patch.object(recording, "_api_client", return_value=self.client))

    async def start(self, **kwargs) -> recording.RecordingStartResult:
        return await recording.start_room_recording(**{
            "room_name": "room-1",
            "business_id": BUSINESS_ID,
            "session_id": "session-1",
            "started_at": STARTED_AT,
            **kwargs,
        })

    async def finalize(self, **kwargs) -> recording.RecordingFinalizeResult:
        return await recording.finalize_room_recording(**{
            "egress_id": "EG_test",
            "expected_url": LOCAL_URI,
            "duration_seconds": 99,
            **kwargs,
        })

    def completed(self, **kwargs) -> api.EgressInfo:
        return api.EgressInfo(
            status=api.EGRESS_COMPLETE,
            file_results=[api.FileInfo(**{
                "filename": LOCAL_PATH,
                "location": LOCAL_PATH,
                "duration": 12_900_000_000,
                **kwargs,
            })],
        )

    def list_results(self, *infos: api.EgressInfo) -> None:
        self.client.egress.list_egress.side_effect = [api.ListEgressResponse(items=[info]) for info in infos]


class LocalRecordingTests(RecordingTestCase):
    async def test_local_request_has_absolute_file_and_no_upload_provider(self) -> None:
        with (
            patch.object(recording, "RECORDING_PUBLIC_BASE_URL", "https://public.invalid"),
            patch.object(recording, "_serialize_credentials") as credentials,
        ):
            result = await self.start(business_id=BUSINESS_ID.upper().replace("-", ""))
        self.assertTrue(result.enabled)
        self.assertEqual(result.egress_id, "EG_test")
        self.assertEqual(result.filepath, LOCAL_PATH)
        self.assertEqual(result.expected_url, LOCAL_URI)
        credentials.assert_not_called()
        request = self.client.egress.start_room_composite_egress.await_args.args[0]
        self.assertEqual(request.room_name, "room-1")
        self.assertTrue(request.audio_only)
        self.assertEqual(len(request.file_outputs), 1)
        output = request.file_outputs[0]
        self.assertEqual(output.filepath, LOCAL_PATH)
        self.assertEqual(output.file_type, api.EncodedFileType.MP3)
        self.assertIsNone(output.WhichOneof("output"))
        self.client.aclose.assert_awaited_once()

    async def test_uri_matches_conversation_service_tenant_path_contract(self) -> None:
        result = await self.start()
        parsed = urlsplit(result.expected_url)
        service_root = PurePosixPath("/service-volume")
        service_path = service_root / parsed.path.lstrip("/")
        self.assertEqual(parsed.scheme, "local-recording")
        self.assertFalse(parsed.netloc or parsed.query or parsed.fragment)
        self.assertTrue(service_path.is_relative_to(service_root))
        self.assertEqual(service_path.parent.name, BUSINESS_ID)
        self.assertEqual(service_path.relative_to(service_root).as_posix(), RELATIVE_PATH)

    async def test_disabled_gate_never_contacts_api(self) -> None:
        with patch.object(recording, "RECORDING_ENABLED", False):
            result = await self.start()
        self.assertFalse(result.enabled)
        self.assertEqual(result.detail, "recording_disabled")
        self.api_client.assert_not_called()

    async def test_rejects_unsafe_or_missing_local_directory(self) -> None:
        for directory in ("", "/", "recordings", "./recordings", "../recordings", "//recordings",
                          "/recordings/../etc", "/recordings/./calls", "/recordings//calls",
                          "/recordings\\calls", "/recordings/%2e%2e", "/recordings/{room_name}",
                          "/recordings\x00", "/recordings\n", " /recordings", "file:///recordings"):
            with self.subTest(directory=directory), patch.object(recording, "RECORDING_LOCAL_DIRECTORY", directory):
                self.assertFalse(recording.is_recording_enabled())
                self.assertFalse((await self.start()).enabled)
        self.api_client.assert_not_called()

    async def test_rejects_unsafe_local_prefixes(self) -> None:
        for prefix in ("", "/", "/absolute", "../outside", "calls/../outside", "calls/./today",
                       "calls//today", "calls\\today", "%2e%2e", "{room_name}", "calls?query",
                       "calls#fragment", "calls\x00", ".", ".."):
            with self.subTest(prefix=prefix), patch.object(recording, "RECORDING_PREFIX", prefix):
                self.assertFalse(recording.is_recording_enabled())
                self.assertFalse((await self.start()).enabled)
        self.api_client.assert_not_called()

    async def test_safe_nested_prefix_and_trailing_directory_slash(self) -> None:
        with patch.multiple(recording, RECORDING_PREFIX="private/calls/", RECORDING_LOCAL_DIRECTORY="/mnt/recordings/"):
            result = await self.start()
        relative = RELATIVE_PATH.replace("livekit-recordings", "private/calls")
        self.assertEqual(result.filepath, "/mnt/recordings/" + relative)
        self.assertEqual(result.expected_url, "local-recording:///" + relative)

    async def test_local_format_validation(self) -> None:
        for fmt, file_type in (("mp3", api.MP3), ("mp4", api.MP4), ("ogg", api.OGG)):
            with self.subTest(fmt=fmt), patch.object(recording, "RECORDING_FORMAT", fmt):
                result = await self.start()
                self.assertTrue(result.filepath.endswith("." + fmt))
                self.assertEqual(self.client.egress.start_room_composite_egress.await_args.args[0].file_outputs[0].file_type, file_type)
        for fmt in ("wav", "../mp3", "mp3/../../etc", "mp3?query"):
            with self.subTest(fmt=fmt), patch.object(recording, "RECORDING_FORMAT", fmt):
                self.assertFalse(recording.is_recording_enabled())

    async def test_invalid_business_id_is_not_slugged_into_a_tenant(self) -> None:
        for business_id in ("", "business-1", "../private", None):
            with self.subTest(business_id=business_id):
                result = await self.start(business_id=business_id)
                self.assertFalse(result.enabled)
                self.assertEqual(result.detail, "invalid_local_recording_business_id")
                self.assertIsNone(result.expected_url)
        self.api_client.assert_not_called()

    async def test_room_and_session_cannot_escape_or_template_path(self) -> None:
        result = await self.start(room_name="../{room_name}?/caf\u00e9", session_id="..\\%2f/#\x00")
        self.assertEqual(PurePosixPath(result.filepath).parent.as_posix(), f"/recordings/livekit-recordings/{BUSINESS_ID}")
        self.assertTrue(result.filepath.isascii())
        self.assertEqual(recording._validated_local_uri(result.expected_url), result.expected_url)

    def test_local_url_rejects_outside_root_and_traversal(self) -> None:
        for filepath in (f"/recordings-other/{RELATIVE_PATH}", f"/etc/{RELATIVE_PATH}",
                         f"/recordings/../{RELATIVE_PATH}", f"/recordings/%2e%2e/{RELATIVE_PATH}"):
            with self.subTest(filepath=filepath), self.assertRaises(ValueError):
                recording._public_url_for_path(filepath)

    async def test_start_error_preserves_private_expected_uri(self) -> None:
        self.client.egress.start_room_composite_egress.side_effect = RuntimeError("egress unavailable")
        result = await self.start()
        self.assertTrue(result.enabled)
        self.assertIsNone(result.egress_id)
        self.assertEqual(result.expected_url, LOCAL_URI)
        self.assertEqual(result.detail, "egress unavailable")
        self.client.aclose.assert_awaited_once()

    def test_environment_contract_requires_true_and_explicit_directory(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("LIVEKIT_RECORDING_")}
        env["LIVEKIT_RECORDING_STORAGE_PROVIDER"] = "local"
        for enabled, directory, expected in ((None, "/recordings", False), ("false", "/recordings", False),
                                             ("1", "/recordings", False), ("true", None, False),
                                             ("true", "/recordings", True)):
            case_env = dict(env)
            if enabled is not None:
                case_env["LIVEKIT_RECORDING_ENABLED"] = enabled
            if directory is not None:
                case_env["LIVEKIT_RECORDING_LOCAL_DIRECTORY"] = directory
            with self.subTest(enabled=enabled, directory=directory):
                result = subprocess.run(
                    [sys.executable, "-c", "from agent.livekit_recording import is_recording_enabled; print(is_recording_enabled())"],
                    env=case_env, capture_output=True, text=True, check=True, timeout=10,
                )
                self.assertEqual(result.stdout.strip(), str(expected))


class CloudRecordingRegressionTests(RecordingTestCase):
    async def test_gcs_request_and_path_contract_unchanged(self) -> None:
        credentials = {"type": "service_account", "project_id": "test-project"}
        with patch.multiple(recording, RECORDING_STORAGE_PROVIDER="gcs", RECORDING_BUCKET="bucket",
                            RECORDING_GCP_CREDENTIALS=json.dumps(json.dumps(credentials)), RECORDING_PREFIX="/archive/"):
            result = await self.start(business_id="Business One")
        output = self.client.egress.start_room_composite_egress.await_args.args[0].file_outputs[0]
        self.assertEqual(output.WhichOneof("output"), "gcp")
        self.assertEqual(output.gcp.bucket, "bucket")
        self.assertEqual(json.loads(output.gcp.credentials), credentials)
        self.assertEqual(output.filepath, "archive/business-one/20260910T123000Z-room-1-session-1.mp3")
        self.assertEqual(result.expected_url, "https://storage.googleapis.com/bucket/" + output.filepath)

    async def test_s3_request_and_path_style_url_unchanged(self) -> None:
        with patch.multiple(recording, RECORDING_STORAGE_PROVIDER="s3", RECORDING_BUCKET="bucket",
                            RECORDING_S3_ACCESS_KEY="access", RECORDING_S3_SECRET_KEY="secret",
                            RECORDING_S3_SESSION_TOKEN="token", RECORDING_S3_ENDPOINT="https://s3.example",
                            RECORDING_S3_FORCE_PATH_STYLE=True):
            result = await self.start()
        output = self.client.egress.start_room_composite_egress.await_args.args[0].file_outputs[0]
        self.assertEqual(output.WhichOneof("output"), "s3")
        self.assertEqual(output.filepath, RELATIVE_PATH)
        self.assertEqual(output.s3, api.S3Upload(access_key="access", secret="secret", session_token="token",
                                              region="af-south-1", endpoint="https://s3.example", bucket="bucket", force_path_style=True))
        self.assertEqual(result.expected_url, "https://s3.example/bucket/" + RELATIVE_PATH)

    def test_cloud_gates_still_require_bucket_and_credentials(self) -> None:
        for provider in ("gcs", "s3"):
            with self.subTest(provider=provider), patch.object(recording, "RECORDING_STORAGE_PROVIDER", provider):
                self.assertFalse(recording.is_recording_enabled())
                with patch.object(recording, "RECORDING_BUCKET", "bucket"):
                    self.assertFalse(recording.is_recording_enabled())

    async def test_invalid_gcs_credentials_disable_start(self) -> None:
        with patch.multiple(recording, RECORDING_STORAGE_PROVIDER="gcs", RECORDING_BUCKET="bucket",
                            RECORDING_GCP_CREDENTIALS="not-json-or-a-file"):
            result = await self.start()
        self.assertFalse(result.enabled)
        self.assertEqual(result.detail, "invalid_gcp_credentials")
        self.api_client.assert_not_called()

    def test_cloud_url_normalization_unchanged(self) -> None:
        with patch.multiple(recording, RECORDING_STORAGE_PROVIDER="gcs", RECORDING_BUCKET="bucket"):
            self.assertEqual(recording._normalize_recording_url("gs://bucket/call.mp3", None), "https://storage.googleapis.com/bucket/call.mp3")
            self.assertEqual(recording._normalize_recording_url("https://cdn.example/call.mp3", None), "https://cdn.example/call.mp3")
            self.assertEqual(recording._normalize_recording_url("/unknown", "fallback"), "fallback")
        with patch.multiple(recording, RECORDING_STORAGE_PROVIDER="s3", RECORDING_BUCKET="bucket",
                            RECORDING_S3_ENDPOINT="https://s3.example"):
            self.assertEqual(recording._public_url_for_path("call.mp3"), "https://bucket.s3.example/call.mp3")
            self.assertEqual(recording._normalize_recording_url("s3://bucket/call.mp3", None), "https://bucket.s3.example/call.mp3")
            with patch.object(recording, "RECORDING_PUBLIC_BASE_URL", "https://private-cdn.example"):
                self.assertEqual(recording._public_url_for_path("call.mp3"), "https://private-cdn.example/call.mp3")
                self.assertEqual(recording._normalize_recording_url("s3://bucket/call.mp3", None), "https://private-cdn.example/call.mp3")
            with patch.object(recording, "RECORDING_S3_ENDPOINT", ""):
                self.assertEqual(recording._normalize_recording_url("s3://bucket/call.mp3", None), "s3://bucket/call.mp3")


class FinalizeRecordingTests(RecordingTestCase):
    async def test_waits_for_complete_despite_early_location(self) -> None:
        infos = [api.EgressInfo(status=status, file_results=[api.FileInfo(location=LOCAL_PATH)])
                 for status in (api.EGRESS_STARTING, api.EGRESS_ACTIVE, api.EGRESS_ENDING)]
        self.list_results(*infos, self.completed())
        result = await self.finalize()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.recording_url, LOCAL_URI)
        self.assertEqual(result.duration_seconds, 12)
        self.assertEqual(self.client.egress.list_egress.await_count, 4)
        self.client.egress.stop_egress.assert_awaited_once_with(api.StopEgressRequest(egress_id="EG_test"))
        self.client.aclose.assert_awaited_once()

    async def test_stop_response_can_confirm_completion(self) -> None:
        self.client.egress.stop_egress.return_value = self.completed()
        result = await self.finalize()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.duration_seconds, 12)
        self.client.egress.list_egress.assert_not_awaited()

    async def test_local_filename_without_location_is_file_output(self) -> None:
        self.list_results(self.completed(location=""))
        result = await self.finalize()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.recording_url, LOCAL_URI)

    async def test_legacy_file_result_and_duration_are_supported(self) -> None:
        self.list_results(api.EgressInfo(status=api.EGRESS_COMPLETE,
                                        file=api.FileInfo(filename=LOCAL_PATH, duration=3_000_000_000)))
        result = await self.finalize()
        self.assertEqual(result.status, "available")
        self.assertEqual(result.duration_seconds, 3)

    async def test_terminal_failure_wins_over_any_file_location(self) -> None:
        for status in (api.EGRESS_FAILED, api.EGRESS_ABORTED, api.EGRESS_LIMIT_REACHED):
            with self.subTest(status=status):
                info = self.completed()
                info.status = status
                info.error = "egress failure"
                self.list_results(info)
                result = await self.finalize()
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.detail, "egress failure")
                self.assertIsNone(result.recording_url)
                self.assertEqual(result.duration_seconds, 99)

    async def test_terminal_stop_response_with_no_error_has_status_detail(self) -> None:
        self.client.egress.stop_egress.return_value = api.EgressInfo(status=api.EGRESS_ABORTED)
        result = await self.finalize()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.detail, f"egress_status_{api.EGRESS_ABORTED}")

    async def test_complete_without_populated_file_remains_processing(self) -> None:
        for info in (api.EgressInfo(status=api.EGRESS_COMPLETE),
                     api.EgressInfo(status=api.EGRESS_COMPLETE, file_results=[api.FileInfo()]),
                     api.EgressInfo(status=api.EGRESS_COMPLETE, file=api.FileInfo()),
                     api.EgressInfo(status=api.EGRESS_COMPLETE, file=api.FileInfo(location=" ", filename=" "))):
            with self.subTest(info=info):
                self.client.egress.list_egress.return_value = api.ListEgressResponse(items=[info])
                result = await self.finalize(timeout_seconds=0.01)
                self.assertEqual(result.status, "processing")
                self.assertIsNone(result.recording_url)

    async def test_active_location_remains_processing_on_timeout(self) -> None:
        info = self.completed()
        info.status = api.EGRESS_ACTIVE
        self.client.egress.list_egress.return_value = api.ListEgressResponse(items=[info])
        result = await self.finalize(timeout_seconds=0.01)
        self.assertEqual(result.status, "processing")
        self.assertEqual(result.detail, "egress_completion_timeout")
        self.assertIsNone(result.recording_url)
        self.assertEqual(result.duration_seconds, 99)

    async def test_missing_egress_id_is_unavailable_without_api(self) -> None:
        result = await self.finalize(egress_id=None)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.detail, "missing_egress_id")
        self.api_client.assert_not_called()

    async def test_missing_duration_uses_caller_fallback(self) -> None:
        self.list_results(self.completed(duration=0))
        self.assertEqual((await self.finalize()).duration_seconds, 99)

    def test_completed_location_requires_complete_and_supports_legacy_file(self) -> None:
        info = self.completed()
        self.assertEqual(recording._extract_completed_location(info), LOCAL_PATH)
        info.status = api.EGRESS_ACTIVE
        self.assertIsNone(recording._extract_completed_location(info))
        legacy = api.EgressInfo(status=api.EGRESS_COMPLETE, file=api.FileInfo(location=LOCAL_PATH))
        self.assertEqual(recording._extract_completed_location(legacy), LOCAL_PATH)

    def test_invalid_duration_and_subsecond_duration(self) -> None:
        for value in (None, 0, -1, "bad", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(recording._file_duration_seconds(SimpleNamespace(duration=value), 99), 99)
        self.assertEqual(recording._file_duration_seconds(api.FileInfo(duration=900_000_000), 99), 0)

    async def test_cloud_completion_normalizes_location_and_duration(self) -> None:
        with patch.object(recording, "RECORDING_STORAGE_PROVIDER", "gcs"):
            self.list_results(self.completed(location="gs://bucket/call.mp3"))
            result = await self.finalize(expected_url="https://storage.googleapis.com/bucket/call.mp3")
        self.assertEqual(result.status, "available")
        self.assertEqual(result.recording_url, "https://storage.googleapis.com/bucket/call.mp3")
        self.assertEqual(result.duration_seconds, 12)

    def test_local_normalization_never_returns_a_public_or_filesystem_url(self) -> None:
        for location in (LOCAL_PATH, "file:///recordings/call.mp3", "https://public.invalid/call.mp3", "gs://bucket/call.mp3", ""):
            with self.subTest(location=location):
                self.assertEqual(recording._normalize_recording_url(location, LOCAL_URI), LOCAL_URI)

    async def test_unsafe_local_expected_uri_cannot_become_available(self) -> None:
        invalid_uris = (None, "https://public.invalid/call.mp3", "local-recording://host/call.mp3",
                        LOCAL_URI.replace(BUSINESS_ID, BUSINESS_ID.upper()),
                        LOCAL_URI.replace(BUSINESS_ID, "not-a-uuid"),
                        LOCAL_URI.replace("livekit-recordings", "../livekit-recordings"),
                        LOCAL_URI.replace("livekit-recordings", "%2e%2e/livekit-recordings"),
                        LOCAL_URI + "?query", LOCAL_URI + "#fragment", LOCAL_URI + "\x00")
        self.client.egress.list_egress.return_value = api.ListEgressResponse(items=[self.completed()])
        for uri in invalid_uris:
            with self.subTest(uri=uri):
                result = await self.finalize(expected_url=uri, timeout_seconds=0.01)
                self.assertEqual(result.status, "processing")
                self.assertIsNone(result.recording_url)

    async def test_skip_stop_supports_deferred_polling(self) -> None:
        self.list_results(self.completed())
        result = await self.finalize(request_stop=False)
        self.assertEqual(result.status, "available")
        self.client.egress.stop_egress.assert_not_awaited()

    async def test_stop_failure_does_not_prevent_polling(self) -> None:
        self.client.egress.stop_egress.side_effect = RuntimeError("already stopped")
        self.list_results(self.completed())
        self.assertEqual((await self.finalize()).status, "available")

    async def test_hung_stop_has_its_own_bound_then_polls(self) -> None:
        async def hang(_request):
            await asyncio.Event().wait()
        self.client.egress.stop_egress.side_effect = hang
        self.list_results(self.completed())
        result = await asyncio.wait_for(self.finalize(timeout_seconds=0.2), timeout=1)
        self.assertEqual(result.status, "available")

    async def test_overall_timeout_bounds_stop_and_list_calls(self) -> None:
        async def hang(_request):
            await asyncio.Event().wait()
        for operation in ("stop_egress", "list_egress"):
            with self.subTest(operation=operation):
                mock = getattr(self.client.egress, operation)
                mock.side_effect = hang
                result = await asyncio.wait_for(self.finalize(timeout_seconds=0.005), timeout=1)
                self.assertEqual(result.status, "processing")
                self.assertEqual(result.detail, "egress_completion_timeout")
                mock.side_effect = None

    async def test_lookup_failure_is_processing_not_confirmed_egress_failure(self) -> None:
        self.client.egress.list_egress.side_effect = RuntimeError("network down")
        result = await self.finalize()
        self.assertEqual(result.status, "processing")
        self.assertEqual(result.detail, "network down")
        self.client.aclose.assert_awaited_once()

    async def test_caller_cancellation_propagates_and_closes_client(self) -> None:
        self.client.egress.stop_egress.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.finalize()
        self.client.aclose.assert_awaited_once()


class StopRecordingTests(RecordingTestCase):
    async def test_stop_acknowledgement_does_not_poll_or_claim_readiness(self) -> None:
        result = await recording.request_stop_room_recording(egress_id="EG_test")
        self.assertIs(result, True)
        self.client.egress.stop_egress.assert_awaited_once_with(api.StopEgressRequest(egress_id="EG_test"))
        self.client.egress.list_egress.assert_not_awaited()
        self.client.aclose.assert_awaited_once()

    async def test_missing_id_does_not_create_client(self) -> None:
        self.assertFalse(await recording.request_stop_room_recording(egress_id=None))
        self.api_client.assert_not_called()

    async def test_api_failure_is_caller_safe(self) -> None:
        self.client.egress.stop_egress.side_effect = RuntimeError("already stopped")
        self.assertFalse(await recording.request_stop_room_recording(egress_id="EG_test"))
        self.client.aclose.assert_awaited_once()

    async def test_client_initialization_failure_is_caller_safe(self) -> None:
        self.api_client.side_effect = RuntimeError("missing LiveKit credentials")
        self.assertFalse(await recording.request_stop_room_recording(egress_id="EG_test"))

    async def test_timeout_cancels_stop_and_closes_client(self) -> None:
        cancelled = asyncio.Event()
        async def hang(_request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.client.egress.stop_egress.side_effect = hang
        result = await asyncio.wait_for(recording.request_stop_room_recording(egress_id="EG_test", timeout_seconds=0.005), timeout=1)
        self.assertFalse(result)
        self.assertTrue(cancelled.is_set())
        self.client.aclose.assert_awaited_once()

    async def test_close_failure_does_not_mask_stop_acknowledgement(self) -> None:
        self.client.aclose.side_effect = RuntimeError("close failed")
        self.assertTrue(await recording.request_stop_room_recording(egress_id="EG_test"))

    async def test_hung_close_is_bounded(self) -> None:
        async def hang():
            await asyncio.Event().wait()
        self.client.aclose.side_effect = hang
        self.assertTrue(await asyncio.wait_for(recording.request_stop_room_recording(egress_id="EG_test"), timeout=2))

    async def test_caller_cancellation_is_not_swallowed(self) -> None:
        self.client.egress.stop_egress.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await recording.request_stop_room_recording(egress_id="EG_test")
        self.client.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
