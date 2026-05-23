#!/bin/bash
set -e

# Deployment script for SalesGirl Voice Agent
# This script is run by GitHub Actions on the VM

ENVIRONMENT="${ENVIRONMENT:-staging}"
APP_BASE="/opt/sales-girl-voice-agent"
APP_PATH="/opt/sales-girl-voice-agent"
LEGACY_APP_PATH="/opt/sales-girl-voice-agent/sales-girl-voice-agent"
VM_USER="${VM_USER:-ubuntu}"

echo "🚀 Deploying SalesGirl Voice Agent (${ENVIRONMENT})..."

# Newer VMs run the service from the root checkout, while some older startup flows
# expected a nested app directory. Deploy into whichever checkout actually exists.
if [ ! -d "$APP_PATH/.git" ] && [ -d "$LEGACY_APP_PATH/.git" ]; then
    APP_PATH="$LEGACY_APP_PATH"
fi

echo "📁 Using checkout at ${APP_PATH}"

# Wait for startup script to complete (check if directory exists)
if [ ! -d "$APP_PATH" ]; then
    echo "⏳ Waiting for startup script to complete..."
    for i in {1..30}; do
        if [ -d "$APP_PATH" ]; then
            echo "✅ Directory found!"
            break
        fi
        if [ $i -eq 30 ]; then
            echo "❌ Directory not found after 5 minutes. Checking what exists..."
            ls -la "$APP_BASE" || echo "Base directory doesn't exist"
            echo "Attempting to clone repository..."
            # Try to clone if directory doesn't exist
            mkdir -p "$APP_BASE"
            cd "$APP_BASE"
            # We need repo URL and branch - these should be in environment or we'll use defaults
            REPO_URL="${REPO_URL:-https://github.com/YOUR_ORG/sales-girl-voice-agent.git}"
            REPO_BRANCH="${REPO_BRANCH:-main}"
            if [ ! -d ".git" ]; then
                sudo -u "$VM_USER" git clone -b "$REPO_BRANCH" "$REPO_URL" .
            fi
            break
        fi
        echo "Attempt $i/30: Waiting 10 seconds..."
        sleep 10
    done
fi

# Navigate to app directory
cd "$APP_PATH" || {
    echo "❌ Failed to cd to $APP_PATH"
    echo "Current directory: $(pwd)"
    echo "Contents of /opt/sales-girl-voice-agent:"
    ls -la /opt/sales-girl-voice-agent || true
    exit 1
}

# Repair mixed ownership from earlier manual/root-managed deploys before Git updates.
chown -R "$VM_USER:$VM_USER" "$APP_PATH"
sudo -u "$VM_USER" git config --global --add safe.directory "$APP_PATH"

# Pull latest code from the requested branch.
TARGET_BRANCH="${REPO_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
sudo -u "$VM_USER" git fetch origin
sudo -u "$VM_USER" git checkout "${TARGET_BRANCH}"
sudo -u "$VM_USER" git pull origin "${TARGET_BRANCH}"

# Ensure data directory exists
sudo -u "$VM_USER" mkdir -p data

# Update Python dependencies
echo "📦 Installing Python dependencies..."
sudo -u "$VM_USER" bash -c "source .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

# Ensure .env file exists (should be created via Secret Manager or manually)
if [ ! -f ".env" ]; then
    echo "⚠️  Warning: .env file not found. Please create it with required environment variables."
    echo "   Required vars: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, DEEPGRAM_API_KEY, GOOGLE_API_KEY"
fi

# Reload systemd daemon
systemctl daemon-reload

# Restart services
echo "🔄 Restarting services..."
systemctl restart sales-girl-backend || echo "⚠️  Backend service not found (will be created)"
systemctl restart sales-girl-agent-en || echo "⚠️  English agent service not found (will be created)"
systemctl restart sales-girl-agent-fr || echo "⚠️  French agent service not found (will be created)"

# Enable services if not already enabled
systemctl enable sales-girl-backend sales-girl-agent-en sales-girl-agent-fr 2>/dev/null || true

# Wait a moment for services to start
sleep 3

# Check service status
echo "📊 Service status:"
systemctl status sales-girl-backend --no-pager -l || true
systemctl status sales-girl-agent-en --no-pager -l || true
systemctl status sales-girl-agent-fr --no-pager -l || true

echo "✅ Deployment completed for ${ENVIRONMENT}"
