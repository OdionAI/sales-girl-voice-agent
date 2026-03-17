# Terraform backend configuration
# Copy this to backend.tf and configure your GCS bucket for state storage

terraform {
  backend "gcs" {
    bucket = "odion-voice-agent1-terraform-state"
    prefix = "odion-voice-agent1/staging"  # or "odion-voice-agent1/prod"
  }
}

# To use this
# 1. Create a GCS bucket: gsutil mb gs://your-terraform-state-bucket
# 2. Enable versioning: gsutil versioning set on gs://your-terraform-state-bucket
# 3. Copy this file to backend.tf
# 4. Update bucket name and prefix
# 5. For staging: prefix = "odion-voice-agent/staging"
# 6. For production: prefix = "odion-voice-agent/prod"
