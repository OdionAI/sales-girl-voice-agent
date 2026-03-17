#!/bin/bash
set -e

ENVIRONMENT="${environment}"
REPO_URL="${repo_url}"
REPO_BRANCH="${repo_branch}"
VM_USER="${vm_user}"
APP_DIR="${app_directory}"
GITHUB_TOKEN="${github_token}"

# Update system
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y

# Install required packages
apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    git \
    curl \
    wget \
    unzip \
    systemd

# Create application user if it doesn't exist
if ! id "$VM_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$VM_USER"
    usermod -aG sudo "$VM_USER"
    # Ensure .ssh directory exists and has correct permissions
    mkdir -p /home/$VM_USER/.ssh
    chmod 700 /home/$VM_USER/.ssh
    chown $VM_USER:$VM_USER /home/$VM_USER/.ssh
fi

# Create app directory
APP_PATH="/opt/sales-girl-voice-agent"
mkdir -p "$APP_PATH"
chown "$VM_USER:$VM_USER" "$APP_PATH"

# Clone or update repository
cd "$APP_PATH"

# Prepare authenticated repo URL if token is provided
if [ -n "$${GITHUB_TOKEN}" ]; then
    # Replace https://github.com/ with https://TOKEN@github.com/
    AUTH_REPO_URL=$(echo "$REPO_URL" | sed "s|https://github.com/|https://$${GITHUB_TOKEN}@github.com/|")
    echo "Using authenticated repository URL"
else
    AUTH_REPO_URL="$REPO_URL"
    echo "No GitHub token provided, using public URL"
fi

if [ -d ".git" ]; then
    # Repository exists, pull latest
    echo "Repository exists, pulling latest changes..."
    if [ -n "$${GITHUB_TOKEN}" ]; then
        # Update remote URL with token for pull
        sudo -u "$VM_USER" git remote set-url origin "$AUTH_REPO_URL"
    fi
    sudo -u "$VM_USER" git fetch origin || echo "Warning: git fetch failed"
    sudo -u "$VM_USER" git checkout "$REPO_BRANCH" || echo "Warning: git checkout failed"
    sudo -u "$VM_USER" git pull origin "$REPO_BRANCH" || echo "Warning: git pull failed"
else
    # Clone fresh
    echo "Cloning repository: $REPO_URL (branch: $REPO_BRANCH)"
    sudo -u "$VM_USER" git clone -b "$REPO_BRANCH" "$AUTH_REPO_URL" . || {
        echo "ERROR: Failed to clone repository"
        echo "Repo URL: $REPO_URL"
        echo "Branch: $REPO_BRANCH"
        echo "Check if repository is private and token is valid"
        exit 1
    }
    echo "Repository cloned successfully"
fi

# Verify clone was successful
if [ ! -d ".git" ]; then
    echo "ERROR: Repository clone failed - .git directory not found"
    exit 1
fi

echo "Repository ready at: $APP_PATH"
ls -la "$APP_PATH" | head -20

# Navigate to app directory
cd "$APP_PATH/$APP_DIR"

# Create data directory for SQLite
sudo -u "$VM_USER" mkdir -p data

# Set up Python virtual environment
if [ ! -d ".venv" ]; then
    sudo -u "$VM_USER" python3.11 -m venv .venv
fi

# Activate venv and install dependencies
sudo -u "$VM_USER" bash -c "source .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

# Note: Environment variables (.env file) should be set via:
# 1. GCP Secret Manager (recommended)
# 2. Or manually after VM creation
# The startup script doesn't create .env to avoid storing secrets in Terraform state.
# The shared sales-girl-platform-infra repo is expected to own the final runtime
# secret/bootstrap flow.

# Install systemd services (will be enabled/started by deployment script)
# The actual deployment and service management happens via GitHub Actions

echo "Startup script completed for environment: $ENVIRONMENT"
echo "Next: Set up .env file and run deployment script"
