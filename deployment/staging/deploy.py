"""Release one Git revision to the currently running staging voice Compose service.

This mirrors ``deployment/lagos/deploy.py`` but targets the isolated staging
stack (``odion-staging``). SIP is intentionally NOT wired on staging, so the
Lagos SIP-route and AICC capacity mutations are omitted; the worker is exercised
through the browser/LiveKit path. TTS/ASR/LLM reuse the shared prod platform
endpoints (``102.88.137.124``), matching the copied ``runtime.env``.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

STACK = Path('/opt/attentive/releases/recovery-20261009')
PROJECT = 'odion-staging'
CONTAINER = 'odion-staging-voice-1'
RELEASE_ROOT = Path('/opt/attentive/releases/voice-ci')
HOSTNAME = 'attentive-staging-server'
HAUSA_RESEARCH_KEY = 'RVC_LAB_ENABLE_HAUSA_TWB_VOICES'
REPLY_GRACE_MS_KEY = 'RVC_LAB_REPLY_GRACE_MS'
REPLY_GRACE_AGENT_IDS_KEY = 'RVC_LAB_REPLY_GRACE_AGENT_IDS'
INACTIVITY_PILOT_AGENT_IDS_KEY = 'RVC_LAB_INACTIVITY_PILOT_AGENT_IDS'
DEFAULT_INACTIVITY_PILOT_AGENT_IDS = '*'
# Reuse the shared prod Qwen realtime ASR. Chat transcripts remain authoritative
# and fall back to realtime finals, exactly as in Lagos.
STAGING_ASR_ENV = {
    # Staging-only trial: shorten turn endpointing while retaining final ASR validation.
    'RVC_LAB_END_SILENCE_MS': '150',
    'RVC_LAB_STT_PROVIDER': 'self_hosted',
    'RVC_LAB_STT_MODEL': 'Qwen3-ASR',
    'RVC_LAB_STT_WS_URL': 'ws://102.88.137.124:8080/asr-rt/v1/realtime',
    'RVC_LAB_STT_BATCH_URL': 'http://102.88.137.124:8080/asr-rt/v1/audio/transcriptions',
    'RVC_LAB_STT_CHAT_URL': 'http://102.88.137.124:8080/asr-rt/v1/chat/completions',
    'RVC_LAB_STT_CHAT_MODEL': 'Qwen3-ASR',
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def inspect_voice():
    if socket.gethostname() != HOSTNAME:
        raise RuntimeError('Unexpected host')
    current = json.loads(subprocess.check_output(['docker', 'inspect', CONTAINER]))[0]
    labels = current['Config']['Labels']
    if (labels.get('com.docker.compose.project') != PROJECT
            or labels.get('com.docker.compose.service') != 'voice'
            or labels.get('com.docker.compose.project.working_dir') != str(STACK)):
        raise RuntimeError('Unexpected Compose target')
    if not current['State']['Running'] or current['State'].get('Health', {}).get('Status') != 'healthy':
        raise RuntimeError('Voice container is not healthy')
    files = [Path(file) for file in labels['com.docker.compose.project.config_files'].split(',')]
    if not files or any(not file.is_file() or not (
            file.parent == STACK or file.parent.parent == RELEASE_ROOT) for file in files):
        raise RuntimeError('Unexpected active Compose file chain')
    return current, files


def compose(files):
    command = ['docker', 'compose', '--project-name', PROJECT]
    for file in files:
        command.extend(['-f', str(file)])
    return command


def wait_healthy():
    for _ in range(36):
        try:
            return inspect_voice()[0]
        except (RuntimeError, subprocess.CalledProcessError):
            time.sleep(5)
    raise RuntimeError('New voice container did not become healthy')


def image_override(path, image, environment=None, runtime=None):
    service = {'image': image}
    if runtime:
        service.update(runtime)
    if environment:
        service['environment'] = environment
    path.write_text(json.dumps({'services': {'voice': service}}) + '\n')


def worker_runtime(previous, *, native):
    config = previous['Config']
    if native:
        return {
            'entrypoint': ['/usr/local/bin/rvc-worker'],
            'command': ['--env'],
            'user': config.get('User') or '0',
            'healthcheck': {
                'test': ['CMD', '/usr/local/bin/rvc-worker', '--healthcheck'],
                'interval': '10s', 'timeout': '3s', 'start_period': '60s', 'retries': 3,
            },
        }
    health = config.get('Healthcheck') or {}
    restored = {'test': health['Test']} if health.get('Test') else {'disable': True}
    for docker_key, compose_key in [('Interval', 'interval'), ('Timeout', 'timeout'),
                                     ('StartPeriod', 'start_period')]:
        if health.get(docker_key):
            restored[compose_key] = f'{health[docker_key]}ns'
    if 'Retries' in health:
        restored['retries'] = health['Retries']
    return {'entrypoint': config.get('Entrypoint'), 'command': config.get('Cmd'),
            'user': config.get('User') or '0', 'healthcheck': restored}


def reply_grace_env(delay_ms='0', agent_ids=''):
    delay = str(delay_ms).strip()
    if not re.fullmatch(r'\d{1,4}', delay) or int(delay) > 1500:
        raise ValueError('RVC_LAB_REPLY_GRACE_MS must be an integer from 0 to 1500')
    raw_ids = [value.strip() for value in str(agent_ids).split(',') if value.strip()]
    try:
        normalized_ids = [str(__import__('uuid').UUID(value)) for value in raw_ids]
    except ValueError as exc:
        raise ValueError('RVC_LAB_REPLY_GRACE_AGENT_IDS must contain UUIDs') from exc
    if int(delay) and not normalized_ids:
        raise ValueError('RVC_LAB_REPLY_GRACE_AGENT_IDS is required when delay is enabled')
    return {
        REPLY_GRACE_MS_KEY: str(int(delay)),
        REPLY_GRACE_AGENT_IDS_KEY: ','.join(dict.fromkeys(normalized_ids)),
    }


def inactivity_pilot_env(agent_ids=None):
    if agent_ids is None:
        agent_ids = DEFAULT_INACTIVITY_PILOT_AGENT_IDS
    raw_ids = [value.strip() for value in str(agent_ids).split(',') if value.strip()]
    if raw_ids == ['*']:
        return {INACTIVITY_PILOT_AGENT_IDS_KEY: '*'}
    if '*' in raw_ids:
        raise ValueError('Wildcard must be used alone in RVC_LAB_INACTIVITY_PILOT_AGENT_IDS')
    try:
        normalized_ids = [str(__import__('uuid').UUID(value)) for value in raw_ids]
    except ValueError as exc:
        raise ValueError('RVC_LAB_INACTIVITY_PILOT_AGENT_IDS must contain UUIDs') from exc
    return {INACTIVITY_PILOT_AGENT_IDS_KEY: ','.join(dict.fromkeys(normalized_ids))}


def deploy(release, revision, enable_hausa_research=False, reply_grace_ms='0',
           reply_grace_agent_ids='', llm_provider='qwen', deepseek_model='deepseek-flash',
           deepseek_reasoning_effort='high', deepseek_api_key='', inactivity_pilot_agent_ids=None):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected a full Git commit SHA')
    if (release.parent != RELEASE_ROOT
            or not re.fullmatch(revision + r'-\d+-\d+', release.name)
            or (release / 'plan.json').exists()):
        raise ValueError('Unexpected or reused release directory')
    previous, files = inspect_voice()
    # Env entries are normally KEY=VALUE, but a previous rollback could have
    # written bare KEY entries (see below); treat a missing '=' as empty so the
    # parser never crashes on an already-degraded container.
    previous_env = dict(
        item.split('=', 1) if '=' in item else (item, '')
        for item in previous['Config']['Env']
    )
    updates = dict(STAGING_ASR_ENV)
    updates.update(reply_grace_env(reply_grace_ms, reply_grace_agent_ids))
    updates.update(inactivity_pilot_env(inactivity_pilot_agent_ids))
    provider = str(llm_provider).strip().lower()
    if provider not in {'qwen', 'deepseek'}:
        raise ValueError('RVC_LAB_LLM_PROVIDER must be qwen or deepseek')
    updates['RVC_LAB_LLM_PROVIDER'] = provider
    if provider == 'deepseek':
        model = str(deepseek_model).strip()
        effort = str(deepseek_reasoning_effort).strip().lower()
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', model):
            raise ValueError('Invalid DeepSeek model name')
        if effort not in {'low', 'high', 'max'}:
            raise ValueError('DeepSeek reasoning effort must be low, high, or max')
        if not str(deepseek_api_key).strip():
            raise ValueError('DeepSeek API key is required when DeepSeek is selected')
        updates.update({
            'RVC_LAB_DEEPSEEK_BASE_URL': 'https://api.deepseek.com',
            'RVC_LAB_DEEPSEEK_MODEL': model,
            'RVC_LAB_DEEPSEEK_THINKING': 'false',
            'RVC_LAB_DEEPSEEK_REASONING_EFFORT': effort,
            'RVC_LAB_DEEPSEEK_API_KEY': str(deepseek_api_key).strip(),
        })
    else:
        updates['RVC_LAB_DEEPSEEK_API_KEY'] = ''
    if enable_hausa_research:
        updates[HAUSA_RESEARCH_KEY] = 'true'
    expected_env_dict = dict(previous_env | updates)
    expected_env = [f'{key}={value}' for key, value in expected_env_dict.items()]
    old_image = f'attentive/voice:staging-before-{release.name}'
    new_image = f'attentive/voice:staging-ci-{release.name}'
    run('docker', 'tag', previous['Image'], old_image)
    run('docker', 'build', '-f', str(release / 'deployment/staging/Dockerfile'),
        # The staging VM uses the containerd image store with BuildKit. By
        # default BuildKit attaches provenance/SBOM attestations and exports an
        # image *index* (manifest list); `docker image inspect <tag>.Id` then
        # returns that index digest, which never matches the platform config
        # digest a container actually runs. Disable attestations so the tag is a
        # single-platform image and its Id matches the running container's.
        '--provenance=false', '--sbom=false',
        '--label', f'org.opencontainers.image.revision={revision}',
        '--label', 'org.opencontainers.image.source=https://github.com/OdionAI/sales-girl-voice-agent',
        '-t', new_image, str(release))
    native_env = previous_env | updates
    env_args = [arg for key in native_env for arg in ('--env', key)]
    run('docker', 'run', '--rm', '--network', 'none',
        '--volumes-from', f"{previous['Id']}:ro", '--user', previous['Config'].get('User') or '0', *env_args,
        new_image, '--check-env', env=os.environ | native_env)
    if inspect_voice()[0]['Id'] != previous['Id']:
        raise RuntimeError('Voice container changed during build; rerun')
    plan_updates = {
        key: ('[redacted]' if key.endswith('_API_KEY') and value else value)
        for key, value in updates.items()
    }
    plan = {'revision': revision, 'previous_image': old_image,
            'previous_image_id': previous['Image'], 'image': new_image,
            'compose_files': [str(file) for file in files],
            'environment_updates': plan_updates}
    (release / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    override = release / 'voice-ci.json'
    image_override(override, new_image, previous_env | updates,
                   worker_runtime(previous, native=True))
    attempted = False
    try:
        run(*compose([*files, override]), 'config', '-q', cwd=STACK)
        attempted = True
        run(*compose([*files, override]), 'up', '-d', '--no-deps', '--no-build',
            '--force-recreate', 'voice', cwd=STACK)
        current = wait_healthy()
        # Verify against what we asked Compose to run, not raw image IDs: with
        # the containerd image store `docker image inspect <tag>.Id` is the
        # manifest digest, whereas a container's `.Image` is its config digest,
        # so an id equality check can never pass. Config.Image carries the tag
        # the container was created from (the override pins it to new_image),
        # and the just-created container is authoritative for that tag. Require
        # every expected env key be present with the right value (the image may
        # legitimately contribute extra default env) and the mounts unchanged.
        current_env = dict(
            item.split('=', 1) if '=' in item else (item, '')
            for item in current['Config']['Env']
        )
        def _redact(key, value):
            if value is not None and ('SECRET' in key or 'API_KEY' in key or 'TOKEN' in key or 'PASSWORD' in key):
                return f'<set len={len(str(value))}>' if value else '<empty>'
            return value
        env_diff = {
            key: (_redact(key, current_env.get(key)), _redact(key, value))
            for key, value in expected_env_dict.items()
            if current_env.get(key) != value
        }
        problems = []
        if current['Config'].get('Image') != new_image:
            problems.append(f"image tag {current['Config'].get('Image')!r} != {new_image!r}")
        if current['Id'] == previous['Id']:
            problems.append('container id unchanged (not recreated)')
        if env_diff:
            problems.append('env differs: ' + json.dumps(env_diff, default=str))
        def _mount_set(mounts):
            return sorted(
                (m.get('Type'), m.get('Source'), m.get('Destination'), bool(m.get('RW')))
                for m in mounts
            )
        if _mount_set(current['Mounts']) != _mount_set(previous['Mounts']):
            problems.append('mounts differ: '
                            + json.dumps([_mount_set(current['Mounts']), _mount_set(previous['Mounts'])]))
        if problems:
            raise RuntimeError('Voice verification failed: ' + '; '.join(problems))
    except Exception:
        if attempted:
            rollback = release / 'voice-rollback.json'
            # Only restore keys that existed before this release; setting a key
            # that was previously absent to None makes Compose render a bare
            # KEY with no value, which poisons the next deploy's env parser.
            restore_env = {key: previous_env[key] for key in updates if key in previous_env}
            image_override(rollback, old_image, restore_env,
                           worker_runtime(previous, native=False))
            run(*compose([*files, rollback]), 'up', '-d', '--no-deps', '--no-build',
                '--force-recreate', 'voice', cwd=STACK)
            wait_healthy()
        raise
    (release / 'DEPLOYED').touch()
    print(json.dumps({'deployed': True, 'revision': revision, 'service': 'voice',
                      'project': PROJECT, 'health': 'healthy'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--enable-hausa-research', action='store_true')
    parser.add_argument('--reply-grace-ms', default='0')
    parser.add_argument('--reply-grace-agent-ids', default='')
    parser.add_argument('--inactivity-pilot-agent-ids')
    parser.add_argument('--llm-provider', default='qwen')
    parser.add_argument('--deepseek-model', default='deepseek-flash')
    parser.add_argument('--deepseek-reasoning-effort', default='high')
    parser.add_argument('--deepseek-api-key-stdin', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        current, files = inspect_voice()
        print(json.dumps({'host': socket.gethostname(), 'project': PROJECT,
                          'image': current['Config']['Image'],
                          'compose_files': len(files), 'health': 'healthy',
                          'architecture': __import__('platform').machine()}))
    else:
        if args.release is None or args.revision is None:
            parser.error('--release and --revision are required for deployment')
        os.umask(0o077)
        deepseek_api_key = sys.stdin.read().strip() if args.deepseek_api_key_stdin else ''
        with open('/var/lock/attentive-staging-voice-deploy.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(args.release.resolve(), args.revision, args.enable_hausa_research,
                   args.reply_grace_ms, args.reply_grace_agent_ids, args.llm_provider,
                   args.deepseek_model, args.deepseek_reasoning_effort,
                   deepseek_api_key, args.inactivity_pilot_agent_ids)
