variable "gcp_project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "gcp_region" {
  description = "GCP Region"
  type        = string
  default     = "us-central1"
}

variable "gcp_zone" {
  description = "GCP Zone"
  type        = string
  default     = "us-central1-a"
}

variable "environment" {
  description = "Environment name (staging or prod)"
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "Environment must be either 'staging' or 'prod'."
  }
}

variable "machine_type" {
  description = "GCE machine type"
  type        = string
  default     = "e2-small"
}

variable "disk_size_gb" {
  description = "Boot disk size in GB"
  type        = number
  default     = 20
}

variable "repo_url" {
  description = "GitHub repository URL (HTTPS)"
  type        = string
  default     = "https://github.com/YOUR_ORG/sales-girl-voice-agent.git"
}

variable "repo_branch" {
  description = "Git branch to deploy (for staging, usually the branch name)"
  type        = string
  default     = "main"
}

variable "vm_user" {
  description = "Username for the VM (will be created if doesn't exist)"
  type        = string
  default     = "salesgirl"
}

variable "app_directory" {
  description = "Directory path inside repo where the app lives"
  type        = string
  default     = "sales-girl-voice-agent"
}

variable "ssh_public_key" {
  description = "SSH public key for VM access (format: 'user:key' or just 'key' for default user)"
  type        = string
  default     = ""
}

variable "github_token" {
  description = "GitHub personal access token for cloning private repositories (sensitive)"
  type        = string
  default     = ""
  sensitive   = true
}
