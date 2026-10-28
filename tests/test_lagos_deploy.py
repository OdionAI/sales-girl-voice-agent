import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deployment.lagos import deploy as release


class LagosReleaseTests(unittest.TestCase):
    def exercise_release(self, *, enabled=False, old_value=None, drift=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            env = ['TOKEN=unchanged=secret', 'MODEL=existing',
                   'RVC_LAB_STT_CONTEXT=sample vocabulary hints',
                   'RVC_AICC_SIP_ROUTE=' + json.dumps({
                       'trunk_ids': ['ST_test'],
                       'caller_routes': {
                           '+2347012345678': {'config_agent_id': 'existing-agent'}
                       },
                       'did_routes': {'030105': {'config_agent_id': 'existing-did'}},
                   })] + [
                f'{key}={value}' for key, value in {
                    **release.LAGOS_ASR_ENV,
                    'RVC_LAB_STT_MODEL': 'whisper-large-v3-turbo',
                    'RVC_LAB_STT_WS_URL': 'ws://asr.example/whisper-rt/v1/realtime',
                }.items()
            ]
            previous_env = dict(item.split('=', 1) for item in env)
            if old_value is not None:
                env.append(f'{release.HAUSA_RESEARCH_KEY}={old_value}')
            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': env}, 'Mounts': [{'Source': '/data'}]}
            updates = dict(release.LAGOS_ASR_ENV)
            updates['RVC_AICC_SIP_ROUTE'] = release.leadway_caller_routes(previous_env)
            if enabled:
                updates[release.HAUSA_RESEARCH_KEY] = 'true'
            expected_env = [f'{key}={value}' for key, value in
                            (previous_env | updates).items()]
            current = {**previous, 'Id': 'new-container', 'Image': 'new-image',
                       'Config': {'Env': list(expected_env)}}
            if enabled:
                current['Config']['Env'] = [item for item in current['Config']['Env']
                    if not item.startswith(release.HAUSA_RESEARCH_KEY + '=')]
                current['Config']['Env'].append(f'{release.HAUSA_RESEARCH_KEY}=true')
            if drift:
                current['Config']['Env'].append('UNEXPECTED=change')
            with patch.object(release, 'RELEASE_ROOT', root), \
                 patch.object(release, 'inspect_voice', return_value=(previous, [root / 'compose.yml'])), \
                 patch.object(release, 'run') as run, \
                 patch.object(release, 'wait_healthy', side_effect=[current, previous]), \
                 patch.object(release.subprocess, 'check_output', return_value='new-image\n'):
                if drift:
                    with self.assertRaisesRegex(RuntimeError, 'environment or mounts'):
                        release.deploy(target, revision, enabled)
                    self.assertFalse((target / 'DEPLOYED').exists())
                    rollback = json.loads((target / 'voice-rollback.json').read_text())
                    rollback_env = {
                        key: previous_env.get(key) for key in release.LAGOS_ASR_ENV
                    }
                    rollback_env['RVC_AICC_SIP_ROUTE'] = previous_env['RVC_AICC_SIP_ROUTE']
                    rollback_env[release.HAUSA_RESEARCH_KEY] = old_value
                    self.assertEqual(rollback['services']['voice']['environment'], rollback_env)
                    self.assertIn('before-', rollback['services']['voice']['image'])
                else:
                    release.deploy(target, revision, enabled)
                    self.assertTrue((target / 'DEPLOYED').exists())
                override = json.loads((target / 'voice-ci.json').read_text())['services']['voice']
                self.assertEqual(override.get('environment', {}), updates)
                updated_route = json.loads(updates['RVC_AICC_SIP_ROUTE'])
                self.assertEqual(
                    updated_route['caller_routes']['+2347012345678'],
                    {'config_agent_id': 'existing-agent'},
                )
                self.assertEqual(
                    updated_route['did_routes'],
                    {'030105': {'config_agent_id': 'existing-did'}},
                )
                for number, target in release.LEADWAY_CALLER_ROUTES.items():
                    self.assertEqual(updated_route['caller_routes'][number], target)
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

    def test_opt_in_adds_only_research_flag(self):
        self.exercise_release(enabled=True)

    def test_opt_in_updates_existing_flag(self):
        self.exercise_release(enabled=True, old_value='false')

    def test_failed_release_removes_previously_absent_flag(self):
        self.exercise_release(enabled=True, drift=True)

    def test_failed_release_restores_existing_flag(self):
        self.exercise_release(enabled=True, old_value='false', drift=True)

    def test_conflicting_leadway_caller_route_fails_closed(self):
        env = {
            'RVC_AICC_SIP_ROUTE': json.dumps({
                'caller_routes': {
                    '08112812216': {'config_agent_id': 'different-agent'}
                }
            })
        }
        with self.assertRaisesRegex(RuntimeError, 'already has a different route'):
            release.leadway_caller_routes(env)

    def test_existing_leadway_caller_route_is_upgraded_with_did_override(self):
        legacy_route = {
            'business_id': release.LEADWAY_CALLER_ROUTES['+2348112812216']['business_id'],
            'config_agent_id': release.LEADWAY_CALLER_ROUTES['+2348112812216']['config_agent_id'],
            'configured_agent_name': 'Amaka',
            'voice_auth_required': False,
        }
        env = {'RVC_AICC_SIP_ROUTE': json.dumps({
            'caller_routes': {'08112812216': legacy_route}
        })}

        upgraded = json.loads(release.leadway_caller_routes(env))['caller_routes']

        self.assertTrue(upgraded['08112812216']['override_did_routes'])
        self.assertTrue(upgraded['+2349040760651']['override_did_routes'])

    def test_existing_leadway_route_with_wrong_agent_still_fails_closed(self):
        env = {'RVC_AICC_SIP_ROUTE': json.dumps({
            'caller_routes': {'08112812216': {
                **release.LEADWAY_CALLER_ROUTES['+2348112812216'],
                'config_agent_id': 'different-agent',
            }}
        })}

        with self.assertRaisesRegex(RuntimeError, 'already has a different route'):
            release.leadway_caller_routes(env)
