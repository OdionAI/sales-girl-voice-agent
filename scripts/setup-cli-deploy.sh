#!/bin/bash
# One-time setup for CLI deployment (Option 1).
# Run from: odion-voice-agent/
# Usage: ./scripts/setup-cli-deploy.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="$REPO_ROOT/terraform"

# Path to GCP service account key (for Terraform and gcloud)
GCP_KEY_PATH="${GCP_KEY_PATH:-$REPO_ROOT/github-actions-key.json}"

echo "=== Odion Voice Agent – CLI deploy setup ==="
echo ""

# 1. Check key file
if [ ! -f "$GCP_KEY_PATH" ]; then
  echo "❌ GCP key not found at: $GCP_KEY_PATH"
  echo "   Set GCP_KEY_PATH or place key at: $REPO_ROOT/github-actions-key.json"
  exit 1
fi
echo "✅ GCP key found: $GCP_KEY_PATH"

# 2. Activate service account for gcloud
echo ""
echo "Activating GCP service account..."
gcloud auth activate-service-account --key-file="$GCP_KEY_PATH"
export CLOUDSDK_CORE_PROJECT=$(gcloud config get-value project 2>/dev/null || true)
if [ -z "$CLOUDSDK_CORE_PROJECT" ]; then
  PROJECT_ID=$(grep -o '"project_id": *"[^"]*"' "$GCP_KEY_PATH" | cut -d'"' -f4)
  gcloud config set project "$PROJECT_ID"
  echo "   Project set to: $PROJECT_ID"
fi

# 3. Terraform init with backend (uses same key via GOOGLE_APPLICATION_CREDENTIALS)
echo ""
echo "Initializing Terraform (GCS backend)..."
export GOOGLE_APPLICATION_CREDENTIALS="$GCP_KEY_PATH"
cd "$TERRAFORM_DIR"

# Backend config: use same bucket/prefix as in backend.tf (or override with env)
TF_BUCKET="${TF_STATE_BUCKET:-odion-voice-agent1-terraform-state}"
TF_PREFIX_PROD="${TF_PREFIX_PROD:-odion-voice-agent1/prod}"

terraform init \
  -backend-config="bucket=$TF_BUCKET" \
  -backend-config="prefix=$TF_PREFIX_PROD" \
  -reconfigure

echo "✅ Terraform initialized (prefix: $TF_PREFIX_PROD)"
terraform output -raw vm_external_ip >/dev/null 2>&1 && echo "✅ VM IP from Terraform: $(terraform output -raw vm_external_ip)" || echo "⚠️  No VM in state yet (run terraform apply first if needed)"

cd "$REPO_ROOT"
echo ""

# 4. SSH: ensure you can reach the VM
VM_IP=$(cd "$TERRAFORM_DIR" && terraform output -raw vm_external_ip 2>/dev/null) || true
if [ -n "$VM_IP" ]; then
  echo "Checking SSH access to VM ($VM_IP)..."
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes odion@"$VM_IP" "echo ok" 2>/dev/null; then
    echo "✅ SSH (key-based) works for odion@$VM_IP"
  else
    echo "⚠️  Key-based SSH to odion@$VM_IP failed or not set up."
    echo "   You can either:"
    echo "   - Add your SSH public key to the VM (user odion):"
    echo "     gcloud compute ssh odion-voice-agent-prod --zone=ZONE --project=PROJECT_ID"
    echo "     Then on VM: echo 'YOUR_PUBKEY' >> /home/odion/.ssh/authorized_keys"
    echo "   - Or use gcloud to deploy instead of key-based SSH (see DEPLOY_FROM_CLI.md)."
  fi
else
  echo "⚠️  No VM IP in Terraform state. After first 'terraform apply', re-run this script to verify SSH."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Deploy with (from $REPO_ROOT):"
echo "  export GOOGLE_APPLICATION_CREDENTIALS=\"$GCP_KEY_PATH\""
echo "  ./scripts/deploy-from-cli.sh prod"
echo ""
echo "Or in one line:"
echo "  GOOGLE_APPLICATION_CREDENTIALS=\"$GCP_KEY_PATH\" ./scripts/deploy-from-cli.sh prod"
echo ""
