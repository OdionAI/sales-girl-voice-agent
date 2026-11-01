#!/usr/bin/env bash
# Official browser SDK, pinned and integrity checked; no npm install scripts.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p target/browser-sdk
sg_sdk_archive=target/browser-sdk/livekit-client.tgz
if [[ ! -f "$sg_sdk_archive" ]]; then
 curl --fail --silent --show-error --max-time 60 https://registry.npmjs.org/livekit-client/-/livekit-client-2.23.0.tgz -o "$sg_sdk_archive"
fi
python3 - <<'PY'
import hashlib,base64,tarfile
from pathlib import Path
p=Path('target/browser-sdk/livekit-client.tgz')
expected='sha512-j9Kg86nJjBjNFUD+ukp2jwMCGa0k+YD5gU//RzAM+ZY3pbG631QCte7fQjwqWMmtBzhDJL9nKYuOLMv/9IbAxg=='
assert 'sha512-'+base64.b64encode(hashlib.sha512(p.read_bytes()).digest()).decode()==expected,'SDK integrity mismatch'
with tarfile.open(p) as archive:
 for name in ['package/dist/livekit-client.umd.js','package/LICENSE']:
  Path('target/browser-sdk',Path(name).name).write_bytes(archive.extractfile(name).read())
print('Official LiveKit browser SDK 2.23.0 verified')
PY
