import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, AsyncMock

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from latency_lab.passkey_demo import DemoSession, b64, enabled, requested

RP = 'docs.odion.ai'
ORIGIN = 'https://' + RP


class Device:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = secrets.token_bytes(32)
        self.user = None
        self.counter = 0

    def proof(self, packet, *, uv=True, origin=ORIGIN, challenge=None):
        options = packet['options']
        mode = packet['mode']
        client = json.dumps({'type': 'webauthn.create' if mode == 'register' else 'webauthn.get',
                             'origin': origin, 'challenge': challenge or options['challenge']}).encode()
        flags = 1 | (4 if uv else 0)
        if mode == 'register':
            self.user = options['user']['id']
            numbers = self.key.public_key().public_numbers()
            cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')}
            auth = hashlib.sha256(RP.encode()).digest() + bytes([flags | 64]) + bytes(4)
            auth += bytes(16) + len(self.id).to_bytes(2, 'big') + self.id + cbor2.dumps(cose)
            response = {'clientDataJSON': b64(client), 'attestationObject': b64(cbor2.dumps(
                {'fmt': 'none', 'attStmt': {}, 'authData': auth}))}
        else:
            self.counter += 1
            auth = hashlib.sha256(RP.encode()).digest() + bytes([flags]) + self.counter.to_bytes(4, 'big')
            signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
            response = {'clientDataJSON': b64(client), 'authenticatorData': b64(auth),
                        'signature': b64(signature), 'userHandle': self.user}
        return {'id': b64(self.id), 'rawId': b64(self.id), 'type': 'public-key', 'response': response}


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = asyncio.Queue()
        self.progress = AsyncMock()
        async def send(packet): await self.sent.put(packet)
        self.demo = DemoSession(send, rp_id=RP, origin=ORIGIN, timeout=2)
        self.device = Device()
        self.tasks = []
        await self.receive({'type': 'session_context', 'correlationId': 'context'})

    async def asyncTearDown(self):
        await self.demo.close()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def receive(self, value):
        await self.demo.receive(json.dumps(value).encode())

    def start(self):
        task = asyncio.create_task(self.demo.run(self.progress)); self.tasks.append(task)
        return task

    async def packet(self): return await asyncio.wait_for(self.sent.get(), 1)

    async def prove(self, packet, **kwargs):
        value = {'type': 'fido2_proof', 'authenticationId': packet['authenticationId'],
                 'correlationId': packet['correlationId'], 'proof': self.device.proof(packet, **kwargs)}
        await self.receive(value)
        return value, await self.packet()

    async def enroll(self):
        task = self.start()
        packet = await self.packet()
        self.assertEqual(packet['mode'], 'register')
        _, result = await self.prove(packet)
        self.assertIs(result['accepted'], True)
        return task, await self.packet()

    async def test_real_registration_and_signature_verification(self):
        task, authentication = await self.enroll()
        self.assertEqual(authentication['mode'], 'authenticate')
        _, result = await self.prove(authentication)
        self.assertIs(result['accepted'], True)
        self.assertIn('No bank transaction was authorized', await task)
        self.assertEqual(self.demo.credential.count, 1)
        self.assertEqual(self.progress.await_count, 2)

    async def test_wrong_origin_and_missing_user_verification_rejected(self):
        for kwargs in [{'origin': 'https://evil.example'}, {'uv': False}, {'challenge': b64(b'wrong')}]:
            task = self.start(); packet = await self.packet()
            _, result = await self.prove(packet, **kwargs)
            self.assertIs(result['accepted'], False)
            self.assertIn('did not pass', await task)
            self.assertIsNone(self.demo.credential)

    async def test_cancel_does_not_send_acceptance(self):
        task = self.start(); packet = await self.packet()
        await self.receive({'type': 'fido2_cancelled', 'authenticationId': packet['authenticationId'],
                            'correlationId': packet['correlationId']})
        self.assertIn('cancelled', await task)
        self.assertTrue(self.sent.empty())
        self.assertIsNone(self.demo.credential)

    async def test_wrong_ids_and_replayed_proof_cannot_authorize(self):
        task, packet = await self.enroll()
        proof = self.device.proof(packet)
        value = {'type': 'fido2_proof', 'authenticationId': 'different',
                 'correlationId': packet['correlationId'], 'proof': proof}
        await self.receive(value)
        self.assertFalse(task.done()); self.assertTrue(self.sent.empty())
        value['authenticationId'] = packet['authenticationId']
        await self.receive(value)
        self.assertIs((await self.packet())['accepted'], True)
        await task
        await self.receive(value)
        self.assertTrue(self.sent.empty())
        task = self.start(); newer = await self.packet()
        value.update(authenticationId=newer['authenticationId'], correlationId=newer['correlationId'])
        await self.receive(value)
        self.assertIs((await self.packet())['accepted'], False)
        self.assertIn('did not pass', await task)

    async def test_expired_proof_is_rejected(self):
        task = self.start(); packet = await self.packet()
        self.demo.pending.expires = 0
        _, result = await self.prove(packet)
        self.assertIs(result['accepted'], False)
        self.assertIn('did not pass', await task)

    async def test_timeout_and_task_cancellation_abort_native_prompt(self):
        self.demo.timeout = 0.02
        task = self.start(); await self.packet()
        self.assertIn('expired', await task)
        self.assertEqual((await self.packet())['type'], 'fido2_abort')
        self.demo.timeout = 2
        task = self.start(); await self.packet(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual((await self.packet())['type'], 'fido2_abort')
        self.assertIsNone(self.demo.pending)

    async def test_context_reset_and_close_invalidate_pending_request(self):
        task = self.start(); packet = await self.packet()
        await self.receive({'type': 'session_context', 'correlationId': 'new-context'})
        await self.prove_old_without_result(packet)
        self.assertIn('did not pass', await task)
        task = self.start(); await self.packet(); await self.demo.close()
        self.assertIn('did not pass', await task)
        self.assertIsNone(self.demo.credential)

    async def prove_old_without_result(self, packet):
        await self.receive({'type': 'fido2_proof', 'authenticationId': packet['authenticationId'],
                            'correlationId': packet['correlationId'], 'proof': self.device.proof(packet)})
        self.assertTrue(self.sent.empty())

    async def test_context_required_and_duplicate_run_does_not_open_sheet(self):
        self.demo.context_id = None
        self.assertIn('not available', await self.demo.run(self.progress)); self.assertTrue(self.sent.empty())
        await self.receive({'type': 'session_context', 'correlationId': 'context'})
        task = self.start(); await self.packet()
        self.assertIn('already waiting', await self.demo.run(self.progress)); self.assertTrue(self.sent.empty())

    async def test_malformed_context_and_bounded_prompts(self):
        self.demo.context_id = None
        for value in [[], {'type': 'session_context', 'correlationId': True},
                      {'type': 'session_context', 'correlationId': 'x', 'version': True}]:
            await self.receive(value)
        await self.demo.receive(b'{' * 14001)
        self.assertIsNone(self.demo.context_id)
        self.demo.context_id = 'x'; self.demo.requests = 6
        self.assertIn('did not pass', await self.demo.run(self.progress)); self.assertTrue(self.sent.empty())


class GateTests(unittest.TestCase):
    def test_explicit_intent_and_operator_scope(self):
        for text in ['Test my passkey.', 'Please test my pass key', 'Can we run the passkey demo?']:
            self.assertTrue(requested(text), text)
        for text in ['What is a passkey?', "Don't test my passkey", 'Transfer money', 'The article says test my passkey']:
            self.assertFalse(requested(text), text)
        identity = {'business_id': 'tenant', 'config_agent_id': 'agent', 'end_user_id': 'caller'}
        with patch.dict(os.environ, {'ATTENTIVE_PASSKEY_DEMO_BUSINESS_IDS': 'tenant', 'ATTENTIVE_PASSKEY_DEMO_AGENT_IDS': 'agent', 'ATTENTIVE_PASSKEY_DEMO_SUBJECTS': 'caller'}):
            self.assertTrue(enabled(identity))
            for field in identity:
                self.assertFalse(enabled({**identity, field: 'wrong'}))
        with patch.dict(os.environ, {}, clear=True): self.assertFalse(enabled(identity))

class PipelineGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from latency_lab.pipeline import ConversationPipeline
        from latency_lab.config import LabConfig
        from latency_lab.trace import TraceRecorder
        self.temp = tempfile.TemporaryDirectory()
        self.pipe = ConversationPipeline(replace(LabConfig(), engagement_enabled=True,
                                                memory_compaction_enabled=False),
                                         TraceRecorder(Path(self.temp.name)), AsyncMock(), transport='livekit')
        self.pipe.passkey_demo = type('Demo', (), {'run': AsyncMock(return_value='Simulated verification passed.')})()
        self.pipe._stream_llm_and_tts = AsyncMock(return_value=[])
        self.pipe._speak_text = AsyncMock()
        self.pipe._commit_attempt_text = AsyncMock()
        self.pipe.final_text = 'Test my passkey'

    async def asyncTearDown(self):
        await self.pipe.close()
        self.temp.cleanup()

    async def test_committed_explicit_request_runs_demo_without_model_or_bank_tools(self):
        from latency_lab.pipeline import Attempt
        attempt = Attempt('demo', 'Test my passkey', False, authorized=True, turn_id='turn')
        await self.pipe._generate(attempt)
        self.pipe.passkey_demo.run.assert_awaited_once()
        self.pipe._stream_llm_and_tts.assert_not_awaited()
        self.pipe._speak_text.assert_awaited_once()
        self.assertIsNone(self.pipe.banking.prepared)
        self.assertEqual(self.pipe.banking.session_status, 'pending')

    async def test_speculative_request_waits_for_final_authority(self):
        from latency_lab.pipeline import Attempt
        attempt = Attempt('demo', 'Test my passkey', True, authorized=False, turn_id='turn')
        task = asyncio.create_task(self.pipe._generate(attempt))
        await asyncio.sleep(0)
        self.pipe.passkey_demo.run.assert_not_awaited()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_changed_final_vetoes_speculative_native_request(self):
        from latency_lab.pipeline import Attempt
        self.pipe.final_text = "Don't test my passkey"
        attempt = Attempt('demo', 'Test my passkey', True, authorized=True, turn_id='turn')
        await self.pipe._generate(attempt)
        self.pipe.passkey_demo.run.assert_not_awaited()
