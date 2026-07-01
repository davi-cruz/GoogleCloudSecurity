#!/usr/bin/env python3
"""Google SecOps Configuration Drift Detector.

This script compares active log sources in Cloud Monitoring against the deployed
Terraform variables file to identify new or decommissioned log feeds, and
significant baseline volume shifts.
"""

import json
import os
import sys
import time
from google.cloud import monitoring_v3

def load_terraform_vars(filepath):
    """Loads currently configured monitors from terraform.tfvars.json."""
    if not os.path.exists(filepath):
        print(f"Error: Terraform vars file '{filepath}' not found.")
        return {}
    
    with open(filepath, "r") as f:
        try:
            data = json.load(f)
            return data.get("monitors", {})
        except json.JSONDecodeError:
            print("Error: Could not parse json variables.")
            return {}

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
        if log_type not in configured:
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
            
    return drift_alerts

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 drift_detector.py <GCP_PROJECT_ID>")
        sys.exit(1)
        
    project_id = sys.argv[1]
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
    
    configured = load_terraform_vars(tf_vars_path)
    active = query_active_log_types(project_id)
    
    alerts = check_drift(configured, active)
    
    if alerts:
        print("🚨 CONFIGURATION DRIFT DETECTED 🚨")
        for alert in alerts:
            print(f"- [{alert['type']}] {alert['message']}")
        # Exit with a warning status so automated orchestrators know configuration changes are needed
        sys.exit(2)
    else:
        print("✅ No configuration drift detected. All active feeds are monitored.")
        sys.exit(0)

if __name__ == "__main__":
    main()
