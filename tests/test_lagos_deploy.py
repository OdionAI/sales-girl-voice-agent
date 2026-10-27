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
    'did_routes': {
        '030105': {
            'business_id': MOMO_BUSINESS_ID,
            'config_agent_id': 'ccc440c5452d4690abc061758c11cebd',
            'configured_agent_name': 'Amaka',
        },
    },
}


class LagosReleaseTests(unittest.TestCase):
    def exercise_release(self, *, enabled=False, old_value=None, drift=False):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            target = root / f'{revision}-1-1'
            target.mkdir()
            base_env = ['TOKEN=unchanged=secret', 'MODEL=existing',
                        'RVC_AICC_SIP_ROUTE=' + json.dumps(
                            BASE_ROUTE, separators=(',', ':'), sort_keys=True)]
            if old_value is not None:
                base_env.append(f'{release.HAUSA_RESEARCH_KEY}={old_value}')
            previous_env = dict(item.split('=', 1) for item in base_env)

            updates = {release.HAUSA_RESEARCH_KEY: 'true'} if enabled else {}
            updates['RVC_AICC_SIP_ROUTE'] = release.momo_feedback_route(previous_env, AGENT_ID)
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
                        release.deploy(target, revision, enabled, AGENT_ID)
                    self.assertFalse((target / 'DEPLOYED').exists())
                    rollback = json.loads((target / 'voice-rollback.json').read_text())
                    self.assertEqual(
                        rollback['services']['voice']['environment'],
                        {release.HAUSA_RESEARCH_KEY: old_value,
                         'RVC_AICC_SIP_ROUTE': previous_env['RVC_AICC_SIP_ROUTE']})
                    self.assertIn('before-', rollback['services']['voice']['image'])
                else:
                    release.deploy(target, revision, enabled, AGENT_ID)
                    self.assertTrue((target / 'DEPLOYED').exists())
                override = json.loads((target / 'voice-ci.json').read_text())['services']['voice']
                self.assertEqual(override.get('environment', {}), updates)
                route = json.loads(override['environment']['RVC_AICC_SIP_ROUTE'])
                for number in ('0301051', '301051'):
                    self.assertEqual(route['did_routes'][number]['config_agent_id'], AGENT_ID)
                    self.assertEqual(route['did_routes'][number]['business_id'], MOMO_BUSINESS_ID_DASHED)
                    self.assertTrue(route['did_routes'][number]['outbound_required'])
                self.assertEqual(route['did_routes']['030105'], BASE_ROUTE['did_routes']['030105'])
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
