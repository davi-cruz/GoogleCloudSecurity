#!/usr/bin/env python3
"""Google SecOps Monitoring Solution - Unified HTTP Orchestrator Entrypoint.

Provides a single Cloud Run Function HTTP entrypoint that dispatches to
profiler, forecast, or drift detector actions based on the incoming JSON payload.
"""

import json
import os
import sys
import functions_framework

from run_profiler import analyze_ingestion_sla, write_output as write_profiler_output
from forecast_engine import calculate_forecast, write_forecast_report, send_webhook_alert as send_forecast_alert, find_active_term
from drift_detector import load_terraform_vars, query_active_log_types, check_drift, send_webhook_alerts as send_drift_alerts

@functions_framework.http
def main_http(request):
    """Unified HTTP trigger for Cloud Run Function.
    
    Expected JSON payload:
    {
      "action": "profiler" | "forecast" | "drift",
      "project_id": "secops-gaia", (optional, defaults to env GCP_PROJECT_ID)
      "webhook_url": "https://..." (optional, defaults to env SOAR_WEBHOOK_URL)
    }
    """
    request_json = request.get_json(silent=True) or {}
    action = request_json.get("action", os.environ.get("DEFAULT_ACTION", "profiler")).lower()
    
    project_id = request_json.get("project_id") or os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    webhook_url = request_json.get("webhook_url") or os.environ.get("SOAR_WEBHOOK_URL")
    
    if not project_id:
        return ("Missing GCP_PROJECT_ID in request body or environment.", 400)

    try:
        if action == "profiler":
            gcs_blob_name = os.environ.get("OUTPUT_GCS_BLOB", "terraform.tfvars.json")
            if not gcs_bucket:
                return ("Missing OUTPUT_GCS_BUCKET environment variable.", 400)
            profiles, host_profiles = analyze_ingestion_sla(project_id)
            write_profiler_output(profiles, "", gcs_bucket, gcs_blob_name, host_profiles)
            return (json.dumps({"status": "success", "action": "profiler", "profiles_count": len(profiles)}), 200, {"Content-Type": "application/json"})

        elif action == "forecast":
            gcs_blob_name = os.environ.get("OUTPUT_FORECAST_BLOB", "forecast_vars.json")
            terms_json = os.environ.get("CONTRACT_TERMS_JSON")
            if not terms_json or not gcs_bucket:
                return ("Missing CONTRACT_TERMS_JSON or OUTPUT_GCS_BUCKET environment variable.", 400)
            terms = json.loads(terms_json)
            active_term, start_dt, end_dt, _ = find_active_term(terms)
            if not active_term:
                return ("No active contract term configuration found for current date.", 400)
            report = calculate_forecast(project_id, active_term, start_dt, end_dt)
            write_forecast_report(report, "", gcs_bucket, gcs_blob_name)
            if webhook_url:
                send_forecast_alert(report, webhook_url, project_id)
            return (json.dumps({"status": "success", "action": "forecast", "report": report}), 200, {"Content-Type": "application/json"})

        elif action == "drift":
            script_dir = os.path.dirname(os.path.abspath(__file__))
            tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
            configured = load_terraform_vars(tf_vars_path)
            active = query_active_log_types(project_id)
            alerts = check_drift(configured, active)
            if alerts and webhook_url:
                send_drift_alerts(alerts, webhook_url, project_id)
            return (json.dumps({"status": "success", "action": "drift", "discrepancies_count": len(alerts), "alerts": alerts}), 200, {"Content-Type": "application/json"})

        else:
            return (f"Invalid action '{action}'. Valid actions are: profiler, forecast, drift.", 400)

    except Exception as e:
        return (json.dumps({"status": "error", "error": str(e)}), 500, {"Content-Type": "application/json"})
