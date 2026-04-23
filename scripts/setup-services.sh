#!/bin/bash
set -e

# Setup systemd services for SalesGirl Voice Agent
# Run this once after VM creation (or as part of deployment)

# Detect app path: repo may be cloned to /opt/sales-girl-voice-agent (flat) or nested.
ROOT="/opt/sales-girl-voice-agent"
if [ -f "${ROOT}/sales-girl-voice-agent/main.py" ] && [ -d "${ROOT}/sales-girl-voice-agent/backend" ]; then
  APP_PATH="${ROOT}/sales-girl-voice-agent"
elif [ -f "${ROOT}/main.py" ] && [ -d "${ROOT}/backend" ]; then
  APP_PATH="${ROOT}"
else
  echo "❌ Could not find app (main.py + backend/) under ${ROOT}"
  exit 1
fi
VM_USER="${VM_USER:-salesgirl}"

if [ -f "${APP_PATH}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${APP_PATH}/.env"
  set +a
fi

ENABLE_FRENCH_AGENT="${ENABLE_FRENCH_AGENT:-false}"
DEFAULT_RUNTIME_AGENT_NAME_EN="${DEFAULT_RUNTIME_AGENT_NAME_EN:-sales-girl-agent-en}"
DEFAULT_RUNTIME_AGENT_NAME_FR="${DEFAULT_RUNTIME_AGENT_NAME_FR:-sales-girl-agent-fr}"

echo "🔧 Setting up systemd services (APP_PATH=${APP_PATH})..."

# Backend service
cat > /tmp/sales-girl-backend.service <<EOF
[Unit]
Description=SalesGirl Voice Agent - FastAPI Backend
After=network.target

[Service]
Type=simple
User=${VM_USER}
WorkingDirectory=${APP_PATH}
EnvironmentFile=${APP_PATH}/.env
Environment="PATH=${APP_PATH}/.venv/bin"
ExecStart=${APP_PATH}/.venv/bin/uvicorn backend.api:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# English agent service
cat > /tmp/sales-girl-agent-en.service <<EOF
[Unit]
Description=SalesGirl Voice Agent - ${DEFAULT_RUNTIME_AGENT_NAME_EN} (English)
After=network.target

[Service]
Type=simple
User=${VM_USER}
WorkingDirectory=${APP_PATH}
EnvironmentFile=${APP_PATH}/.env
Environment="AGENT_NAME=${DEFAULT_RUNTIME_AGENT_NAME_EN}"
Environment="AGENT_PORT=8081"
Environment="PATH=${APP_PATH}/.venv/bin"
ExecStart=${APP_PATH}/.venv/bin/python main.py start
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# French agent service
cat > /tmp/sales-girl-agent-fr.service <<EOF
[Unit]
Description=SalesGirl Voice Agent - ${DEFAULT_RUNTIME_AGENT_NAME_FR} (French)
After=network.target

[Service]
Type=simple
User=${VM_USER}
WorkingDirectory=${APP_PATH}
EnvironmentFile=${APP_PATH}/.env
Environment="AGENT_NAME=${DEFAULT_RUNTIME_AGENT_NAME_FR}"
Environment="AGENT_PORT=8082"
Environment="PATH=${APP_PATH}/.venv/bin"
ExecStart=${APP_PATH}/.venv/bin/python main.py start
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Install services
cp /tmp/sales-girl-backend.service /etc/systemd/system/
cp /tmp/sales-girl-agent-en.service /etc/systemd/system/
if [ "${ENABLE_FRENCH_AGENT}" = "true" ]; then
  cp /tmp/sales-girl-agent-fr.service /etc/systemd/system/
else
  rm -f /etc/systemd/system/sales-girl-agent-fr.service
fi

# Reload systemd
systemctl daemon-reload

# Enable services
systemctl enable sales-girl-backend sales-girl-agent-en
if [ "${ENABLE_FRENCH_AGENT}" = "true" ]; then
  systemctl enable sales-girl-agent-fr
else
  systemctl disable sales-girl-agent-fr 2>/dev/null || true
fi

# Restart services so they rebind to the freshly cloned checkout.
# A plain "start" is a no-op when the units are already running, which can
# leave the workers attached to a deleted working directory during redeploys.
systemctl restart sales-girl-backend sales-girl-agent-en
if [ "${ENABLE_FRENCH_AGENT}" = "true" ]; then
  systemctl restart sales-girl-agent-fr
else
  systemctl stop sales-girl-agent-fr 2>/dev/null || true
fi

echo "✅ Systemd services installed and started (ENABLE_FRENCH_AGENT=${ENABLE_FRENCH_AGENT})"
