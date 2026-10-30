"""Release one Git revision to the currently running Lagos voice Compose service."""

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
import uuid

STACK = Path('/opt/attentive/releases/recovery-20261009')
PROJECT = 'odion-recovery-lagos'
CONTAINER = 'odion-recovery-lagos-voice-1'
RELEASE_ROOT = Path('/opt/attentive/releases/voice-ci')
HAUSA_RESEARCH_KEY = 'RVC_LAB_ENABLE_HAUSA_TWB_VOICES'
REPLY_GRACE_MS_KEY = 'RVC_LAB_REPLY_GRACE_MS'
REPLY_GRACE_AGENT_IDS_KEY = 'RVC_LAB_REPLY_GRACE_AGENT_IDS'
INACTIVITY_PILOT_AGENT_IDS_KEY = 'RVC_LAB_INACTIVITY_PILOT_AGENT_IDS'
DEFAULT_INACTIVITY_PILOT_AGENT_ID = 'ccc440c5-452d-4690-abc0-61758c11cebd'
LAGOS_ASR_ENV = {
    'RVC_LAB_STT_PROVIDER': 'self_hosted',
    'RVC_LAB_STT_MODEL': 'Qwen3-ASR',
    'RVC_LAB_STT_WS_URL': 'ws://102.88.137.124:8080/asr-rt/v1/realtime',
    'RVC_LAB_STT_BATCH_URL': 'http://102.88.137.124:8080/asr-rt/v1/audio/transcriptions',
    # The chat endpoint is authoritative for the selected businesses. Empty or
    # failed chat transcripts automatically fall back to realtime Qwen finals.
    'RVC_LAB_STT_CHAT_URL': 'http://102.88.137.124:8080/asr-rt/v1/chat/completions',
    'RVC_LAB_STT_CHAT_MODEL': 'Qwen3-ASR',
    # Keep Wema's existing business scope and add only the intended Amaka agent.
    'RVC_LAB_STT_CHAT_SCOPE': (
        'a0c8cda2-3b46-4ee0-898c-48f9a4d3a444 '
        '00f99439-59d9-42a2-9c3a-db06422ea6cf'
    ),
}
LEADWAY_CALLER_ROUTES = {
    "+2349040760651": {
        "business_id": "762d1f6d-9a7a-4e41-9088-bc6dcbb6ef4e",
        "config_agent_id": "c4f4925d-0188-431e-a881-15533f6bb31b",
        "configured_agent_name": "Amaka",
        "voice_auth_required": False,
        "override_did_routes": True,
    },
}
MOMO_FEEDBACK_CALLERS = {"+2348112812216"}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def inspect_voice():
    if socket.gethostname() != 'attentive-lagos-server':
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


def image_override(path, image, environment=None):
    service = {'image': image}
    if environment:
        service['environment'] = environment
    path.write_text(json.dumps({'services': {'voice': service}}) + '\n')


def momo_feedback_route(previous_env, agent_id):
    agent_id = str(uuid.UUID(agent_id))
    route = json.loads(previous_env.get('RVC_AICC_SIP_ROUTE') or '{}')
    did_routes = route.get('did_routes')
    if not isinstance(did_routes, dict):
        raise RuntimeError('Lagos SIP To route map is missing')
    momo_route = did_routes.get('030105')
    if not isinstance(momo_route, dict) or not momo_route.get('business_id'):
        raise RuntimeError('Existing MoMo route is missing')
    business_id = str(uuid.UUID(momo_route['business_id']))
    feedback_route = {
        'business_id': business_id,
        'config_agent_id': agent_id,
        'configured_agent_name': 'MoMo Feedback',
        'outbound_required': True,
    }
    for number in ('0301051', '301051'):
        existing = did_routes.get(number)
        if existing and existing != feedback_route:
            raise RuntimeError(f'SIP To number {number} already has a different route')
        did_routes[number] = feedback_route
    return json.dumps(route, separators=(',', ':'), sort_keys=True)


def leadway_caller_routes(previous_env):
    """Apply explicit Leadway caller routes and clear callers using the MoMo DID."""
    route = json.loads(previous_env.get('RVC_AICC_SIP_ROUTE') or '{}')
    caller_routes = route.setdefault('caller_routes', {})
    if not isinstance(caller_routes, dict):
        raise RuntimeError('Lagos SIP caller route map is invalid')

    momo_digits = {
        ''.join(character for character in number if character.isdigit())[-10:]
        for number in MOMO_FEEDBACK_CALLERS
    }
    for existing_number in list(caller_routes):
        digits = ''.join(
            character for character in str(existing_number) if character.isdigit()
        )[-10:]
        if digits in momo_digits:
            del caller_routes[existing_number]

    for number, target in LEADWAY_CALLER_ROUTES.items():
        digits = ''.join(character for character in number if character.isdigit())[-10:]
        for existing_number, existing_route in caller_routes.items():
            existing_digits = ''.join(
                character for character in str(existing_number) if character.isdigit()
            )[-10:]
            if existing_digits == digits:
                if not isinstance(existing_route, dict):
                    raise RuntimeError(f'Caller number {number} already has a different route')
                # Upgrade the deployed mapping, which predates the explicit
                # flag needed to take precedence over carrier-rewritten DIDs.
                legacy_target = {
                    key: value for key, value in target.items()
                    if key != 'override_did_routes'
                }
                if any(existing_route.get(key) != value
                       for key, value in legacy_target.items()):
                    raise RuntimeError(f'Caller number {number} already has a different route')
                caller_routes[existing_number] = target
                break
        else:
            caller_routes[number] = target
    return json.dumps(route, separators=(',', ':'), sort_keys=True)


def reply_grace_env(delay_ms='0', agent_ids=''):
    delay = str(delay_ms).strip()
    if not re.fullmatch(r'\d{1,4}', delay) or int(delay) > 1500:
        raise ValueError('RVC_LAB_REPLY_GRACE_MS must be an integer from 0 to 1500')
    raw_ids = [value.strip() for value in str(agent_ids).split(',') if value.strip()]
    try:
        normalized_ids = [str(uuid.UUID(value)) for value in raw_ids]
    except ValueError as exc:
        raise ValueError('RVC_LAB_REPLY_GRACE_AGENT_IDS must contain UUIDs') from exc
    if int(delay) and not normalized_ids:
        raise ValueError('RVC_LAB_REPLY_GRACE_AGENT_IDS is required when delay is enabled')
    return {
        REPLY_GRACE_MS_KEY: str(int(delay)),
        REPLY_GRACE_AGENT_IDS_KEY: ','.join(dict.fromkeys(normalized_ids)),
    }


def inactivity_pilot_env(agent_ids=None):
    # Temporary single-agent production pilot; unset deployments select Amaka.
    if agent_ids is None:
        agent_ids = DEFAULT_INACTIVITY_PILOT_AGENT_ID
    raw_ids = [value.strip() for value in str(agent_ids).split(',') if value.strip()]
    try:
        normalized_ids = [str(uuid.UUID(value)) for value in raw_ids]
    except ValueError as exc:
        raise ValueError('RVC_LAB_INACTIVITY_PILOT_AGENT_IDS must contain UUIDs') from exc
    return {INACTIVITY_PILOT_AGENT_IDS_KEY: ','.join(dict.fromkeys(normalized_ids))}


def deploy(release, revision, enable_hausa_research=False, momo_feedback_agent_id='',
           reply_grace_ms='0', reply_grace_agent_ids='', llm_provider='qwen',
           deepseek_model='deepseek-flash', deepseek_reasoning_effort='high',
           deepseek_api_key='', inactivity_pilot_agent_ids=None):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected a full Git commit SHA')
    if (release.parent != RELEASE_ROOT
            or not re.fullmatch(revision + r'-\d+-\d+', release.name)
            or (release / 'plan.json').exists()):
        raise ValueError('Unexpected or reused release directory')
    previous, files = inspect_voice()
    previous_env = dict(item.split('=', 1) for item in previous['Config']['Env'])
    # Lagos ASR is shared by the voice worker. Reassert Qwen realtime + batch
    # endpoints each release so stale Whisper overrides cannot persist.
    updates = dict(LAGOS_ASR_ENV)
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
        # Qwen's existing URL/model/key remain untouched for one-variable rollback.
        # Remove the unused DeepSeek credential from the new container environment.
        updates['RVC_LAB_DEEPSEEK_API_KEY'] = ''
    if enable_hausa_research:
        updates[HAUSA_RESEARCH_KEY] = 'true'
    if not momo_feedback_agent_id:
        raise RuntimeError('AICC_MOMO_FEEDBACK_AGENT_ID is required for this release')
    route_env = {
        'RVC_AICC_SIP_ROUTE': momo_feedback_route(previous_env, momo_feedback_agent_id)
    }
    updates['RVC_AICC_SIP_ROUTE'] = leadway_caller_routes(route_env)
    expected_env = [f'{key}={value}' for key, value in (previous_env | updates).items()]
    old_image = f'attentive/voice:before-{release.name}'
    new_image = f'attentive/voice:ci-{release.name}'
    run('docker', 'tag', previous['Image'], old_image)
    run('docker', 'build', '-f', str(release / 'deployment/lagos/Dockerfile'),
        '--label', f'org.opencontainers.image.revision={revision}',
        '--label', 'org.opencontainers.image.source=https://github.com/OdionAI/sales-girl-voice-agent',
        '-t', new_image, str(release))
    run('docker', 'run', '--rm', '--network', 'none', new_image, 'python', '-c',
        'import pyrnnoise; import latency_lab.livekit_worker')
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
    image_override(override, new_image, updates)
    attempted = False
    try:
        run(*compose([*files, override]), 'config', '-q', cwd=STACK)
        attempted = True
        run(*compose([*files, override]), 'up', '-d', '--no-deps', '--no-build',
            '--force-recreate', 'voice', cwd=STACK)
        current = wait_healthy()
        if (current['Image'] != subprocess.check_output(['docker', 'image', 'inspect',
                new_image, '--format', '{{.Id}}'], text=True).strip()
                or sorted(current['Config']['Env']) != sorted(expected_env)
                or current['Mounts'] != previous['Mounts']):
            raise RuntimeError('Voice image, environment or mounts changed unexpectedly')
    except Exception:
        if attempted:
            rollback = release / 'voice-rollback.json'
            # A null Compose value removes a key absent from the prior container.
            image_override(rollback, old_image, {key: previous_env.get(key) for key in updates})
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
    parser.add_argument('--momo-feedback-agent-id', default='')
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
                          'compose_files': len(files), 'health': 'healthy'}))
    else:
        if args.release is None or args.revision is None:
            parser.error('--release and --revision are required for deployment')
        os.umask(0o077)
        deepseek_api_key = sys.stdin.read().strip() if args.deepseek_api_key_stdin else ''
        with open('/var/lock/attentive-voice-auth-deploy.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(args.release.resolve(), args.revision, args.enable_hausa_research,
                   args.momo_feedback_agent_id, args.reply_grace_ms,
                   args.reply_grace_agent_ids, args.llm_provider,
                   args.deepseek_model, args.deepseek_reasoning_effort,
                   deepseek_api_key, args.inactivity_pilot_agent_ids)
