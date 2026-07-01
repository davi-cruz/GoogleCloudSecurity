terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.0.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0.0"
    }
  }

  # backend "gcs" {
  #   bucket  = "my-company-secops-tfstate"
  #   prefix  = "terraform/secops-monitoring/state"
  # }
}

# Provider configurations for project division
provider "google" {
  project = var.orchestration_project_id
  region  = var.region
}

provider "google" {
  alias   = "byop"
  project = var.byop_project_id
  region  = var.region
}

# ==============================================================================
# SECTION A: STORAGE & DATA LOOKUPS (Orchestration Project)
# ==============================================================================

# Look up pre-existing Secret holding the SOAR Webhook URL (provisioned in Step 3)
data "google_secret_manager_secret" "soar_webhook_url" {
  secret_id = "secops-soar-webhook-url"
}

# Fetch the active version of the secret payload
data "google_secret_manager_secret_version" "soar_webhook_url_version" {
  secret = data.google_secret_manager_secret.soar_webhook_url.id
}

# Configuration Bucket to store outputs (e.g. terraform.tfvars.json)
resource "google_storage_bucket" "config_bucket" {
  name                        = "${var.orchestration_project_id}-secops-configs"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  versioning {
    enabled = true
  }
}

# Deployment Artifacts Bucket to store source code zip files
resource "google_storage_bucket" "source_bucket" {
  name                        = "${var.orchestration_project_id}-secops-deployments"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
}

# Archive scripts folder into a single ZIP artifact
data "archive_file" "scripts_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../scripts"
  output_path = "${path.module}/scripts_deployment.zip"
}

# Upload ZIP code package to source bucket
resource "google_storage_bucket_object" "scripts_object" {
  name   = "scripts-${data.archive_file.scripts_zip.output_md5}.zip"
  bucket = google_storage_bucket.source_bucket.name
  source = data.archive_file.scripts_zip.output_path
}

# ==============================================================================
# SECTION B: CLOUD RUN FUNCTIONS (Orchestration Project)
# ==============================================================================

# Create IAM Service Account for running the Functions
resource "google_service_account" "function_sa" {
  account_id   = "secops-monitoring-sa"
  display_name = "SecOps Monitoring Service Account"
}

# Give Service Account access to read metrics in the BYOP Project
resource "google_project_iam_member" "byop_metrics_viewer" {
  provider = google.byop
  project  = var.byop_project_id
  role     = "roles/monitoring.viewer"
  member   = "serviceAccount:${google_service_account.function_sa.email}"
}

# Give Service Account access to write outputs to GCS config bucket
resource "google_project_iam_member" "storage_admin_orchestration" {
  project = var.orchestration_project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.function_sa.email}"
}

# Give Service Account access to retrieve Webhook secrets dynamically
resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  secret_id = data.google_secret_manager_secret.soar_webhook_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.function_sa.email}"
}

# Deploy SLA Profiler Function
resource "google_cloudfunctions2_function" "sla_profiler" {
  name        = "secops-sla-profiler"
  location    = var.region
  description = "Queries metrics and auto-updates variables in GCS config bucket"

  build_config {
    runtime     = "python310"
    entry_point = "main_http"
    source {
      storage_source {
        bucket = google_storage_bucket.source_bucket.name
        object = google_storage_bucket_object.scripts_object.name
      }
    }
  }

  service_config {
    max_instance_count = 1
    available_memory   = "256Mi"
    timeout_seconds    = 300
    service_account_email = google_service_account.function_sa.email
    
    environment_variables = {
      GCP_PROJECT_ID    = var.byop_project_id
      OUTPUT_GCS_BUCKET = google_storage_bucket.config_bucket.name
      OUTPUT_GCS_BLOB   = "terraform.tfvars.json"
    }
  }
}

# Deploy Forecast Engine Function
resource "google_cloudfunctions2_function" "forecast_engine" {
  name        = "secops-forecast-engine"
  location    = var.region
  description = "Aggregates contract ingestion and projects runways"

  build_config {
    runtime     = "python310"
    entry_point = "main_http"
    source {
      storage_source {
        bucket = google_storage_bucket.source_bucket.name
        object = google_storage_bucket_object.scripts_object.name
      }
    }
  }

  service_config {
    max_instance_count = 1
    available_memory   = "256Mi"
    timeout_seconds    = 300
    service_account_email = google_service_account.function_sa.email
    
    environment_variables = {
      GCP_PROJECT_ID      = var.byop_project_id
      OUTPUT_GCS_BUCKET   = google_storage_bucket.config_bucket.name
      OUTPUT_FORECAST_BLOB = "forecast_vars.json"
      CONTRACT_TERMS_JSON = var.contract_terms_json
    }
  }
}

# ==============================================================================
# SECTION C: CRON SCHEDULER JOBS (Orchestration Project)
# ==============================================================================

# Weekly Trigger for SLA Profiler (Sunday at Midnight)
resource "google_cloud_scheduler_job" "profiler_scheduler" {
  name             = "secops-profiler-weekly-trigger"
  description      = "Triggers the SLA Ingestion Profiler Function every Sunday at midnight"
  schedule         = "0 0 * * 0"
  time_zone        = "UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.sla_profiler.service_config[0].uri
    
    oidc_token {
      service_account_email = google_service_account.function_sa.email
    }
  }
}

# Daily Trigger for Forecast Engine (Daily at 1:00 AM)
resource "google_cloud_scheduler_job" "forecast_scheduler" {
  name             = "secops-forecast-daily-trigger"
  description      = "Triggers the Consumption Forecast Function every day at 1:00 AM"
  schedule         = "0 1 * * *"
  time_zone        = "UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.forecast_engine.service_config[0].uri
    
    oidc_token {
      service_account_email = google_service_account.function_sa.email
    }
  }
}

# ==============================================================================
# SECTION D: ALERT POLICIES & NOTIFICATION CHANNELS (BYOP Project)
# ==============================================================================

# Configures the Webhook Notification Channel in the BYOP project
resource "google_monitoring_notification_channel" "soar_webhook" {
  provider     = google.byop
  display_name = "SecOps SOAR Webhook Gateway"
  type         = "webhook_tokenauth"
  labels = {
    url = data.google_secret_manager_secret_version.soar_webhook_url_version.secret_data
  }
  user_labels = {
    target = "secops-soar"
  }
}

# Dynamic Alert Policies per Log Feed (Metric Absence alerts matching SLA)
resource "google_monitoring_alert_policy" "log_feed_absence" {
  provider     = google.byop
  for_each     = var.monitors
  display_name = "SecOps Log Ingestion Absence - ${each.value.log_type}"
  combiner     = "OR"

  conditions {
    display_name = "No records seen for ${each.value.log_type}"
    condition_absent {
      filter   = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND resource.labels.log_type = \"${each.value.log_type}\""
      duration = "${each.value.alert_window_seconds}s"
      
      trigger {
        count = 1
      }
      
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project_id", "resource.labels.log_type"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]
  
  documentation {
    content   = "Log feed `${each.value.log_type}` went silent for more than its SLA of ${each.value.alert_window_seconds} seconds. Please verify the active feeds page in SecOps. Reference: SOP-SILENT-SOURCE."
    mime_type = "text/markdown"
  }
  
  user_labels = {
    severity = "high"
    log_type = each.value.log_type
  }
}

# Alert Policy for BindPlane Collection Agent Outage
resource "google_monitoring_alert_policy" "bindplane_agent_outage" {
  provider     = google.byop
  display_name = "SecOps Bindplane Agent Outage"
  combiner     = "OR"

  conditions {
    display_name = "No traces processed by BindPlane Agent"
    condition_absent {
      filter   = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/agent/exporter_accepted_spans_count\""
      duration = "3600s"
      
      trigger {
        count = 1
      }
      
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MEAN"
        cross_series_reducer = "REDUCE_MEAN"
        group_by_fields      = ["project_id", "resource.labels.collector_id"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]
  
  documentation {
    content   = "BindPlane Collection Agent has stopped emitting metrics. The collector may be offline or experiencing local buffer overflows. Reference: SOP-COLLECTOR-OUTAGE."
    mime_type = "text/markdown"
  }
  
  user_labels = {
    severity = "critical"
  }
}

# Alert Policy for Parser Degradation / Normalization Errors
resource "google_monitoring_alert_policy" "parser_degradation" {
  provider     = google.byop
  display_name = "SecOps Normalization Parser Degradation Alert"
  combiner     = "OR"

  conditions {
    display_name = "Parser failure ratio > 5% on parsed logs"
    condition_threshold {
      filter          = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/normalizer/log/record_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.05
      duration        = "900s"
      
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project_id", "resource.labels.log_type"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]
  
  documentation {
    content   = "Parser failure ratio exceeds 5% in the last 15 minutes. This suggests a parser layout shift or broken format structure in parsed logs. Reference: SOP-PARSER-DEGRADATION."
    mime_type = "text/markdown"
  }
  
  user_labels = {
    severity = "high"
  }
}

# Alert Policy for Ingestion Quota Reaching Capacity
resource "google_monitoring_alert_policy" "ingestion_quota_warning" {
  provider     = google.byop
  display_name = "SecOps Ingestion Quota Approaching Limit"
  combiner     = "OR"

  conditions {
    display_name = "Ingestion rate is above 80% of configured daily quota"
    condition_matched_log {
      filter = "resource.type = \"chronicle.googleapis.com/Collector\""
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]
  
  documentation {
    content   = "Daily ingestion has reached 80% of total contracted license quota. Overage billing charges will apply soon if ingestion trajectory continues. Reference: SOP-QUOTA-MANAGEMENT."
    mime_type = "text/markdown"
  }
  
  user_labels = {
    severity = "critical"
  }
}
