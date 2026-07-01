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

## 1. Architectural Rationale: Leveraging Google's Native Infrastructure Telemetry

This solution is designed to leverage Google's robust, enterprise-grade cloud infrastructure monitoring platform (**Google Cloud Monitoring**) to oversee your ingestion pipeline health, rather than relying on duplicate, in-SIEM analytics monitoring layers.

By utilizing Google's native, dedicated metrics engine, the solution provides an efficient and highly resilient architecture for security telemetry monitoring:

```
+-------------------------------------------------------------------------------+
|                            GOOGLE SECOPS CONSOLE                              |
|                                                                               |
|  +------------------------+                    +---------------------------+  |
|  |     Custom Dashboards  |                    |      SOAR Alert Queue     |  |
|  |  (YARA-L Ingestion)    |                    |  - Outage alerts queue    |  |
|  +------------------------+                    +---------------------------+  |
|               ^ (queries metrics)                            ^                |
+---------------|----------------------------------------------|----------------+
                |                                              | (webhooks)
+------------------------------------+           +-------------+-------------+
|    Cloud Monitoring API (BYOP)     | <-------- |    Orchestration Project  |
|   - Outage / Absence Detection     |           |    - Python SLA engine    |
+------------------------------------+           +---------------------------+
```

### Strategic Benefits of This Architecture:
1.  **Platform Efficiency**: Monitoring alerts are evaluated by a dedicated, low-latency infrastructure telemetry layer. Outages and ingestion anomalies are flagged in minutes without placing analytical load on your SIEM indexing or threat hunting engines.
2.  **No Duplicate Agents or Pipelines**: Instead of deploying secondary collectors or configurations to query status, the solution queries native system metrics generated automatically by the platform's API gateways, forwarders, and normalizers.
3.  **Consolidated Analyst Workflow**: Operational signals are fully unified within the analyst's day-to-day workspace:
    *   **SOAR Incident Management**: Outage alerts are pushed via webhook directly to the **Google SecOps SOAR Alert Queue**, allowing security teams to manage and track collection issues using standard playbook workflows.
    *   **Consolidated Dashboards**: Historical log volumes, ingestion limits, and parsing errors are tracked inside **Google SecOps Custom Dashboards** using YARA-L 2.0 queries on the native `ingestion` metrics schema.

---

## 2. Compliance Alignment: OMB M-21-31
This solution satisfies the event logging (EL) requirements outlined in the White House Office of Management and Budget (OMB) Memorandum **M-21-31**:
*   **EL1 (Basic)**: Implements metric-absence alerts to guarantee continuous syslog, agent, and api data flows.
*   **EL2 (Intermediate)**: Tracks parsing and normalization health ratio rates to maintain UDM schema compliance.
*   **EL3 (Advanced)**: Routes alerts directly to SOAR playbooks for automated remediation.

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

### Step 5: Configure the Deployment Variables
Navigate to the `terraform` folder and edit the `terraform.tfvars.json` file. Provide your parameters:

```json
{
  "byop_project_id": "<YOUR_BYOP_PROJECT_ID>",
  "orchestration_project_id": "<YOUR_ORCHESTRATION_PROJECT_ID>",
  "region": "us-central1",
  "soar_webhook_url": "https://secops-instance.siemplify-soar.com/api/webhooks/incoming/gcp-monitoring",
  "contract_terms_json": "[\n  {\n    \"start_date\": \"2026-01-01T00:00:00Z\",\n    \"end_date\": \"2026-12-31T23:59:59Z\",\n    \"committed_gb\": 365000.0\n  }\n]"
}
```

*   **byop_project_id**: Project where Alert Policies are written.
*   **orchestration_project_id**: Project where GCS buckets, Secret Manager, Cloud Run Functions, and Scheduler are deployed.
*   **contract_terms_json**: The timeline of committed daily/yearly volumes for overage forecasting (represented as a serialized JSON string). 
    > [!NOTE]
    > If you do not have your contract details (committed GB pools or term dates) handy, **please reach out to your Google Cloud Representative or Account Team** to retrieve the exact allocations.

---

### Step 6: Bootstrap Ingestion Metrics (Dry-Run Check)
Before deploying, it is best practice to run the scripts in a Python Virtual Environment (`venv` or `uv`) to verify that your credentials can query Google Cloud Monitoring metrics:

#### Option A: Using Python standard `venv`
```bash
# 1. Create virtual environment
python3 -m venv venv

# 2. Activate virtual environment
source venv/bin/activate

# 3. Install required Python libraries
pip3 install -r ../scripts/requirements.txt

# 4. Run the profiler dry run (replace project ID with your BYOP Project ID)
python3 ../scripts/run_profiler.py <YOUR_BYOP_PROJECT_ID> --dry-run
```

#### Option B: Using `uv` (recommended for faster setup)
```bash
# 1. Create and activate virtual environment
uv venv
source .venv/bin/activate

# 2. Install dependencies and run the dry run
uv pip install -r ../scripts/requirements.txt
python3 ../scripts/run_profiler.py <YOUR_BYOP_PROJECT_ID> --dry-run
```
*If successful, this will output a markdown list of all log feeds detected in the environment and their SLA thresholds.*

To update the default variables file before initial provisioning, run:
```bash
python3 ../scripts/run_profiler.py <YOUR_BYOP_PROJECT_ID>
```
*(Remember to deactivate your virtual environment afterwards using the `deactivate` command).*

---

### Step 7: Provision via Terraform
Deploy the alerting policies and serverless schedulers to GCP:
```bash
# 1. Initialize Terraform plugins
terraform init

# 2. Review the plan changes to verify both project targets
terraform plan

# 3. Apply changes (deploys alerting rules to BYOP and serverless triggers to Orchestration)
terraform apply -auto-approve
```

---

## 8. Secret Manager Integration (Secrets Governance)

To comply with enterprise security practices, do not hardcode the `soar_webhook_url` (which contains integration secrets/tokens) in cleartext variables. Use **Secret Manager**:

### 1. Provision the Secret in GCP
```bash
# Create the secret holder
gcloud secrets create secops-soar-webhook-url --replication-policy="automatic"

# Add the webhook URL version payload
echo -n "https://secops-instance.siemplify-soar.com/api/webhooks/incoming/gcp-monitoring" | \
  gcloud secrets versions add secops-soar-webhook-url --data-file=-
```

### 2. IAM Permissions for Secrets
*   **Provisioner Persona**: Needs `roles/secretmanager.admin` to manage the secret infrastructure.
*   **Terraform Service Account**: Needs `roles/secretmanager.secretAccessor` to retrieve the secret during plan/apply.

### 3. Retrieve Secret version in Terraform
In your Terraform configuration, fetch the webhook URL dynamically using a data block:
```hcl
data "google_secret_manager_secret_version" "soar_webhook" {
  secret = "secops-soar-webhook-url"
}

# Map the data version payload to the notification channel
resource "google_monitoring_notification_channel" "soar_webhook" {
  display_name = "SecOps SOAR Webhook Gateway"
  type         = "webhook_tokenauth"
  labels = {
    url = data.google_secret_manager_secret_version.soar_webhook.secret_data
  }
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

Before deploying configurations, use the built-in **Dry Run** flag to evaluate calculations directly inside your terminal in markdown or HTML formats.

### 1. Profiler Dry Run
Queries Cloud Monitoring API and prints the suggested SLA window configurations as a markdown table:
```bash
# Run local dry run
python3 scripts/run_profiler.py <YOUR_BYOP_PROJECT_ID> --dry-run --format markdown
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
  <YOUR_BYOP_PROJECT_ID> \
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

---

## 11. Customization & Adjustments
*   **Adjusting SLA Thresholds**: To manually override a feed's SLA window (e.g. increase an Azure feed's absence alert threshold to 12 hours), edit the corresponding entry block in `terraform.tfvars.json` and redeploy.
*   **Managing Exclusions**: If a test collector or log type triggers false positive alerts, exclude it inside `terraform/main.tf` by appending filter rules (e.g. `AND metric.labels.log_type != "DUMMY_SOURCE"`).
*   **Manual Trigger**: You can trigger the SLA profiling script immediately from the GCP Console (Cloud Functions page) or by running the weekly Cloud Scheduler job manually using:
    ```bash
    gcloud scheduler jobs run secops-profiler-weekly-trigger --location=us-central1
    ```
