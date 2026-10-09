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

  backend "gcs" {
    bucket = "dc-secops-lab-tfstate"
    prefix = "secops-monitoring"
  }
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

# Secret Manager Secret (Managed or Pre-existing)
resource "google_secret_manager_secret" "soar_webhook_url_managed" {
  count     = var.create_webhook_secret ? 1 : 0
  secret_id = "secops-soar-webhook-url"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "soar_webhook_url_version_managed" {
  count       = var.create_webhook_secret ? 1 : 0
  secret      = google_secret_manager_secret.soar_webhook_url_managed[0].id
  secret_data = var.webhook_url
}

data "google_secret_manager_secret" "soar_webhook_url" {
  count     = var.create_webhook_secret ? 0 : 1
  secret_id = "secops-soar-webhook-url"
}

data "google_secret_manager_secret_version" "soar_webhook_url_version" {
  count  = var.create_webhook_secret ? 0 : 1
  secret = data.google_secret_manager_secret.soar_webhook_url[0].id
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

locals {
  all_byop_projects = distinct(compact(concat([var.byop_project_id], var.byop_project_ids)))
}

# Give Service Account access to read metrics in all BYOP Projects (Multi-Tenant Support)
resource "google_project_iam_member" "byop_metrics_viewer" {
  for_each = toset(local.all_byop_projects)
  project  = each.value
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
  secret_id = var.create_webhook_secret ? google_secret_manager_secret.soar_webhook_url_managed[0].id : data.google_secret_manager_secret.soar_webhook_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.function_sa.email}"
}

# Deploy Unified Monitoring Orchestration Cloud Run Function
resource "google_cloudfunctions2_function" "secops_orchestrator" {
  name        = "secops-monitoring-orchestrator"
  location    = var.region
  description = "Unified Orchestration Engine for SecOps Log Profiler, Consumption Forecast, and Drift Detector"

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
    max_instance_count    = 1
    available_memory      = "256Mi"
    timeout_seconds       = 300
    service_account_email = google_service_account.function_sa.email

    environment_variables = {
      GCP_PROJECT_ID       = var.byop_project_id
      BYOP_PROJECT_ID      = var.byop_project_id
      OUTPUT_GCS_BUCKET    = google_storage_bucket.config_bucket.name
      OUTPUT_GCS_BLOB      = "terraform.tfvars.json"
      OUTPUT_FORECAST_BLOB = "forecast_vars.json"
      CONTRACT_TERMS_JSON  = var.contract_terms_json
    }

    dynamic "secret_environment_variables" {
      for_each = var.create_webhook_secret || var.webhook_url != "" ? [1] : []
      content {
        key        = "SOAR_WEBHOOK_URL"
        project_id = var.orchestration_project_id
        secret     = var.create_webhook_secret ? google_secret_manager_secret.soar_webhook_url_managed[0].secret_id : data.google_secret_manager_secret.soar_webhook_url[0].secret_id
        version    = "latest"
      }
    }
  }
}

# Allow Cloud Scheduler to invoke the Cloud Run Function via function_sa OIDC token
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.orchestration_project_id
  location = google_cloudfunctions2_function.secops_orchestrator.location
  name     = google_cloudfunctions2_function.secops_orchestrator.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.function_sa.email}"
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
    uri         = google_cloudfunctions2_function.secops_orchestrator.service_config[0].uri
    body        = base64encode("{\"action\": \"profiler\"}")
    headers     = { "Content-Type" = "application/json" }

    oidc_token {
      service_account_email = google_service_account.function_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
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
    uri         = google_cloudfunctions2_function.secops_orchestrator.service_config[0].uri
    body        = base64encode("{\"action\": \"forecast\"}")
    headers     = { "Content-Type" = "application/json" }

    oidc_token {
      service_account_email = google_service_account.function_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}

# Daily Trigger for Configuration Drift Detector (Daily at 2:00 AM)
resource "google_cloud_scheduler_job" "drift_scheduler" {
  name             = "secops-drift-daily-trigger"
  description      = "Triggers the Configuration Drift Detector Function every day at 2:00 AM"
  schedule         = "0 2 * * *"
  time_zone        = "UTC"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions2_function.secops_orchestrator.service_config[0].uri
    body        = base64encode("{\"action\": \"drift\"}")
    headers     = { "Content-Type" = "application/json" }

    oidc_token {
      service_account_email = google_service_account.function_sa.email
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}

# ==============================================================================
# ==============================================================================
# SECTION D: ALERT POLICIES & NOTIFICATION CHANNELS (BYOP Project)
# Categorized into: 1. Ingestion | 2. Parsing | 3. Billing
# ==============================================================================

# Configures the Webhook Notification Channel in the BYOP project
resource "google_monitoring_notification_channel" "soar_webhook" {
  provider     = google.byop
  display_name = "SecOps SOAR Webhook Gateway"
  type         = "webhook_tokenauth"
  labels = {
    url = var.create_webhook_secret ? google_secret_manager_secret_version.soar_webhook_url_version_managed[0].secret_data : data.google_secret_manager_secret_version.soar_webhook_url_version[0].secret_data
  }
  user_labels = {
    target = "secops-soar"
  }
}

# ------------------------------------------------------------------------------
# CATEGORY 1: INGESTION ALERTS
# ------------------------------------------------------------------------------

# Dynamic Alert Policies per Log Feed (Metric Absence alerts matching SLA)
# Note: Google Cloud Monitoring condition_absent enforces a max duration of 23h30m (84600s) and full minute multiples.
# Feeds with gap > 84600s are low-frequency / variable feeds where continuous absence alerting creates false positives.
resource "google_monitoring_alert_policy" "log_feed_absence" {
  provider     = google.byop
  for_each     = { for k, v in var.monitors : k => v if !lookup(v, "ignore", false) && v.alert_window_seconds <= 84600 }
  display_name = "[Ingestion] SecOps Log Ingestion Absence - ${each.value.log_type}"
  combiner     = "OR"

  conditions {
    display_name = "No records seen for ${each.value.log_type}"
    condition_absent {
      filter   = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND resource.labels.log_type = \"${each.value.log_type}\""
      duration = "${floor(min(84600, max(300, each.value.alert_window_seconds)) / 60) * 60}s"

      trigger {
        count = 1
      }

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project", "resource.labels.log_type"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Log feed `${each.value.log_type}` went silent for more than its dynamic SLA of ${coalesce(each.value.alert_window_human, "${each.value.alert_window_seconds}s")}. Please verify feed and collector health. Reference: SOP-SILENT-SOURCE."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "ingestion"
    severity = "high"
    log_type = lower(replace(each.value.log_type, "_", "-"))
  }
}

# Alert Policy for Ingestion Volume Anomaly / Rate Dip
resource "google_monitoring_alert_policy" "ingestion_volume_anomaly" {
  provider     = google.byop
  for_each     = { for k, v in var.monitors : k => v if !lookup(v, "ignore", false) && v.volume_threshold > 0 }
  display_name = "[Ingestion] SecOps Ingestion Volume Dip Anomaly - ${each.value.log_type}"
  combiner     = "OR"

  conditions {
    display_name = "Ingestion volume dip below threshold for ${each.value.log_type}"
    condition_threshold {
      filter          = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND resource.labels.log_type = \"${each.value.log_type}\""
      comparison      = "COMPARISON_LT"
      threshold_value = each.value.volume_threshold
      duration        = "3600s"

      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project", "resource.labels.log_type"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Ingestion volume for `${each.value.log_type}` dropped below the dynamic baseline threshold (${each.value.volume_threshold} logs/hr). Potential upstream collector drop or pipeline blockage. Detailed event-to-ingested latency must be reviewed in Google SecOps Health Hub. Reference: SOP-INGESTION-ANOMALY."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "ingestion"
    severity = "medium"
    log_type = lower(replace(each.value.log_type, "_", "-"))
  }
}

# Alert Policy for BindPlane / Feed Collection Issue
resource "google_monitoring_alert_policy" "bindplane_agent_outage" {
  provider     = google.byop
  display_name = "[Ingestion] SecOps Collection Feed & Agent Outage"
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
        group_by_fields      = ["project", "resource.labels.collector_id"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "BindPlane Collection Agent has stopped emitting metrics. The collector may be offline or experiencing local buffer overflows. Reference: SOP-COLLECTOR-OUTAGE."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "ingestion"
    severity = "critical"
  }
}

# Dynamic Alert Policy for Silent Endpoint Hosts (ignoring hosts marked with ignore = true)
# Note: Google Cloud Monitoring condition_absent enforces a max duration of 23h30m (84600s) and full minute multiples.
resource "google_monitoring_alert_policy" "silent_host" {
  provider     = google.byop
  for_each     = { for k, v in var.host_monitors : k => v if !lookup(v, "ignore", false) && v.alert_window_seconds <= 84600 }
  display_name = "[Ingestion] SecOps Silent Endpoint Host - ${each.value.host_name}"
  combiner     = "OR"

  conditions {
    display_name = "No records received from host ${each.value.host_name}"
    condition_absent {
      filter   = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND metric.labels.ingestion_source = \"${each.value.host_name}\""
      duration = "${floor(min(84600, max(300, each.value.alert_window_seconds)) / 60) * 60}s"

      trigger {
        count = 1
      }

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project", "metric.labels.ingestion_source"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Endpoint host `${each.value.host_name}` went silent for more than ${coalesce(each.value.alert_window_human, "${each.value.alert_window_seconds}s")}. Verify agent daemon and network status on host. Reference: SOP-SILENT-HOST."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "ingestion"
    severity = "high"
    host     = lower(replace(each.value.host_name, "_", "-"))
  }
}

# ------------------------------------------------------------------------------
# CATEGORY 2: PARSING ALERTS
# ------------------------------------------------------------------------------

# Alert Policy for Parser Degradation / Normalization Errors
resource "google_monitoring_alert_policy" "parser_degradation" {
  provider     = google.byop
  display_name = "[Parsing] SecOps Normalization Parser Degradation Alert"
  combiner     = "OR"

  conditions {
    display_name = "Parser failure ratio > 5% on parsed logs"
    condition_monitoring_query_language {
      query    = <<-EOT
        fetch chronicle.googleapis.com/Collector
        | { metric 'chronicle.googleapis.com/normalizer/log/record_count'
            | filter metric.state == 'failed_parsing'
            | group_by [resource.project_id, resource.log_type], 15m, sum(val())
          ; metric 'chronicle.googleapis.com/normalizer/log/record_count'
            | group_by [resource.project_id, resource.log_type], 15m, sum(val()) }
        | ratio
        | condition val() > 0.05
      EOT
      duration = "0s"
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Parser failure ratio exceeds 5% in the last 15 minutes. This suggests a parser layout shift or broken format structure in parsed logs. Reference: SOP-PARSER-DEGRADATION."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "parsing"
    severity = "high"
  }
}

# Alert Policy for Catch-All (CATCH_ALL) Ingestion Warning
resource "google_monitoring_alert_policy" "unspecified_log_growth" {
  provider     = google.byop
  display_name = "[Parsing] SecOps Catch-All Log Type Growth Warning"
  combiner     = "OR"

  conditions {
    display_name = "High ingestion volume on CATCH_ALL log type bucket"
    condition_threshold {
      filter          = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND (resource.labels.log_type = \"CATCH_ALL\" OR resource.labels.log_type = \"UNSPECIFIED_LOG_TYPE\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 1000 # Warning if catch-all exceeds 1,000 unparsed records per 5m
      duration        = "1800s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project", "resource.labels.log_type"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Unparsed logs are arriving at CATCH_ALL route. Inspect BindPlane/Forwarder fallback routes and parser mappings. Reference: SOP-CATCHALL-LOGS."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "parsing"
    severity = "medium"
  }
}

# ------------------------------------------------------------------------------
# CATEGORY 3: BILLING ALERTS
# ------------------------------------------------------------------------------

# Alert Policy for Ingestion Quota Rejection / Dropped Logs
resource "google_monitoring_alert_policy" "ingestion_quota_rejection" {
  provider     = google.byop
  display_name = "[Billing] SecOps Ingestion Quota Rejection / Dropped Logs"
  combiner     = "OR"

  conditions {
    display_name = "SecOps ingestion quota exceeded (quota_rejected_bytes_count > 0)"
    condition_threshold {
      filter          = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/quota_rejected_bytes_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]

  documentation {
    content   = "Ingestion logs are actively being dropped due to quota limit exhaustion (`quota_rejected_bytes_count > 0`). Immediate license expansion or collection rate limiting required. Proactive 80% consumption pacing is tracked by the daily forecast engine. Reference: SOP-QUOTA-MANAGEMENT."
    mime_type = "text/markdown"
  }

  user_labels = {
    category = "billing"
    severity = "critical"
  }
}
