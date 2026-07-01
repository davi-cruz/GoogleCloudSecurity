# 📋 Google SecOps Comprehensive Monitoring Solution Deployment & Adoption Guide

This directory contains the production templates, scripts, and documentation for deploying the **Google SecOps Comprehensive Monitoring Solution**. 

This solution is designed to alert on ingestion pipeline outages, track parsed event quality, profile source ingestion latency, and forecast license/credits exhaustion—all natively within Google Cloud Monitoring and Google SecOps, without requiring BigQuery.

---

## 📂 Directory Structure
```
/monitoring-solution/
├── README.md                 <- This deployment guide
├── scripts/
│   ├── run_profiler.py       <- Weekly script to profile ingestion SLA & metrics
│   ├── drift_detector.py     <- Daily script to alert on unmonitored or silent sources
│   ├── forecast_engine.py    <- Runs consumption calculations and alerts on overages
│   ├── contract_terms.json   <- Multi-term contract schedules configuration sample
│   └── requirements.txt      <- Dependencies for the Python engine
└── terraform/
    ├── main.tf               <- IaC deploying both BYOP alerts & Orchestration serverless components
    ├── variables.tf          <- Alert and infrastructure variables
    └── terraform.tfvars.json <- Configured parameter values (byop/orchestration project divisions)
```

---

## 1. Compliance Alignment: OMB M-21-31
This solution satisfies the event logging (EL) requirements outlined in the White House Office of Management and Budget (OMB) Memorandum **M-21-31**:
*   **EL1 (Basic)**: Implements metric-absence alerts to guarantee continuous syslog, agent, and api data flows.
*   **EL2 (Intermediate)**: Tracks parsing and normalization health ratio rates to maintain UDM schema compliance.
*   **EL3 (Advanced)**: Routes alerts directly to SOAR playbooks for automated remediation.

---

## 2. Solution Architecture & Key Metrics

The architecture uses a unified, low-churn approach:
*   **Engine Profiler Script (Python)**: Queries the GCP Metrics API to calculate volume patterns, suggests SLA groupings, and auto-provisions Terraform.
*   **Alert Policies**: Defined natively in **Google Cloud Monitoring** (BYOP project).
*   **Visualizations**: Built natively inside **Google SecOps Custom Dashboards** using YARA-L queries on the `ingestion` (Ingestion Metrics) schema.
*   **SOAR Integration**: Routes Cloud Monitoring alerts via Webhook into Google SecOps SOAR.

### Project Separation (BYOP vs. Orchestration)
To support separate billing accounts and access control models, the architecture is split across two projects:
1.  **BYOP Project (`byop_project_id`)**: The Google Cloud project where Google SecOps metrics are stored, and where Monitoring Alert Policies and Notification Channels are deployed.
2.  **Orchestration Project (`orchestration_project_id`)**: The Google Cloud project housing the orchestration resources, including Google Cloud Storage buckets, Secret Manager secrets, Cloud Run Functions, and Cloud Scheduler triggers.

---

## 3. Alerts Summary & Alerting Logic

The solution generates the following primary alerts:

1.  **Log Ingestion Absence (Per Source)**: Triggers when no log records are ingested for a source (e.g. `WINDOWS_DNS`) for longer than its designated SLA window (e.g. 5m for realtime, 12h for variable).
2.  **Silent Endpoint Host**: Alerts if a specific server host ceases sending logs, while the gateway agent remains online. (Requires Bindplane to copy `host.name` to the `ingestion_source` label).
3.  **Active-Passive HA Outage**: Alerts only when *all* redundant cluster members (e.g. `prod-fw-1` and `prod-fw-2`) stop sending data simultaneously.
4.  **Parser/Normalization Failure**: Alerts if parsing errors rise to $\ge 5\%$ of total logs in a 15-minute window, identifying vendor format shifts or broken parsers.
5.  **Bindplane Agent Outage**: Alerts when a collection agent daemon fails or loses connectivity.
6.  **Quota Approaching Limit**: Alerts when consumption rates reach $80\%$ of daily or monthly license volumes.

---

## 4. Ingestion Trends Without BigQuery (YARA-L Dashboard)

If your Google SecOps instance does not export to BigQuery, use the native dashboard engine within SecOps. The engine natively supports the `ingestion` prefix with a 365-day query retention period.

### Daily Throughput Chart (GB)
Create a new custom dashboard widget in Google SecOps with this YARA-L 2.0 query:
```yara
ingestion.component = "Ingestion API"
ingestion.log_type != ""
ingestion.log_type != "FORWARDER_HEARTBEAT"

$Date = timestamp.get_date(ingestion.end_time)

match:
  $Date
outcome:
  $Throughput_GB = math.round(sum(ingestion.log_volume) / (1000 * 1000 * 1000), 2)
order:
  $Date desc
```

---

## 5. Webhook Ontology Payload Mapping

Alerts are sent as JSON payloads to the SOAR webhook. Map the incoming GCP Monitoring fields to the SOAR ontology inside Google SecOps SOAR:

```json
{
  "StartTime": 1782918400000,
  "EndTime": 1782918700000,
  "product_type": "Google Cloud Monitoring",
  "event_type": "Metric Absence",
  "soar_alert_id": "incident_30b5e050_7c2b_489d",
  "detection_time": 1782918400000,
  "source_rule": "SecOps Source Silent - WINEVTLOG",
  "source_system_uri": "https://console.cloud.google.com/monitoring/alerting/incidents/incident_30b5e050_7c2b_489d?project=my-byop-project",
  "Message": "No logs ingested for WINEVTLOG in the last 60 minutes.",
  "description": "Please review Outage Alert Response SOP located at SOP-URI. Check forwarder and collector health.",
  "Severity": "Critical",
  "CategoryOutcome": "open",
  "custom_fields": {
    "project_id": "my-byop-project",
    "collector_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "log_type": "WINEVTLOG",
    "ingestion_source": "central-syslog-gateway-us"
  }
}
```

---

## 6. Execution Schedules & Maintenance

### Run Frequencies
*   **Weekly Engine Run (`scripts/run_profiler.py`)**: Runs every Sunday night. Analyzes P95 latency and average volume behaviors, updates `terraform.tfvars.json`, and triggers a `terraform apply` deployment.
*   **Daily Drift Detection (`scripts/drift_detector.py`)**: Runs daily. Compares active log sources with current Terraform configurations, warning the team if a new log source has appeared or if an existing stream went decommissioned.

---

## 7. Infrastructure Provisioning via Terraform

Unlike manual scripts, the entire serverless orchestration stack (Cloud Run Functions, Cloud Scheduler triggers, Secret Manager secrets, and GCS buckets) is deployed automatically via the Terraform templates.

```
                  +----------------------------------------------+
                  |            Orchestration Project             |
                  |  +--------------------+                      |
                  |  |   Secret Manager   | --(webhook url)      |
                  |  +--------------------+         |            |
                  |  | GCS Config/Deploy  |         |            |
                  |  +---------+----------+         |            |
                  |            | (deploys)          v            |
                  |  +---------v----------+   +------------+     |
                  |  | Cloud Run Function |-->| Scheduler  |     |
                  |  +--------------------+   +------------+     |
                  +----------------------------------------------+
                                |
                                v (deploys Alert Policies & Channels)
                  +----------------------------------------------+
                  |                 BYOP Project                 |
                  |  +--------------------+                      |
                  |  |   Alert Policies   |                      |
                  |  +--------------------+                      |
                  +----------------------------------------------+
```

### 1. Variables & Project Configurations
Edit `terraform/terraform.tfvars.json` to define the target projects:
```json
{
  "byop_project_id": "my-secops-byop-project",
  "orchestration_project_id": "my-secops-orchestration-project",
  "region": "us-central1",
  "soar_webhook_url": "https://secops-instance.siemplify-soar.com/api/webhooks/incoming/gcp-monitoring",
  "contract_terms_json": "[\n  {\n    \"start_date\": \"2026-01-01T00:00:00Z\",\n    \"end_date\": \"2026-12-31T23:59:59Z\",\n    \"committed_gb\": 365000.0\n  }\n]"
}
```

### 2. Required IAM Permissions for Deployment
The deployment engineer or CI/CD Service Account requires the following permissions:
*   **On the Orchestration Project**:
    *   `roles/secretmanager.admin` (to configure Webhook secret storage).
    *   `roles/storage.admin` (to create deployment/config buckets).
    *   `roles/cloudfunctions.developer` & `roles/run.developer` (to deploy serverless routines).
    *   `roles/cloudscheduler.admin` (to create cron scheduler jobs).
    *   `roles/iam.serviceAccountAdmin` (to create the function's runtime service account).
*   **On the BYOP Project**:
    *   `roles/monitoring.editor` (to deploy Alert Policies and Webhook Channels).

---

## 8. Secrets & State Governance

### 1. Secret Manager Integration
The `soar_webhook_url` is stored securely inside Secret Manager on the orchestration project (`secops-soar-webhook-url`). During deployment, Terraform fetches the secret version dynamically using the `google_secret_manager_secret_version` data source and maps it to the BYOP Project's Notification Channel. This ensures the integration token is never written in plaintext within state files.

### 2. GCS State Store
To prevent concurrent state modifications and secure your state history, use a remote **GCS backend** instead of local files. Update `terraform/main.tf` by uncommenting the backend block:
```hcl
terraform {
  backend "gcs" {
    bucket  = "my-company-secops-tfstate"
    prefix  = "terraform/secops-monitoring/state"
  }
}
```

---

## 9. Implementation, Validation & Dry-Run Guides

Before deploying configurations, use the built-in **Dry Run** flag to evaluate calculations directly inside your terminal in markdown or HTML formats.

### 1. Profiler Dry Run
Queries Cloud Monitoring API and prints the suggested SLA window configurations as a markdown table:
```bash
# Run local dry run
python3 scripts/run_profiler.py my-secops-byop-project --dry-run --format markdown
```

#### Output Example:
| Log Type | SLA Profile | Alert Window (sec) | Daily Avg Logs | Volume Threshold |
| :--- | :--- | :--- | :--- | :--- |
| **CROWDSTRIKE_EDR** | realtime | 300 | 540301 | 54030 |
| **GCP_CLOUDTRAIL** | near_realtime | 1200 | 120401 | 12040 |
| **WINDOWS_DNS** | batch | 7200 | 25032 | 2503 |

### 2. Consumption Forecast Engine Dry Run (Multi-Year Contract Verification)
Queries current log metrics since the active contract term and projects overage:
```bash
# Point to the multi-year config file directly for terminal dry-run review
python3 scripts/forecast_engine.py \
  my-secops-byop-project \
  --terms-file scripts/contract_terms.json \
  --dry-run --format markdown
```

#### Output Example:
**Active Contract Term:** Year 1 of 3

| Parameter | Value |
| :--- | :--- |
| **Calculated At** | 2026-07-01T12:00:00Z |
| **Active Term Range** | 2026-01-01T00:00:00Z to 2026-12-31T23:59:59Z (181 days elapsed, 184 remaining) |
| **Committed License Volume** | 365000.0 GB |
| **Cumulative Ingested** | 210403.5 GB (57.64% of active quota) |
| **Ideal Target Volume** | 181000.0 GB (49.58% of term) |
| **Projected Volume (Term End)** | 424218.42 GB |
| **Estimated Overage** | **59218.42 GB** |

### 3. Simulating an Ingestion Outage Alert
To verify that absence alerts trigger and route correctly to your notification channel, simulate an outage:
1.  Temporarily lower the absence threshold for a specific feed (e.g., set `alert_window_seconds = 60` for a test feed) inside `terraform.tfvars.json`.
2.  Run `terraform apply` to deploy the change.
3.  Stop sending test logs to that stream for 1 minute.
4.  Monitor the Cloud Monitoring console to ensure the policy transitions to the **Firing** state.
5.  Revert the threshold window and redeploy `terraform apply`.

### 4. Webhook Payload & Ontology Verification
Verify that the payload structure integrates correctly with Google SecOps SOAR:
1.  Go to the **Google Cloud Monitoring console > Alerting > Notification Channels**.
2.  Select your Webhook Gateway channel and click **Send Test Connection**.
3.  Inside your Google SecOps SOAR console, open **Incoming Webhooks Log** and verify:
    *   The connection payload was received successfully.
    *   Ontology mappings mapped `policy_name` to `source_rule` and timestamps correctly parsed.
