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

1.  **Log Ingestion Absence (Per Source)**: Triggers when no log records are ingested for a source (e.g. `WINDOWS_DNS`) for longer than its calculated SLA window (e.g. 5m for realtime, 12h for variable).
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

## 7. Required Permissions & Deployment Steps

### IAM Permissions Required
Deploying and running this monitoring solution requires:
1.  **For Terraform Deployment**:
    *   `roles/monitoring.editor` (to create alert policies and notification channels).
2.  **For Script Executions (Profiler / Drift Detection / Forecast)**:
    *   `roles/monitoring.viewer` (to read metric time series data).

### Deployment Instructions

#### Step 1: Initialize Terraform
Configure the backend in your Terraform root and run:
```bash
cd terraform/
terraform init
```

#### Step 2: Configure the Variables
Edit `terraform.tfvars.json` to define your target `project_id` and the `soar_webhook_url` pointing to your SOAR gateway.

#### Step 3: Run the Ingestion Profiler
Bootstrap your variables dynamically:
```bash
python3 ../scripts/run_profiler.py <GCP_PROJECT_ID>
```
*This will query the metrics API and overwrite the `monitors` configuration block in `terraform/terraform.tfvars.json` with the P95 SLA values.*

#### Step 4: Apply the Alerts Infrastructure
Deploy the policies:
```bash
terraform plan
terraform apply -auto-approve
```

---

## 8. Customization & Adjustments
*   **Adjusting SLA Thresholds**: To manually override a feed's SLA window (e.g. increase an Azure feed's absence alert threshold to 12 hours), edit the corresponding entry block in `terraform.tfvars.json` and redeploy.
*   **Adding Exclusions**: If a test collector or log type triggers false positive alerts, exclude it inside `terraform/main.tf` by appending filter rules (e.g. `AND metric.labels.log_type != "DUMMY_SOURCE"`).
