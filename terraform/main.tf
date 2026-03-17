terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
  zone    = var.gcp_zone
}

# Legacy standalone VM Terraform.
# The shared source of truth for infrastructure is sales-girl-platform-infra.
resource "google_service_account" "sales_girl_vm" {
  account_id   = "sales-girl-vm-${var.environment}"
  display_name = "SalesGirl Voice Agent VM - ${var.environment}"
}

# Grant necessary permissions (minimal for now).
resource "google_project_iam_member" "sales_girl_vm_logging" {
  project = var.gcp_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.sales_girl_vm.email}"
}

# Compute Engine instance
resource "google_compute_instance" "sales_girl_vm" {
  name         = "sales-girl-voice-agent-${var.environment}"
  machine_type = var.machine_type
  zone         = var.gcp_zone

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
      size  = var.disk_size_gb
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {
      # Ephemeral public IP.
    }
  }

  service_account {
    email  = google_service_account.sales_girl_vm.email
    scopes = ["cloud-platform"]
  }

  metadata = var.ssh_public_key != "" ? {
    ssh-keys = var.ssh_public_key
  } : {}

  metadata_startup_script = templatefile("${path.module}/startup-script.sh", {
    environment        = var.environment
    repo_url          = var.repo_url
    repo_branch       = var.environment == "prod" ? "main" : var.repo_branch
    vm_user           = var.vm_user
    app_directory     = var.app_directory
    github_token      = var.github_token
  })

  tags = ["sales-girl-voice-agent", "sales-girl-${var.environment}"]

  labels = {
    environment = var.environment
    managed-by  = "terraform"
    app         = "sales-girl-voice-agent"
  }
}

# Firewall rule for HTTP (backend dashboard)
resource "google_compute_firewall" "sales_girl_http" {
  name    = "sales-girl-http-${var.environment}"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["sales-girl-${var.environment}"]
}

# Firewall rule for HTTPS (if you add Nginx later)
resource "google_compute_firewall" "sales_girl_https" {
  name    = "sales-girl-https-${var.environment}"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["443"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["sales-girl-${var.environment}"]
}

# Firewall rule for SSH (port 22).
resource "google_compute_firewall" "sales_girl_ssh" {
  name    = "sales-girl-ssh-${var.environment}"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["sales-girl-${var.environment}"]
}

# Outputs
output "vm_external_ip" {
  value       = google_compute_instance.sales_girl_vm.network_interface[0].access_config[0].nat_ip
  description = "External IP address of the VM"
}

output "vm_internal_ip" {
  value       = google_compute_instance.sales_girl_vm.network_interface[0].network_ip
  description = "Internal IP address of the VM"
}

output "dashboard_url" {
  value       = "http://${google_compute_instance.sales_girl_vm.network_interface[0].access_config[0].nat_ip}:8000/dashboard"
  description = "URL to access the appointments dashboard"
}
