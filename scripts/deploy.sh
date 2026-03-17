#!/bin/bash
set -e

# Deployment script for SalesGirl Voice Agent
# This script is run by GitHub Actions on the VM

ENVIRONMENT="${ENVIRONMENT:-staging}"
APP_BASE="/opt/sales-girl-voice-agent"
APP_PATH="/opt/sales-girl-voice-agent/sales-girl-voice-agent"
VM_USER="${VM_USER:-salesgirl}"

echo "🚀 Deploying SalesGirl Voice Agent (${ENVIRONMENT})..."

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
            if [ ! -d "sales-girl-voice-agent" ]; then
                echo "❌ Repository cloned but sales-girl-voice-agent directory not found"
                ls -la
                exit 1
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

# Pull latest code (if not already done by startup script)
sudo -u "$VM_USER" git fetch origin
sudo -u "$VM_USER" git checkout "$(git rev-parse --abbrev-ref HEAD)"
sudo -u "$VM_USER" git pull origin "$(git rev-parse --abbrev-ref HEAD)"

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
