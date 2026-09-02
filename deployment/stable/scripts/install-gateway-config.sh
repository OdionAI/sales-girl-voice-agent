#!/usr/bin/env bash
set -euo pipefail

target="${GATEWAY_CONFIG_TARGET:-/etc/nginx/sites-available/ascend-ai-gateway}"
source_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/gateway/ascend-ai-gateway.nginx.conf"

if [[ "${1:-}" != "--apply" ]]; then
  cat >&2 <<EOF
Dry-run guard: this script has not changed Nginx.
Usage: sudo $0 --apply
Source: ${source_file}
Target: ${target}

It writes a timestamped backup and runs nginx -t, but never reloads Nginx.
EOF
  exit 2
fi

if [[ -f "${target}" ]]; then
  cp -a "${target}" "${target}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
fi
install -m 0644 "${source_file}" "${target}"
nginx -t

echo "Configuration installed and syntax-tested. Nginx was not reloaded."
