#!/bin/bash
# Deploy Odion Voice Agent to GCP VM from your local machine (no GitHub Actions).
# Usage: ./scripts/deploy-from-cli.sh [prod|staging]

set -e

ENVIRONMENT="${1:-prod}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="$REPO_ROOT/terraform"

# VM connection (override with env vars)
VM_USER="${VM_USER:-odion}"
VM_IP="${VM_IP:-}"
REPO_URL="${REPO_URL:-}"
REPO_BRANCH="${REPO_BRANCH:-}"

if [ "$ENVIRONMENT" != "prod" ] && [ "$ENVIRONMENT" != "staging" ]; then
  echo "Usage: $0 [prod|staging]"
  exit 1
fi

echo "🚀 Deploying to GCP VM (${ENVIRONMENT})..."

# Resolve VM IP (Terraform reads GCS state; set GOOGLE_APPLICATION_CREDENTIALS if using GCS backend)
if [ -z "$VM_IP" ]; then
  if [ -d "$TERRAFORM_DIR" ]; then
    echo "Getting VM IP from Terraform..."
    # Only capture stdout; terraform writes warnings to stderr
    VM_IP=$(cd "$TERRAFORM_DIR" && terraform output -raw vm_external_ip 2>/dev/null) || true
  fi
  # Trim whitespace and reject if it looks like Terraform error output
  VM_IP=$(echo "$VM_IP" | tr -d '\n\r\t' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ -z "$VM_IP" ] || [[ ! "$VM_IP" =~ ^[0-9a-zA-Z._:-]+$ ]]; then
    VM_IP=""
    echo "Terraform state has no VM output (or backend not initialized for this env)."
    echo "Get your VM IP from GCP Console or run: gcloud compute instances list --filter='name~odion-voice-agent' --format='get(networkInterfaces[0].accessConfigs[0].natIP)'"
    read -p "Enter VM IP address: " VM_IP
    VM_IP=$(echo "$VM_IP" | tr -d '\n\r\t' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  fi
fi

if [ -z "$VM_IP" ] || [[ ! "$VM_IP" =~ ^[0-9a-zA-Z._:-]+$ ]]; then
  echo "❌ VM_IP is required and must be a valid hostname or IP (e.g. 34.x.x.x)"
  exit 1
fi

# Repo URL/branch for deploy script on VM
if [ -z "$REPO_URL" ]; then
  ORIGIN="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null)"
  if [[ "$ORIGIN" =~ github.com[:/]([^/]+/[^/]+) ]]; then
    REPO_URL="https://github.com/${BASH_REMATCH[1]%.git}.git"
  else
    REPO_URL="https://github.com/OdionAI/odion-voice-agent.git"
  fi
fi
if [ -z "$REPO_BRANCH" ]; then
  if [ "$ENVIRONMENT" = "prod" ]; then
    REPO_BRANCH="main"
  else
    REPO_BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null)" || REPO_BRANCH="main"
  fi
fi

echo "  VM: $VM_USER@$VM_IP"
echo "  Repo: $REPO_URL (branch: $REPO_BRANCH)"
echo ""

# Remote deploy script (heredoc) - clone if empty, then deploy
run_remote_deploy() {
  cat << 'REMOTE'
APP_PATH="/opt/odion-voice-agent"
APP_SUBDIR="odion-voice-agent"   # app is inside this subdir in the repo

# Check if repo is cloned (look for .git or any files)
if [ ! -d "$APP_PATH/.git" ] && [ "$(ls -A $APP_PATH 2>/dev/null | wc -l)" -eq 0 ]; then
  echo "📦 Repository not cloned. Cloning now..."
  cd "$APP_PATH"
  
  # Build authenticated URL if GITHUB_TOKEN is set
  if [ -n "$GITHUB_TOKEN" ]; then
    AUTH_URL=$(echo "$REPO_URL" | sed "s|https://github.com/|https://${GITHUB_TOKEN}@github.com/|")
  else
    AUTH_URL="$REPO_URL"
  fi
  
  sudo -u "${VM_USER:-odion}" git clone -b "${REPO_BRANCH:-main}" "$AUTH_URL" . || {
    echo "❌ Failed to clone repository."
    echo "   If repo is private, set GITHUB_TOKEN before running deploy."
    exit 1
  }
  echo "✅ Repository cloned."
fi

# Determine app path (subdir or root)
if [ -d "$APP_PATH/$APP_SUBDIR" ]; then
  WORK_PATH="$APP_PATH/$APP_SUBDIR"
else
  WORK_PATH="$APP_PATH"
fi

cd "$WORK_PATH" || { echo "❌ Could not cd to $WORK_PATH"; exit 1; }
echo "📂 Working in: $WORK_PATH"

# Pull latest
echo "🔄 Pulling latest code..."
sudo -u "${VM_USER:-odion}" git fetch origin 2>/dev/null || true
sudo -u "${VM_USER:-odion}" git pull origin "${REPO_BRANCH:-main}" 2>/dev/null || true

# Create venv if missing
if [ ! -d ".venv" ]; then
  echo "🐍 Creating Python virtual environment..."
  sudo -u "${VM_USER:-odion}" python3.11 -m venv .venv || sudo -u "${VM_USER:-odion}" python3 -m venv .venv
fi

# Install dependencies
echo "📦 Installing Python dependencies..."
sudo -u "${VM_USER:-odion}" bash -c "source .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

# Restart services (if they exist)
echo "🔄 Restarting services..."
sudo systemctl daemon-reload 2>/dev/null || true
sudo systemctl restart odion-backend 2>/dev/null || echo "⚠️  odion-backend service not found"
sudo systemctl restart odion-agent-en 2>/dev/null || echo "⚠️  odion-agent-en service not found"
sudo systemctl restart odion-agent-fr 2>/dev/null || echo "⚠️  odion-agent-fr service not found"

echo "✅ Deploy complete on VM."
REMOTE
}

# GitHub token for private repos (optional; set GITHUB_TOKEN env var)
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

REMOTE_ENV="export ENVIRONMENT=$ENVIRONMENT REPO_URL='$REPO_URL' REPO_BRANCH='$REPO_BRANCH' VM_USER=$VM_USER GITHUB_TOKEN='$GITHUB_TOKEN'"

# Prefer gcloud compute ssh if USE_GCLOUD_SSH=1 or key-based SSH not set up
USE_GCLOUD_SSH="${USE_GCLOUD_SSH:-}"
if [ -z "$USE_GCLOUD_SSH" ]; then
  if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5 "$VM_USER@$VM_IP" "echo ok" 2>/dev/null; then
    USE_GCLOUD_SSH="0"
  else
    USE_GCLOUD_SSH="1"
    echo "Key-based SSH not available. Using gcloud compute ssh..."
  fi
fi

if [ "$USE_GCLOUD_SSH" = "1" ]; then
  # Resolve project from key file and ensure gcloud uses this account
  GCP_KEY="${GOOGLE_APPLICATION_CREDENTIALS:-$REPO_ROOT/github-actions-key.json}"
  if [ ! -f "$GCP_KEY" ]; then
    echo "❌ Set GOOGLE_APPLICATION_CREDENTIALS to your service account key (needed for gcloud compute ssh)."
    exit 1
  fi
  GCP_PROJECT="${GCP_PROJECT_ID:-$(grep -o '"project_id": *"[^"]*"' "$GCP_KEY" 2>/dev/null | cut -d'"' -f4)}"
  if [ -z "$GCP_PROJECT" ]; then
    echo "❌ Could not get GCP project. Set GCP_PROJECT_ID or use a key file with project_id."
    exit 1
  fi
  gcloud auth activate-service-account --key-file="$GCP_KEY" --project="$GCP_PROJECT" 2>/dev/null || true
  # Find instance name and zone by IP (zone may be full URL; we need short name e.g. us-central1-a)
  INSTANCE_LINE=$(gcloud compute instances list --filter="networkInterfaces[0].accessConfigs[0].natIP=$VM_IP" --format="get(name,zone)" --project="$GCP_PROJECT" 2>/dev/null) || true
  if [ -z "$INSTANCE_LINE" ]; then
    echo "❌ Could not find VM with IP $VM_IP in project $GCP_PROJECT."
    echo "   Set GCP_INSTANCE_NAME and GCP_ZONE (e.g. odion-voice-agent-prod, us-central1-a) to use gcloud ssh."
    exit 1
  fi
  GCP_INSTANCE="${GCP_INSTANCE_NAME:-$(echo "$INSTANCE_LINE" | awk '{print $1}')}"
  ZONE_RAW="$(echo "$INSTANCE_LINE" | awk '{print $2}')"
  # Zone may be full URL; extract short name (e.g. us-central1-a)
  GCP_ZONE="${GCP_ZONE:-$(echo "$ZONE_RAW" | sed 's|.*/||')}"
  echo "  Using: gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --project=$GCP_PROJECT"
  run_remote_deploy | gcloud compute ssh "$GCP_INSTANCE" --zone="$GCP_ZONE" --project="$GCP_PROJECT" --command "$REMOTE_ENV; bash -s"
else
  run_remote_deploy | ssh -o StrictHostKeyChecking=no "$VM_USER@$VM_IP" "$REMOTE_ENV; bash -s"
fi

echo ""
echo "✅ Deployment complete."
echo "   Dashboard: http://${VM_IP}:8000/dashboard"
echo "   Health:    http://${VM_IP}:8000/health"
