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
│   └── forecast_engine.py    <- Runs consumption calculations and alerts on overages
└── terraform/
    ├── main.tf               <- IaC for alerts & webhook notification configurations
    ├── variables.tf          <- Alert parameters
    └── terraform.tfvars.json <- SLA parameters (output of run_profiler.py)
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

### Core Metrics Table
| Metric Name (Prefix: `chronicle.googleapis.com/`) | Type | Key Labels | Description |
| :--- | :--- | :--- | :--- |
| **`ingestion/log/bytes_count`** | DELTA | `log_type`, `ingestion_source` | Authoritative raw log bytes count. |
| **`ingestion/log/record_count`** | DELTA | `log_type`, `ingestion_source` | Authoritative raw log record count. |
| **`normalizer/log/record_count`** | DELTA | `log_type`, `state` | Log lines parsed (`state` = `parsed` or `failed_parsing`). |
| **`agent/exporter_accepted_spans_count`** | DELTA | `collector_id`, `input_type` | Ingestion traces accepted by Bindplane Agents. |
| **`forwarder/last_heartbeat`** | GAUGE | `collector_id`, `input_type` | Heartbeat timestamp of Bindplane/OTEL collectors. |

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

## 7. Production Deployment: Cloud Run Functions & Scheduler

In a production environment, running Python scripts on local machines is discouraged. Instead, deploy the scripts as serverless **Cloud Run Functions** (previously Cloud Functions Gen 2) orchestrated by **Cloud Scheduler**.

```
+-----------------+                      +---------------------+
| Cloud Scheduler | --(HTTP trigger)-->  | Cloud Run Function  |
| (Weekly Cron)   |                      | (SLA Engine/Python) |
+-----------------+                      +----------+----------+
                                                    |
                                                    v (writes variables)
+-----------------+                      +----------+----------+
|   Cloud Build   | <---(triggers)------ | GCS Config Bucket   |
| (Terraform Apply)                      | (tfvars.json)       |
+-----------------+                      +---------------------+
```

### 1. Environment Variables Configuration
Configure the following env variables on your Cloud Run Functions:

| Variable Name | Required By | Description | Example Value |
| :--- | :--- | :--- | :--- |
| `GCP_PROJECT_ID` | All scripts | The GCP project housing your BYOP SecOps metrics. | `my-secops-byop-project` |
| `OUTPUT_GCS_BUCKET` | Profiler / Forecast | The GCS bucket where output configuration files are written. | `my-company-secops-configs` |
| `OUTPUT_GCS_BLOB` | Profiler | Filename for the output Terraform variables JSON file. | `terraform.tfvars.json` |
| `COMMITTED_GB` | Forecast | Your contracted committed log volume (in Gigabytes) for the term. | `365000` |
| `CONTRACT_START` | Forecast | Start of the contract term (ISO format). | `2026-01-01T00:00:00Z` |
| `CONTRACT_END` | Forecast | End of the contract term (ISO format). | `2026-12-31T23:59:59Z` |

### 2. IAM Service Account Permissions
The Service Account assigned to the Cloud Run Functions must possess the following IAM roles:
*   `roles/monitoring.viewer`: Permitted to query Cloud Monitoring metric time-series data.
*   `roles/storage.objectAdmin`: Permitted to write and update configuration files in the GCS config bucket.

### 3. Deploying as a Cloud Run Function (Command Line)
Deploy the SLA Profiler using the `gcloud` CLI:
```bash
gcloud functions deploy secops-sla-profiler \
  --gen2 \
  --runtime=python310 \
  --region=us-central1 \
  --main-http-entrypoint=main \
  --source=./scripts \
  --set-env-vars GCP_PROJECT_ID=my-secops-byop-project,OUTPUT_GCS_BUCKET=my-company-secops-configs \
  --trigger-http \
  --no-allow-unauthenticated
```
*(Ensure you modify your Python scripts' code to act as an HTTP endpoint by accepting `request` parameter as required by the Functions framework).*

### 4. Configuring Cloud Scheduler (Weekly SLA Profiler Trigger)
Schedule the Cloud Run function to trigger every Sunday night at midnight:
```bash
gcloud scheduler jobs create http trigger-secops-profiler \
  --schedule="0 0 * * 0" \
  --uri="https://us-central1-my-project.cloudfunctions.net/secops-sla-profiler" \
  --http-method=POST \
  --oidc-service-account-email="secops-function-sa@my-project.iam.gserviceaccount.com" \
  --location=us-central1
```

---

## 8. Terraform State Management & CI/CD

### 1. Remote GCS State Store
To prevent concurrent state modifications and secure your state history, use a remote **GCS backend** instead of local files. Update `terraform/main.tf` by uncommenting the backend block:

```hcl
terraform {
  backend "gcs" {
    bucket  = "my-company-secops-tfstate"
    prefix  = "terraform/secops-monitoring/state"
  }
}
```
> [!IMPORTANT]
> Always enable **Object Versioning** on your GCS tfstate bucket to recover state in case of accidental deletions.

### 2. GitOps Automation Trigger (Cloud Build)
Create a Cloud Build trigger that automatically executes `terraform apply` when a new `terraform.tfvars.json` is written by the profiling script to the GCS configuration bucket:

1.  **Trigger Source**: Cloud Storage bucket (`gs://my-company-secops-configs/terraform.tfvars.json`).
2.  **Build Steps**:
    *   Pull Terraform files from your repository.
    *   Download `terraform.tfvars.json` from GCS.
    *   Run `terraform init`.
    *   Run `terraform apply -auto-approve`.

---

## 9. Implementation & Validation Plan

To ensure your alert configurations function as intended, follow this three-phase validation strategy:

```
[Phase 1: Validation] ---> [Phase 2: Alert Outage Simulation] ---> [Phase 3: Webhook Verification]
```

### Phase 1: Dry-Running Python Scripts
Validate that API connections, IAM roles, and GCS write permissions are functional before deploying IaC:
```bash
# Run locally using your user credentials to test Metric queries
export GCP_PROJECT_ID="my-secops-byop-project"
python3 scripts/run_profiler.py

# Confirm that terraform/terraform.tfvars.json has been correctly updated
cat terraform/terraform.tfvars.json
```

### Phase 2: Simulating an Alert Event (Outage Test)
To verify that absence alerts trigger and route correctly to your notification channel, simulate an outage:
1.  Temporarily lower the absence threshold for a specific feed (e.g., set `alert_window_seconds = 60` for a test feed) inside `terraform.tfvars.json`.
2.  Run `terraform apply` to deploy the change.
3.  Stop sending test logs to that stream for 1 minute.
4.  Monitor the Cloud Monitoring console to ensure the policy transitions to the **Firing** state.
5.  Revert the threshold window and redeploy `terraform apply`.

### Phase 3: Webhook Payload & Ontology Verification
Verify that the payload structure integrates correctly with Google SecOps SOAR:
1.  Go to the **Google Cloud Monitoring console > Alerting > Notification Channels**.
2.  Select your Webhook Gateway channel and click **Send Test Connection**.
3.  Inside your Google SecOps SOAR console, open **Incoming Webhooks Log** and verify:
    *   The connection payload was received successfully.
    *   Ontology mappings mapped `policy_name` to `source_rule` and timestamps correctly parsed.
