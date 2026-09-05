from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from livekit import api
from agent import livekit_recording as recording


class LocalRecordingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.patch = patch.multiple(
            recording,
            RECORDING_ENABLED=True,
            RECORDING_STORAGE_PROVIDER="local",
            RECORDING_LOCAL_OUTPUT_DIR="/out",
            RECORDING_BUCKET="",
            RECORDING_GCP_CREDENTIALS="",
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_local_requires_explicit_absolute_output_directory(self):
        self.assertTrue(recording.is_recording_enabled())
        for directory in ("", "relative"):
            with patch.object(recording, "RECORDING_LOCAL_OUTPUT_DIR", directory):
                self.assertFalse(recording.is_recording_enabled())
        with patch.object(recording, "RECORDING_ENABLED", False):
            self.assertFalse(recording.is_recording_enabled())
        with patch.object(recording, "RECORDING_STORAGE_PROVIDER", "gcs"):
            self.assertFalse(recording.is_recording_enabled())

    async def test_local_egress_uses_mount_without_cloud_credentials(self):
        client = SimpleNamespace(
            egress=SimpleNamespace(start_room_composite_egress=AsyncMock(
                return_value=SimpleNamespace(egress_id="EG_test")
            )),
            aclose=AsyncMock(),
        )
        with patch.object(recording, "_api_client", return_value=client):
            result = await recording.start_room_recording(
                room_name="room-" + "x" * 300,
                business_id="business-1",
                session_id="session-1",
                started_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            )
        request = client.egress.start_room_composite_egress.await_args.args[0]
        output = request.file_outputs[0]
        self.assertTrue(request.audio_only)
        self.assertTrue(output.filepath.startswith("/out/livekit-recordings/business-1/"))
        self.assertLess(len(output.filepath.rsplit("/", 1)[1]), 255)
        self.assertFalse(output.HasField("gcp"))
        self.assertFalse(output.HasField("s3"))
        self.assertEqual(result.expected_url, "local-recording:///" + result.filepath)
        self.assertEqual(result.egress_id, "EG_test")
        client.aclose.assert_awaited_once()

    async def test_local_finalize_waits_until_file_is_complete(self):
        def item(status):
            return SimpleNamespace(items=[SimpleNamespace(
                status=status, file_results=[SimpleNamespace(location="/out/test.mp3")]
            )])
        client = SimpleNamespace(egress=SimpleNamespace(
            stop_egress=AsyncMock(),
            list_egress=AsyncMock(side_effect=[
                item(api.EgressStatus.EGRESS_ENDING), item(api.EgressStatus.EGRESS_COMPLETE)
            ]),
        ), aclose=AsyncMock())
        with patch.object(recording, "_api_client", return_value=client), patch.object(recording.asyncio, "sleep", AsyncMock()):
            result = await recording.finalize_room_recording(
                egress_id="EG_test", expected_url="local-recording:///test.mp3", duration_seconds=3
            )
        self.assertEqual(result.status, "available")
        self.assertEqual(result.recording_url, "local-recording:///test.mp3")
        self.assertEqual(client.egress.list_egress.await_count, 2)

    async def test_existing_cloud_upload_providers_are_unchanged(self):
        for provider in ("gcs", "s3"):
            with self.subTest(provider=provider), patch.multiple(
                recording, RECORDING_STORAGE_PROVIDER=provider, RECORDING_BUCKET="test-bucket",
                RECORDING_GCP_CREDENTIALS='{"type":"service_account"}',
                RECORDING_S3_ACCESS_KEY="test-access", RECORDING_S3_SECRET_KEY="test-secret",
                RECORDING_S3_ENDPOINT="https://storage.example.test",
            ):
                client = SimpleNamespace(
                    egress=SimpleNamespace(start_room_composite_egress=AsyncMock(
                        return_value=SimpleNamespace(egress_id="EG_cloud")
                    )), aclose=AsyncMock(),
                )
                with patch.object(recording, "_api_client", return_value=client):
                    result = await recording.start_room_recording(
                        room_name="test-room", business_id="business-1", session_id="session-1",
                        started_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
                    )
                output = client.egress.start_room_composite_egress.await_args.args[0].file_outputs[0]
                self.assertTrue(output.HasField("s3" if provider == "s3" else "gcp"))
                self.assertFalse(output.filepath.startswith("/out/"))
                self.assertIn("test-room-session-1", output.filepath)
                self.assertTrue(result.expected_url.startswith("https://"))
