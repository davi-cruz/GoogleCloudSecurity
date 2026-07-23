import argparse
import json
import os
import sys
import time
from google.cloud import monitoring_v3

from secops_monitoring_utils import send_soar_webhook_alert, load_json_file

def load_terraform_vars(filepath):
    """Loads currently configured monitors from terraform.tfvars.json."""
    data = load_json_file(filepath)
    return data.get("monitors", {})

def query_active_log_types(project_id):
    """Queries Cloud Monitoring for all active log types in the last 7 days."""
    client = monitoring_v3.MetricServiceClient()
    name = f"projects/{project_id}"
    
    now = time.time()
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(now), "nanos": 0},
            "start_time": {"seconds": int(now - 7 * 24 * 3600), "nanos": 0},
        }
    )
    
    results = client.list_time_series(
        request={
            "name": name,
            "filter": 'metric.type = "chronicle.googleapis.com/ingestion/log/bytes_count"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    
    active_sources = {}
    for series in results:
        labels = series.metric.labels
        log_type = labels.get("log_type")
        if not log_type or log_type == "FORWARDER_HEARTBEAT":
            continue
            
        # Sum volume to verify active ingestion
        total_bytes = sum(point.value.int64_value for point in series.points)
        if total_bytes > 0:
            # Calculate average daily rate
            days = 7
            daily_avg = total_bytes / (days * 1024 * 1024 * 1024) # daily GB
            active_sources[log_type] = daily_avg
            
    return active_sources

def check_drift(configured, active):
    """Compares configured vs active sources and returns discrepancies."""
    drift_alerts = []
    
    # 1. Check for newly discovered unmonitored feeds
    for log_type in active:
        if log_type not in configured and log_type != "CATCH_ALL":
            drift_alerts.append({
                "type": "NEW_UNMONITORED_SOURCE",
                "log_type": log_type,
                "message": f"Log type '{log_type}' is actively ingesting but is not configured in monitoring variables."
            })
            
    # 2. Check for stale/decommissioned monitored feeds
    for log_type in configured:
        if log_type not in active:
            drift_alerts.append({
                "type": "DECOMMISSIONED_OR_SILENT_SOURCE",
                "log_type": log_type,
                "message": f"Log type '{log_type}' is configured in monitoring variables but has sent zero bytes over the last 7 days."
            })

    # 3. Check for CATCH_ALL log type presence / growth
    if "CATCH_ALL" in active:
        drift_alerts.append({
            "type": "CATCH_ALL_LOG_TYPE_WARNING",
            "log_type": "CATCH_ALL",
            "message": f"Log type 'CATCH_ALL' is active (Avg daily rate: {active['CATCH_ALL']:.2f} GB/day). Review BindPlane/Forwarder fallback routes."
        })
            
    return drift_alerts

def send_webhook_alerts(alerts, webhook_url, project_id):
    """Sends SOAR-formatted webhook alert payload for configuration drift findings."""
    if not webhook_url or not alerts:
        return
        
    messages = [a["message"] for a in alerts]
    summary_msg = f"Configuration drift detected on project {project_id}: {len(alerts)} discrepancies found.\n" + "\n".join(messages[:5])
    description = f"Detailed drift findings: {json.dumps(alerts)}"
    custom_fields = {
        "project_id": project_id,
        "discrepancies_count": str(len(alerts))
    }
    
    send_soar_webhook_alert(
        webhook_url=webhook_url,
        project_id=project_id,
        event_type="Configuration Drift Warning",
        source_rule="SecOps Configuration Drift Detector",
        message=summary_msg,
        description=description,
        severity="High",
        custom_fields=custom_fields
    )

def main():
    parser = argparse.ArgumentParser(description="Google SecOps Configuration Drift Detector.")
    parser.add_argument("project_id", nargs="?", help="The GCP Project ID to query metrics from.")
    parser.add_argument("--webhook-url", help="Optional Webhook URL to dispatch drift alerts.")
    
    args = parser.parse_args()
    project_id = args.project_id or os.environ.get("GCP_PROJECT_ID")
    webhook_url = args.webhook_url or os.environ.get("SOAR_WEBHOOK_URL")
    
    if not project_id:
        parser.print_help()
        sys.exit(1)
        
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
    
    configured = load_terraform_vars(tf_vars_path)
    active = query_active_log_types(project_id)
    
    alerts = check_drift(configured, active)
    
    if alerts:
        print("🚨 CONFIGURATION DRIFT DETECTED 🚨")
        for alert in alerts:
            print(f"- [{alert['type']}] {alert['message']}")
            
        if webhook_url:
            send_webhook_alerts(alerts, webhook_url, project_id)
            
        # Exit with warning status
        sys.exit(2)
    else:
        print("✅ No configuration drift detected. All active feeds are monitored.")
        sys.exit(0)

if __name__ == "__main__":
    main()
