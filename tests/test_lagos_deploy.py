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
            env = ['TOKEN=unchanged=secret', 'MODEL=existing']
            if old_value is not None:
                env.append(f'{release.HAUSA_RESEARCH_KEY}={old_value}')
            previous = {'Id': 'old-container', 'Image': 'old-image',
                        'Config': {'Env': env}, 'Mounts': [{'Source': '/data'}]}
            current = {**previous, 'Id': 'new-container', 'Image': 'new-image',
                       'Config': {'Env': list(env)}}
            if enabled:
                current['Config']['Env'] = [item for item in env
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
                    self.assertEqual(rollback['services']['voice']['environment'],
                                     {release.HAUSA_RESEARCH_KEY: old_value})
                    self.assertIn('before-', rollback['services']['voice']['image'])
                else:
                    release.deploy(target, revision, enabled)
                    self.assertTrue((target / 'DEPLOYED').exists())
                override = json.loads((target / 'voice-ci.json').read_text())['services']['voice']
                self.assertEqual(override.get('environment', {}),
                                 {release.HAUSA_RESEARCH_KEY: 'true'} if enabled else {})
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
