import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.lagos import deploy as release


AGENT_ID = '11111111-2222-3333-4444-555555555555'
MOMO_BUSINESS_ID = 'f2ff0fe3d56c468890489a76a6310d4a'
# momo_feedback_route normalises ids via uuid.UUID(...), so stored ids are dashed.
MOMO_BUSINESS_ID_DASHED = 'f2ff0fe3-d56c-4688-9048-9a76a6310d4a'
BASE_ROUTE = {
    'trunk_ids': ['ST_test_trunk'],
    'caller_routes': {
        '+2347012345678': {'config_agent_id': 'existing-agent'},
    },
    'did_routes': {
        '030105': {
            'business_id': MOMO_BUSINESS_ID,
            'config_agent_id': 'ccc440c5452d4690abc061758c11cebd',
            'configured_agent_name': 'Amaka',
        },
    },
}


class LagosReleaseTests(unittest.TestCase):
    def exercise_release(self, *, enabled=False, old_value=None, drift=False,
                         reply_grace_ms='0', reply_grace_agent_ids='',
                         llm_provider='qwen', deepseek_api_key=''):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            base_env = ['TOKEN=unchanged=secret', 'MODEL=existing',
                        'RVC_LAB_STT_CONTEXT=sample vocabulary hints'] + [
                f'{key}={value}' for key, value in {
                    **release.LAGOS_ASR_ENV,
                    'RVC_LAB_STT_MODEL': 'whisper-large-v3-turbo',
                    'RVC_LAB_STT_WS_URL': 'ws://asr.example/whisper-rt/v1/realtime',
                }.items()
            ] + ['RVC_AICC_SIP_ROUTE=' + json.dumps(
                BASE_ROUTE, separators=(',', ':'), sort_keys=True)]
            if old_value is not None:
                base_env.append(f'{release.HAUSA_RESEARCH_KEY}={old_value}')
            previous_env = dict(item.split('=', 1) for item in base_env)

            updates = dict(release.LAGOS_ASR_ENV)
            updates.update(release.reply_grace_env(reply_grace_ms, reply_grace_agent_ids))
            updates.update(release.inactivity_pilot_env())
            updates['RVC_LAB_LLM_PROVIDER'] = llm_provider
            updates['RVC_LAB_DEEPSEEK_API_KEY'] = deepseek_api_key if llm_provider == 'deepseek' else ''
            if llm_provider == 'deepseek':
                updates.update({
                    'RVC_LAB_DEEPSEEK_BASE_URL': 'https://api.deepseek.com',
                    'RVC_LAB_DEEPSEEK_MODEL': 'deepseek-flash',
                    'RVC_LAB_DEEPSEEK_THINKING': 'false',
                    'RVC_LAB_DEEPSEEK_REASONING_EFFORT': 'high',
                    'RVC_LAB_DEEPSEEK_API_KEY': deepseek_api_key,
                })
            if enabled:
                updates[release.HAUSA_RESEARCH_KEY] = 'true'
            momo_route = release.momo_feedback_route(previous_env, AGENT_ID)
            updates['RVC_AICC_SIP_ROUTE'] = release.leadway_caller_routes(
                {'RVC_AICC_SIP_ROUTE': momo_route}
            )
            expected_env = [f'{key}={value}' for key, value in (previous_env | updates).items()]

            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': base_env}, 'Mounts': [{'Source': '/data'}]}
            current = {**previous, 'Id': 'new-container', 'Image': 'new-image',
                       'Config': {'Env': list(expected_env)}}
            if drift:
                current['Config']['Env'] = list(expected_env) + ['UNEXPECTED=change']
            with patch.object(release, 'RELEASE_ROOT', root), \
                 patch.object(release, 'inspect_voice', return_value=(previous, [root / 'compose.yml'])), \
                 patch.object(release, 'run') as run, \
                 patch.object(release, 'wait_healthy', side_effect=[current, previous]), \
                 patch.object(release.subprocess, 'check_output', return_value='new-image\n'):
                if drift:
                    with self.assertRaisesRegex(RuntimeError, 'environment or mounts'):
                        release.deploy(target, revision, enabled, AGENT_ID,
                                       reply_grace_ms, reply_grace_agent_ids,
                                       llm_provider, 'deepseek-flash', 'high', deepseek_api_key)
                    self.assertFalse((target / 'DEPLOYED').exists())
                    rollback = json.loads((target / 'voice-rollback.json').read_text())
                    rollback_env = {key: previous_env.get(key) for key in updates}
                    rollback_env['RVC_AICC_SIP_ROUTE'] = previous_env['RVC_AICC_SIP_ROUTE']
                    rollback_env[release.REPLY_GRACE_MS_KEY] = previous_env.get(
                        release.REPLY_GRACE_MS_KEY)
                    rollback_env[release.REPLY_GRACE_AGENT_IDS_KEY] = previous_env.get(
                        release.REPLY_GRACE_AGENT_IDS_KEY)
                    rollback_env[release.INACTIVITY_PILOT_AGENT_IDS_KEY] = previous_env.get(
                        release.INACTIVITY_PILOT_AGENT_IDS_KEY)
                    if enabled:
                        rollback_env[release.HAUSA_RESEARCH_KEY] = old_value
                    self.assertEqual(rollback['services']['voice']['environment'], rollback_env)
                    self.assertIn('before-', rollback['services']['voice']['image'])
                else:
                    release.deploy(target, revision, enabled, AGENT_ID,
                                   reply_grace_ms, reply_grace_agent_ids,
                                   llm_provider, 'deepseek-flash', 'high', deepseek_api_key)
                    self.assertTrue((target / 'DEPLOYED').exists())
                override = json.loads((target / 'voice-ci.json').read_text())['services']['voice']
                self.assertEqual(override.get('environment', {}), updates)
                if llm_provider == 'deepseek':
                    plan = json.loads((target / 'plan.json').read_text())
                    self.assertEqual(plan['environment_updates']['RVC_LAB_DEEPSEEK_API_KEY'], '[redacted]')
                    self.assertNotIn(deepseek_api_key, (target / 'plan.json').read_text())
                route = json.loads(override['environment']['RVC_AICC_SIP_ROUTE'])
                for number in ('0301051', '301051'):
                    self.assertEqual(route['did_routes'][number]['config_agent_id'], AGENT_ID)
                    self.assertEqual(route['did_routes'][number]['business_id'], MOMO_BUSINESS_ID_DASHED)
                    self.assertTrue(route['did_routes'][number]['outbound_required'])
                self.assertEqual(route['did_routes']['030105'], BASE_ROUTE['did_routes']['030105'])
                self.assertEqual(
                    route['caller_routes']['+2347012345678'],
                    {'config_agent_id': 'existing-agent'},
                )
                self.assertNotIn('08112812216', route['caller_routes'])
                self.assertNotIn('+2348112812216', route['caller_routes'])
                for number, target in release.LEADWAY_CALLER_ROUTES.items():
                    self.assertEqual(route['caller_routes'][number], target)
                    self.assertEqual(
                        route['caller_routes'][number]['config_agent_id'],
                        'c4f4925d-0188-431e-a881-15533f6bb31b',
                    )
                self.assertEqual(
                    set(updates['RVC_LAB_STT_CHAT_SCOPE'].split()),
                    {
                        'a0c8cda2-3b46-4ee0-898c-48f9a4d3a444',
                        '00f99439-59d9-42a2-9c3a-db06422ea6cf',
                    },
                )
                self.assertEqual(
                    dict(item.split('=', 1) for item in current['Config']['Env'])[
                        'RVC_LAB_STT_CONTEXT'
                    ],
                    'sample vocabulary hints',
                )
                for call in run.call_args_list:
                    if 'up' in call.args:
                        self.assertEqual(call.args[-1], 'voice')
                        self.assertIn('--no-deps', call.args)

    def test_normal_release_preserves_environment(self):
        self.exercise_release()

    def test_deepseek_release_adds_settings_and_redacts_key_from_plan(self):
        self.exercise_release(llm_provider='deepseek', deepseek_api_key='test-secret')

    def test_deepseek_release_requires_api_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': ['RVC_AICC_SIP_ROUTE=' + json.dumps(BASE_ROUTE)]},
                        'Mounts': []}
            with patch.object(release, 'RELEASE_ROOT', root), \
                 patch.object(release, 'inspect_voice', return_value=(previous, [root / 'compose.yml'])), \
                 patch.object(release, 'run'):
                with self.assertRaisesRegex(ValueError, 'API key is required'):
                    release.deploy(target, revision, False, AGENT_ID,
                                   llm_provider='deepseek')

    def test_opt_in_adds_only_research_flag(self):
        self.exercise_release(enabled=True)

    def test_opt_in_updates_existing_flag(self):
        self.exercise_release(enabled=True, old_value='false')

    def test_agent_scoped_reply_grace_can_be_released(self):
        self.exercise_release(reply_grace_ms='500', reply_grace_agent_ids=AGENT_ID)

    def test_inactivity_pilot_agent_allowlist_is_validated_and_released(self):
        self.assertEqual(release.inactivity_pilot_env(AGENT_ID)[
            release.INACTIVITY_PILOT_AGENT_IDS_KEY], AGENT_ID)
        with self.assertRaisesRegex(ValueError, 'must contain UUIDs'):
            release.inactivity_pilot_env('not-an-agent')

    def test_reply_grace_requires_agent_id_and_bounded_delay(self):
        with self.assertRaisesRegex(ValueError, 'AGENT_IDS is required'):
            release.reply_grace_env('500', '')
        with self.assertRaisesRegex(ValueError, 'integer from 0 to 1500'):
            release.reply_grace_env('2000', AGENT_ID)
        with self.assertRaisesRegex(ValueError, 'must contain UUIDs'):
            release.reply_grace_env('500', 'not-an-agent')

    def test_failed_release_removes_previously_absent_flag(self):
        self.exercise_release(enabled=True, drift=True)

    def test_failed_release_restores_existing_flag(self):
        self.exercise_release(enabled=True, old_value='false', drift=True)

    def test_release_requires_momo_feedback_agent_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': ['RVC_AICC_SIP_ROUTE=' + json.dumps(BASE_ROUTE)]},
                        'Mounts': []}
            with patch.object(release, 'RELEASE_ROOT', root), \
                 patch.object(release, 'inspect_voice', return_value=(previous, [root / 'compose.yml'])), \
                 patch.object(release, 'run'):
                with self.assertRaisesRegex(RuntimeError, 'AICC_MOMO_FEEDBACK_AGENT_ID'):
                    release.deploy(target, revision, False, '')

    def test_release_requires_existing_momo_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            empty_route = json.dumps({'did_routes': {}})
            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': ['RVC_AICC_SIP_ROUTE=' + empty_route]},
                        'Mounts': []}
            with patch.object(release, 'RELEASE_ROOT', root), \
                 patch.object(release, 'inspect_voice', return_value=(previous, [root / 'compose.yml'])), \
                 patch.object(release, 'run'):
                with self.assertRaisesRegex(RuntimeError, 'MoMo route is missing'):
                    release.deploy(target, revision, False, AGENT_ID)

    def test_momo_feedback_caller_override_is_removed(self):
        env = {
            'RVC_AICC_SIP_ROUTE': json.dumps({
                'caller_routes': {
                    '08112812216': {'config_agent_id': 'different-agent'}
                }
            })
        }
        updated = json.loads(release.leadway_caller_routes(env))['caller_routes']
        self.assertNotIn('08112812216', updated)
        self.assertIn('+2349040760651', updated)

    def test_existing_leadway_caller_route_is_retained_with_did_override(self):
        legacy_route = {
            'business_id': release.LEADWAY_CALLER_ROUTES['+2349040760651']['business_id'],
            'config_agent_id': release.LEADWAY_CALLER_ROUTES['+2349040760651']['config_agent_id'],
            'configured_agent_name': 'Amaka',
            'voice_auth_required': False,
        }
        env = {'RVC_AICC_SIP_ROUTE': json.dumps({
            'caller_routes': {'09040760651': legacy_route}
        })}

        upgraded = json.loads(release.leadway_caller_routes(env))['caller_routes']

        self.assertTrue(upgraded['09040760651']['override_did_routes'])
        self.assertEqual(
            upgraded['09040760651']['config_agent_id'],
            'c4f4925d-0188-431e-a881-15533f6bb31b',
        )

    def test_existing_momo_caller_override_is_removed_even_if_stale(self):
        env = {'RVC_AICC_SIP_ROUTE': json.dumps({
            'caller_routes': {'08112812216': {
                'config_agent_id': 'stale-agent',
            }}
        })}

        updated = json.loads(release.leadway_caller_routes(env))['caller_routes']
        self.assertNotIn('08112812216', updated)
