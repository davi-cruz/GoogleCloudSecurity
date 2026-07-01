terraform {
  required_version = ">= 1.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 4.0.0"
    }
  }

  # --- RECOMMENDED PRODUCTION STATE STORAGE CONFIGURATION ---
  # To use a remote Google Cloud Storage bucket for storing terraform state (tfstate):
  # 1. Create a GCS bucket (e.g. gs://my-company-secops-tfstate).
  # 2. Enable object versioning on the GCS bucket for backup safety.
  # 3. Uncomment the block below and replace bucket name.
  #
  # backend "gcs" {
  #   bucket  = "my-company-secops-tfstate"
  #   prefix  = "terraform/secops-monitoring/state"
  # }
}

provider "google" {
  project = var.project_id
}

# 1. Configures the Webhook Notification Channel pointing to Google SecOps SOAR
resource "google_monitoring_notification_channel" "soar_webhook" {
  display_name = "SecOps SOAR Webhook Gateway"
  type         = "webhook_tokenauth"
  labels = {
    url = var.soar_webhook_url
  }
  user_labels = {
    target = "secops-soar"
  }
}

# 2. Dynamic Alert Policies per Log Feed (Metric Absence alerts matching SLA)
resource "google_monitoring_alert_policy" "log_feed_absence" {
  for_each     = var.monitors
  display_name = "SecOps Log Ingestion Absence - ${each.value.log_type}"
  combiner     = "OR"

  conditions {
    display_name = "No records seen for ${each.value.log_type}"
    condition_absent {
      filter   = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/ingestion/log/record_count\" AND metric.labels.log_type = \"${each.value.log_type}\""
      duration = "${each.value.alert_window_seconds}s"
      
      trigger {
        count = 1
      }
      
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project_id", "metric.labels.log_type"]
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

# 3. Alert Policy for BindPlane Collection Agent Outage
resource "google_monitoring_alert_policy" "bindplane_agent_outage" {
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

# 4. Alert Policy for Parser Degradation / Normalization Errors
resource "google_monitoring_alert_policy" "parser_degradation" {
  display_name = "SecOps Normalization Parser Degradation Alert"
  combiner     = "OR"

  conditions {
    display_name = "Parser failure ratio > 5% on parsed logs"
    condition_threshold {
      filter          = "resource.type = \"chronicle.googleapis.com/Collector\" AND metric.type = \"chronicle.googleapis.com/normalizer/log/record_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.05
      duration        = "900s"
      
      # Using MQL ratio filter style
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["project_id", "metric.labels.log_type"]
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

# 5. Alert Policy for Ingestion Quota Reaching Capacity
resource "google_monitoring_alert_policy" "ingestion_quota_warning" {
  display_name = "SecOps Ingestion Quota Approaching Limit"
  combiner     = "OR"

  conditions {
    display_name = "Ingestion rate is above 80% of configured daily quota"
    condition_matched_log {
      filter = "resource.type = \"chronicle.googleapis.com/Collector\""
    }
    # Note: Complex ratio operations are best represented directly as MQL query filters
  }
  
  # For complex MQL conditions, standard monitoring configuration blocks are deployed using the query block:
  # query = "fetch chronicle.googleapis.com/Collector | { metric 'ingestion/log/bytes_count' | group_by [project_id], 5m, sum(value.bytes_count) ; metric 'ingestion/quota_limit' | filter (metric.quota_type == 'LONG_TERM') | group_by [project_id], 5m, min(value.quota_limit) } | join | value [quota_utilization_pct: 100 * (sum(bytes_count) / min(quota_limit))] | condition quota_utilization_pct > 80.0"

  notification_channels = [google_monitoring_notification_channel.soar_webhook.name]
  
  documentation {
    content   = "Daily ingestion has reached 80% of total contracted license quota. Overage billing charges will apply soon if ingestion trajectory continues. Reference: SOP-QUOTA-MANAGEMENT."
    mime_type = "text/markdown"
  }
  
  user_labels = {
    severity = "critical"
  }
}
