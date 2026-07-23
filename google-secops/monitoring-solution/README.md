# 📋 Google SecOps Comprehensive Monitoring Solution Deployment & Adoption Guide

This directory contains the production templates, scripts, and documentation for deploying the **Google SecOps Comprehensive Monitoring Solution**. 

This solution is designed to alert on ingestion pipeline outages, track parsed event quality, profile source ingestion latency, and forecast license/credits exhaustion—all natively within Google Cloud Monitoring and Google SecOps, without requiring BigQuery.

---

## 📂 Directory Structure
```
/monitoring-solution/
├── README.md                    <- Comprehensive deployment & adoption guide
├── scripts/
│   ├── main.py                  <- Unified Cloud Run Function HTTP entrypoint
│   ├── secops_monitoring_utils.py <- Shared utilities module (SOAR Webhook POST, duration format, GCS I/O)
│   ├── run_profiler.py          <- SLA Ingestion Profiler & P99 arrival gap engine
│   ├── drift_detector.py        <- Configuration Drift Detector & CATCH_ALL route monitor
│   ├── forecast_engine.py       <- Multi-term contract consumption forecast engine
│   ├── contract_terms.json      <- Multi-term contract schedule configuration
│   └── requirements.txt         <- Dependencies for the Python engine
└── terraform/
    ├── main.tf                  <- IaC deploying BYOP alert policies & single Orchestration Function
    ├── variables.tf             <- Alert and infrastructure variable definitions
    └── terraform.tfvars.json    <- Configured parameter values (project IDs, SLA monitors, contract terms)
```

---

## 1. Architectural Rationale: Unified Serverless Orchestration

This solution leverages Google Cloud's dedicated telemetry platform (**Google Cloud Monitoring**) alongside a **single, unified Cloud Run Function** (`secops-monitoring-orchestrator`) to oversee ingestion pipeline health, track parsed event quality, profile source ingestion latency, and forecast license consumption—with **zero BigQuery requirement** for standard operations.

```
+----------------------------------------------------------------------------------------------------+
|                                    CLOUD SCHEDULER JOBS                                            |
|   ├── profiler_scheduler (Weekly)   ├── forecast_scheduler (Daily)   ├── drift_scheduler (Daily)   |
|   └── POST {"action": "profiler"}   └── POST {"action": "forecast"}   └── POST {"action": "drift"}  |
+--------------------------------------------------+-------------------------------------------------+
                                                   |
                                                   v (HTTP POST Trigger)
+----------------------------------------------------------------------------------------------------+
|                       UNIFIED CLOUD RUN FUNCTION (secops-monitoring-orchestrator)                  |
|                        (scripts/main.py & scripts/secops_monitoring_utils.py)                     |
|                                                                                                    |
|   ├── Action: profiler  ──> Queries Cloud Monitoring API ──> Generates terraform.tfvars.json in GCS |
|   ├── Action: forecast  ──> Projects contract runway    ──> Dispatches SOAR Webhook Alert on Overage  |
|   └── Action: drift     ──> Checks CATCH_ALL / Feed Drift──> Dispatches SOAR Webhook Alert on Drift    |
+----------------------+-----------------------------------|-----------------------------------------+
                       |                                   |
                       v (GCS Upload)                      v (SOAR Webhook POST)
+--------------------------------------------+    +--------------------------------------------------+
|      Cloud Storage Bucket (GCS)            |    |              GOOGLE SECOPS CONSOLE               |
|      gs://<bucket>/terraform.tfvars.json   |    |                                                  |
+----------------------+---------------------+    |   +------------------------------------------+   |
                       |                          |   |           SOAR Alert Queue               |   |
                       v (GCS Object Trigger)     |   |   (Dedicated Environment: SecOps-Health) |   |
+--------------------------------------------+    |   +------------------------------------------+   |
|           Cloud Build CI/CD                |    |                        ^                         |
|   Executes `terraform apply` on change     |    |                        | (Alert Webhooks)        |
+----------------------+---------------------+    +------------------------|-------------------------+
                       |                                                   |
                       v (Deploys Alert Policies)                          |
+--------------------------------------------------------------------------+                         |
|                       GOOGLE CLOUD MONITORING (BYOP Project)                                       |
|   ├── Ingestion Alert Policies (Dynamic P99 Absence SLA, P95 Latency, Silent Host) ────────────────┘
|   ├── Parsing Alert Policies (Parser Error Ratio, Catch-All Route Warning)
|   └── Billing Alert Policies (Daily Quota Approaching Limit)
+----------------------------------------------------------------------------------------------------+
```

### Strategic Highlights of the Unified Architecture:
1. **Single Cloud Run Function Deployment**: All analytical logic (`run_profiler.py`, `forecast_engine.py`, `drift_detector.py`) is encapsulated into a single HTTP serverless entrypoint (`main.py`), reducing cloud resource footprint and simplifying security lifecycle management.
2. **Action Dispatch via HTTP Payload**: Cloud Scheduler jobs pass a lightweight JSON payload (`{"action": "profiler"}`, `{"action": "forecast"}`, `{"action": "drift"}`) to trigger specific tasks.
3. **SOAR Webhook Integration**: Both engine-level alerts (contract overage, configuration drift) and infrastructure-level alerts (metric absence, latency surge) post SOAR-formatted JSON payloads directly to the **SecOps SOAR Alert Queue**.
4. **Human-Readable Alert Telemetry**: Alert policy documentations and profiler outputs automatically convert raw seconds into clear human-readable magnitudes (e.g. `6h 18m`, `5m`, `30m`).

---

## 2. Compliance Alignment: OMB M-21-31
This solution satisfies the event logging (EL) requirements outlined in the White House Office of Management and Budget (OMB) Memorandum **M-21-31**:
*   **EL1 (Basic)**: Implements metric-absence alerts to guarantee continuous syslog, agent, and api data flows.
*   **EL2 (Intermediate)**: Tracks parsing and normalization health ratio rates to maintain UDM schema compliance.
*   **EL3 (Advanced)**: Routes alerts directly to SOAR playbooks for automated remediation.

---

## 3. Alerts Summary & Categorized Alerting Logic

The solution categorizes all alerts into 3 core operational domains:

### 1. Ingestion Category
1. **Log Ingestion Absence (Per Source)**: Triggers when no log records are ingested for a source (e.g., `WINDOWS_DNS`) for longer than its dynamic P99 arrival SLA window.
2. **P95 Ingestion Latency Surge**: Alerts when P95 ingestion latency exceeds 30 minutes (or $3\times$ baseline), identifying delayed ingestion pipelines that compromise YARA-L detection rule windows.
3. **Silent Endpoint Host**: Alerts if a specific server host ceases sending logs while the gateway agent remains online.
4. **Bindplane Agent / Feed Outage**: Alerts when a collection agent daemon fails or loses connectivity.
5. **Log Spike & Log Dip**: MQL ratio policies detecting sudden surges ($>200\%$) or drops ($>70\%$) relative to historical baselines.

### 2. Parsing Category
1. **Parser/Normalization Degradation**: Alerts if parsing error ratio rises to $\ge 5\%$ over a 15-minute window, identifying vendor format shifts or broken parsers.
2. **Catch-All Growth Warning**: Alerts when `CATCH_ALL` (or `UNSPECIFIED_LOG_TYPE`) volume surges, indicating unmapped Bindplane feeds or missing parser assignments.

### 3. Billing Category
1. **Ingestion Quota Approaching Limit**: Alerts when consumption rates reach $80\%$ of daily capacity.
2. **Contract Consumption Overage Forecast**: `forecast_engine.py` aggregates multi-term contract consumption velocity and dispatches Webhook alerts when projected end-of-term volume exceeds license commitments.

---

## 4. Ingestion Trends & Dashboards

> [!NOTE]
> **Coming Soon**: This section will contain recommended native Google SecOps ingestion health and log monitoring dashboard visualizations.

---

## 5. Google SecOps SOAR Ontology Field Mapping Guide

Alerts dispatched to the SOAR incoming Webhook endpoint follow a standardized JSON schema. Map incoming JSON alert keys to SOAR ontology fields using the reference guide below:

| JSON Payload Field | Google SecOps SOAR Ontology Field | Description / Mapping Logic |
| :--- | :--- | :--- |
| `soar_alert_id` | `TicketId`, `DisplayId` | Unique incident identifier for alert queue indexing and deduplication. |
| `"Google Cloud Monitoring"` | `SourceSystemName`, `DeviceVendor` | Originating telemetry source system tag. |
| `source_rule` | `Name`, `RuleGenerator` | Alert rule title displayed in analyst queues (e.g., `"SecOps Source Silent - WINEVTLOG"`). |
| `StartTime` | `StartTime` | Epoch millisecond timestamp of alert trigger. |
| `EndTime` | `EndTime` | Epoch millisecond timestamp of incident close/update. |
| `Message` | `Reason` | High-level summary of the triggered alert condition. |
| `description` | `Description` | Detailed Markdown text including Operating Procedures (SOP) and remediation guidance. |
| `Severity` | `Severity`, `Priority` | Priority level (`"Critical"`, `"High"`, `"Warning"`). |
| `CategoryOutcome` | `CategoryOutcome` | Status outcome (`"open"`). |
| `product_type` | `DeviceProduct`, `EventProduct` | Categorizes the monitoring system tool (`"Google Cloud Monitoring"`). |
| `event_type` | `EventName` | Specific trigger subcategory (e.g., `"Metric Absence"`, `"Configuration Drift Warning"`). |
| `custom_fields.project_id` | `Environment` | Segregates alert cases into dedicated SOAR environments (e.g. `SecOps-Health`). |
| `custom_fields.log_type` | `custom_fields["log_type"]` | Impacted Security Log Type schema label (e.g., `"AZURE_AD_AUDIT"`). |
| `custom_fields` | `EventsList` | Array representation of custom metadata fields (`collector_id`, `log_type`, `ingestion_source`) for analyst inspection in event panels. |

---

## 6. Execution Schedules & Maintenance

### Run Frequencies
*   **Weekly Engine Run (`scripts/run_profiler.py`)**: Runs every Sunday night. Analyzes P95 latency and average volume behaviors, updates `terraform.tfvars.json`, and triggers a `terraform apply` deployment.
*   **Daily Drift Detection (`scripts/drift_detector.py`)**: Runs daily. Compares active log sources with current Terraform configurations, warning the team if a new log source has appeared or if an existing stream went decommissioned.

---

## 7. Step-by-Step GCP Deployment Guide

This guide walks you through deploying this solution using your terminal. 

> [!TIP]
> **Recommended Environment: Google Cloud Shell**
> We recommend executing this deployment directly in **Google Cloud Shell** in the Google Cloud console. Cloud Shell provides a secure Linux environment pre-loaded with the Google Cloud SDK (`gcloud` CLI), Git, and Terraform. 
> To open Cloud Shell, click the terminal icon in the upper right corner of the Google Cloud console.

### Prerequisites (If running locally instead of Cloud Shell)
If you decide to deploy from your local system instead of Cloud Shell, verify that these dependencies are installed:
*   [Google Cloud SDK (gcloud CLI)](https://cloud.google.com/sdk/docs/install)
*   [Terraform CLI](https://developer.hashicorp.com/terraform/install)

---

### Step 1: Authenticate in Google Cloud
Open your terminal (or Cloud Shell) and authenticate using your GCP administrative account:
```bash
# Log in to your Google Account
gcloud auth login

# Set application default credentials (enables Terraform to authenticate)
gcloud auth application-default login
```

---

### Step 2: Identify your GCP Project IDs
You will need two GCP project IDs:
1.  **BYOP Project ID**: The project provided by Google for your SecOps instance (houses metrics data).
2.  **Orchestration Project ID**: A project you control where the serverless billing metrics and engines run. *(This can be the same as your BYOP project if you do not require project segregation)*.

Configure the orchestration project as your default CLI workspace:
```bash
gcloud config set project <YOUR_ORCHESTRATION_PROJECT_ID>
```

---

### Step 3: Store Webhook URL securely in Secret Manager
Store your target Google SecOps SOAR Webhook URL inside Secret Manager. Run this command:
```bash
# 1. Enable Secret Manager API in your Orchestration Project
gcloud services enable secretmanager.googleapis.com

# 2. Create the secret container resource
gcloud secrets create secops-soar-webhook-url --replication-policy="automatic"

# 3. Add the Webhook URL value (replace URL with your target SOAR Webhook URL)
echo -n "https://secops-instance.siemplify-soar.com/api/webhooks/incoming/gcp-monitoring" | \
  gcloud secrets versions add secops-soar-webhook-url --data-file=-
```

---

### Step 4: Enable Required Cloud Services
Enable the serverless, storage, and scheduling APIs in your **Orchestration Project**:
```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  storage.googleapis.com
```

---

### Step 5: Configure Contract Terms, Overrides & Project IDs
1. **Contract Terms Schedule**: Edit `scripts/contract_terms.json` to define your 1-year or multi-year terms schedule. This is the single source of truth for contract commitments.
2. **Manual SLA & Host Overrides**: Define any custom feed or silent host overrides in `scripts/overrides.json`.
3. **Project IDs Configuration**: Edit `terraform/terraform.tfvars.json` to specify your project IDs and region:
   ```json
   {
     "byop_project_id": "<YOUR_BYOP_PROJECT_ID>",
     "orchestration_project_id": "<YOUR_ORCHESTRATION_PROJECT_ID>",
     "region": "us-central1"
   }
   ```
   > [!TIP]
   > You **do not** need to manually format or serialize JSON strings inside `terraform.tfvars.json`. The profiler script automatically reads `scripts/contract_terms.json` and injects the serialized `contract_terms_json` string into `terraform.tfvars.json` during execution!

---

### Step 6: Initial SLA Profiling & Configuration Generation Run
Execute `run_profiler.py` to query metrics, apply overrides, auto-serialize contract terms, and populate `terraform.tfvars.json`:

```bash
cd google-secops/monitoring-solution/scripts

# Activate Virtual Environment
source .venv/bin/activate

# Generate and populate terraform.tfvars.json
python3 run_profiler.py <YOUR_BYOP_PROJECT_ID>
```

---

### Step 7: Provision Infrastructure via Terraform
Deploy alerting policies and serverless schedulers to Google Cloud:

```bash
cd ../terraform

# Initialize Terraform plugins
terraform init

# Validate configuration syntax
terraform validate

# Review execution plan
terraform plan -var-file="terraform.tfvars.json"

# Apply changes
terraform apply -var-file="terraform.tfvars.json" -auto-approve
```

---

## 8. Secret Manager Integration (Secrets Governance)

To comply with enterprise security practices, do not hardcode the webhook URL (which contains integration secrets/tokens) in cleartext variables. 

As configured in **Step 3**, you store the secret in Secret Manager. During `terraform apply`, Terraform dynamically retrieves the value from Secret Manager to configure the BYOP Project's notification channel, keeping the secret completely out of your local settings or version control systems.

### IAM Permissions for Secrets
*   **Provisioner Persona**: Needs `roles/secretmanager.admin` to manage the secret infrastructure.
*   **Terraform Service Account**: Needs `roles/secretmanager.secretAccessor` to retrieve the secret during plan/apply.

### Retrieve Secret version in Terraform
In your Terraform configuration, fetch the webhook URL dynamically using a data block:
```hcl
data "google_secret_manager_secret" "soar_webhook_url" {
  secret_id = "secops-soar-webhook-url"
}

data "google_secret_manager_secret_version" "soar_webhook_url_version" {
  secret = data.google_secret_manager_secret.soar_webhook_url.id
}
```

---

## 9. Terraform State Management & CI/CD

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

## 10. Verification, Validation & Dry-Run Guides

### 1. Terminal CLI Dry-Run Testing (Python Virtual Environment)

Run dry-run reports directly in your terminal using Python virtual environment to evaluate dynamic SLA windows, latency thresholds, consumption overages, and drift findings before applying Terraform configurations:

```bash
cd google-secops/monitoring-solution/scripts

# Activate Virtual Environment
source .venv/bin/activate

# 1. SLA Profiler & Latency Threshold Dry-Run
python3 run_profiler.py <YOUR_BYOP_PROJECT_ID> --dry-run

# 2. Consumption Forecast Engine Dry-Run (with Webhook dispatch test)
python3 forecast_engine.py <YOUR_BYOP_PROJECT_ID> \
  --terms-file contract_terms.json \
  --dry-run \
  --webhook-url https://webhook.site/b563870a-190f-4c99-b7b6-d097cc6b2bde

# 3. Configuration Drift & CATCH_ALL Route Detector Dry-Run
python3 drift_detector.py <YOUR_BYOP_PROJECT_ID> \
  --webhook-url https://webhook.site/b563870a-190f-4c99-b7b6-d097cc6b2bde
```

---

#### 📊 Sample Output: SLA Ingestion Profiler (`run_profiler.py`)
| Log Type | SLA Profile | P99 Gap | Alert Window (Human) | Alert Window (sec) | P95 Latency Thresh | Daily Avg Logs | Volume Threshold |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **AZURE_AD_AUDIT** | batch | 4h 12m | **6h 18m** | 22680 | 30m | 21 | 10 |
| **GCP_CLOUDAUDIT** | realtime | 1m | **5m** | 300 | 30m | 486,541 | 48,654 |
| **GCP_LOADBALANCING** | near_realtime | 49m | **1h 13m** | 4410 | 30m | 1,433 | 143 |
| **WORKSPACE_ACTIVITY** | batch | 3h 6m | **4h 39m** | 16740 | 30m | 25 | 10 |

---

#### 📊 Sample Output: Consumption Forecast Engine (`forecast_engine.py`)
**Active Contract Term:** Year 1 of 3

| Parameter | Value |
| :--- | :--- |
| **Calculated At** | 2026-07-23T21:57:40Z |
| **Active Term Range** | 2026-01-01T00:00:00Z to 2026-12-31T23:59:59Z (203 days elapsed, 161 remaining) |
| **Committed License Volume** | 1,825.0 GB |
| **Cumulative Ingested** | 7.9 GB (0.43% of active quota) |
| **Ideal Target Volume** | 1,017.33 GB (55.62% of term) |
| **Projected Volume (Term End)** | 14.16 GB |
| **Estimated Overage** | **0.0 GB** |
| **Webhook Status** | `Webhook alert successfully dispatched (HTTP 200)` |

---

#### 📊 Sample Output: Configuration Drift Detector (`drift_detector.py`)
| Finding Type | Log Type | Daily Avg (GB/day) | Discrepancy Action / Recommendation |
| :--- | :--- | :--- | :--- |
| **DECOMMISSIONED_OR_SILENT_SOURCE** | `AZURE_AD_CONTEXT` | 0.00 GB | Feed silent for >7 days. Remove from `monitors` or check upstream Azure collector. |
| **NEW_UNMONITORED_SOURCE** | `AWS_CLOUDTRAIL` | 14.20 GB | Newly active log feed detected. Run profiler to generate SLA and update `terraform.tfvars.json`. |
| **CATCH_ALL_LOG_TYPE_WARNING** | `CATCH_ALL` | 2.40 GB | Unparsed logs arriving at catch-all route. Inspect BindPlane/Forwarder fallback routes. |

---

### 2. Terraform Deployment Validation

After completing dry-run checks and generating `terraform.tfvars.json`, validate and apply your deployment following **Step 7** in the Deployment Guide above.

---

### 3. SOAR Environment Recommendation & Webhook Integration

> [!TIP]
> **Recommended SOAR Environment Configuration**:
> When configuring the incoming Webhook endpoint in **Google SecOps SOAR**, we strongly recommend assigning incoming health & infrastructure webhooks to a dedicated **SOAR Environment** (e.g. `Infra-Monitoring` or `SecOps-Health`):
> - **Clear Incident Separation**: Separates system telemetry and collection outage tickets from security threat detection alerts.
> - **Targeted Playbook Automation**: Enables specialized SOAR playbooks (e.g., auto-restarting a BindPlane agent or pinging feed owners) to execute automatically without clogging security analyst queues.

To test the Webhook endpoint manually from your workstation:
```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{
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
      "log_type": "WINEVTLOG"
    }
  }' \
  https://<YOUR_SOAR_INSTANCE>/api/webhooks/incoming/gcp-monitoring
```

---

## 11. Frequently Asked Questions & Customization

### Q1: How do manual SLA and Silent Host exceptions persist when the profiler updates `terraform.tfvars.json`?
> **Answer**:
> The profiler checks for an optional `scripts/overrides.json` file during every run. You can configure custom absence windows, P95 latency thresholds, or explicitly ignore specific log types or silent endpoint hosts:
> ```json
> {
>   "log_types": {
>     "AZURE_AD_AUDIT": {
>       "alert_window_seconds": 43200,
>       "latency_p95_seconds": 3600
>     },
>     "TEST_FEED": {
>       "ignore": true
>     }
>   },
>   "hosts": {
>     "decommissioned-server-01": {
>       "ignore": true
>     },
>     "critical-syslog-host-01": {
>       "alert_window_seconds": 600,
>       "ignore": false
>     }
>   }
> }
> ```
> Settings in `overrides.json` are automatically merged into the profiler output and persisted across automated weekly profiling runs. Setting `"ignore": true` on a host or log type prevents alert policies from being created for it.

### Q2: If I decommission or remove a log source, do its Cloud Monitoring alert policies get deleted?
> **Answer**:
> **Yes, automatically!** Because Terraform manages feed alert policies using `for_each = var.monitors`:
> 1. When a log feed is removed from `terraform.tfvars.json` (either manually or when `drift_detector.py` flags a decommissioned feed), `var.monitors` no longer contains that `log_type` key.
> 2. On the next `terraform apply` (triggered automatically via Cloud Build GCS trigger or manually), Terraform detects that the corresponding `google_monitoring_alert_policy.log_feed_absence["LOG_TYPE"]` resource is no longer defined in configuration.
> 3. Terraform **automatically destroys and removes** the old alert policies from Google Cloud Monitoring, maintaining zero alert drift.

---

## 12. References & Citations

### 1. Framework Architecture & ML Principles
*   **Joe Lopes - Log Health Monitoring**: [lopes.id/log/log-health-monitoring/](https://lopes.id/log/log-health-monitoring/)
*   **Gist: `log-health.gs` (Apps Script Notifications)**: [gist.github.com/lopes/041a25c7792303eb15ab600251f5c11b](https://gist.github.com/lopes/041a25c7792303eb15ab600251f5c11b)
*   **Gist: `derive-seeds.py` (Reassessment Tool)**: [gist.github.com/lopes/54809c3ac1f0ae007bb4f48cdd217f92](https://gist.github.com/lopes/54809c3ac1f0ae007bb4f48cdd217f92)

### 2. SIEM Latency & Delay Research
*   **That SIEM Guy - Identifying Late Arriving Log Sources**: [medium.com/@thatsiemguy/identifying-late-arriving-log-sources-8780b1f01836](https://medium.com/@thatsiemguy/identifying-late-arriving-log-sources-8780b1f01836)
*   **That SIEM Guy - Latency Analysis in Google SecOps**: [medium.com/@thatsiemguy/latency-analysis-in-google-secops-3f94291a82c7](https://medium.com/@thatsiemguy/latency-analysis-in-google-secops-3f94291a82c7)
*   **Google Security Operations - Understand Rule Detection Delays**: [docs.cloud.google.com/chronicle/docs/detection/detection-delays](https://docs.cloud.google.com/chronicle/docs/detection/detection-delays)

### 3. Official Google Cloud Ingestion Documentation
*   **Ingestion Overview**: [docs.cloud.google.com/chronicle/docs/ingestion/ingestion-overview](https://docs.cloud.google.com/chronicle/docs/ingestion/ingestion-overview)
*   **Understand Ingestion Metrics**: [docs.cloud.google.com/chronicle/docs/ingestion/understand-ingestion-metrics2](https://docs.cloud.google.com/chronicle/docs/ingestion/understand-ingestion-metrics2)
*   **Ingestion Notifications for Health Metrics**: [docs.cloud.google.com/chronicle/docs/ingestion/ingestion-notifications-for-health-metrics](https://docs.cloud.google.com/chronicle/docs/ingestion/ingestion-notifications-for-health-metrics)
*   **Silent Host Monitoring**: [docs.cloud.google.com/chronicle/docs/ingestion/silent-host-monitoring](https://docs.cloud.google.com/chronicle/docs/ingestion/silent-host-monitoring)
*   **View Billed Ingestion Volume**: [docs.cloud.google.com/chronicle/docs/ingestion/view-billed-ingestion-volume](https://docs.cloud.google.com/chronicle/docs/ingestion/view-billed-ingestion-volume)
*   **Analyze Feed Activity with Cloud Logging**: [docs.cloud.google.com/chronicle/docs/ingestion/analyze-feed-activity-with-cloud-logging](https://docs.cloud.google.com/chronicle/docs/ingestion/analyze-feed-activity-with-cloud-logging)
*   **Troubleshooting Ingestion**: [docs.cloud.google.com/chronicle/docs/ingestion/troubleshooting-ingestion](https://docs.cloud.google.com/chronicle/docs/ingestion/troubleshooting-ingestion)
*   **Understand Billing**: [docs.cloud.google.com/chronicle/docs/onboard/understand-billing](https://docs.cloud.google.com/chronicle/docs/onboard/understand-billing)
