#!/usr/bin/env python3
"""Google SecOps Log Profiler and SLA Generator.

This script queries Google Cloud Monitoring metrics to analyze ingestion latency
and volume baseline characteristics for each active log type, automatically
recommending and generating Terraform alert variables.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from google.cloud import monitoring_v3

# Default SLA Mapping rules
# Threshold is 99% of logs arrived within:
# - Realtime: 5 minutes
# - Near Realtime: 20 minutes
# - Batch: 2 hours
# - Variable: 12 hours (unpredictable/slow SaaS APIs)
SLA_PROFILES = {
    "realtime": {"alert_window_seconds": 300, "description": "Streaming feeds (Syslog, BindPlane)"},
    "near_realtime": {"alert_window_seconds": 1200, "description": "Micro-batch feeds (e.g. AWS S3 polling)"},
    "batch": {"alert_window_seconds": 7200, "description": "Scheduled bulk ingestion (e.g. hourly GCS)"},
    "variable": {"alert_window_seconds": 43200, "description": "High latency SaaS API feeds"},
}

def analyze_ingestion_sla(project_id, lookback_days=15):
    """Queries Cloud Monitoring API to determine ingestion characteristics and SLA."""
    client = monitoring_v3.MetricServiceClient()
    name = f"projects/{project_id}"

    now = time.time()
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(now), "nanos": 0},
            "start_time": {"seconds": int(now - lookback_days * 24 * 3600), "nanos": 0},
        }
    )

    print(f"Querying ingestion metrics for project '{project_id}' over last {lookback_days} days...")

    # We fetch the record_count metric to understand rate of logs arriving
    results = client.list_time_series(
        request={
            "name": name,
            "filter": 'metric.type = "chronicle.googleapis.com/ingestion/log/record_count"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )

    log_profiles = {}

    for series in results:
        labels = series.metric.labels
        log_type = labels.get("log_type", "UNKNOWN")
        if not log_type or log_type == "FORWARDER_HEARTBEAT":
            continue

        # Extract timestamps to check ingestion frequency and points density
        timestamps = []
        volumes = []
        for point in series.points:
            timestamps.append(point.interval.end_time.seconds)
            volumes.append(point.value.int64_value)

        if not timestamps:
            continue

        # Sort chronological
        timestamps.reverse()
        volumes.reverse()

        # Calculate time intervals between points (in seconds)
        gaps = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
        
        if not gaps:
            # Single data point seen, fallback to variable
            profile = "variable"
        else:
            avg_gap = sum(gaps) / len(gaps)
            max_gap = max(gaps)
            
            # Profile assignment based on metric emission patterns
            if avg_gap <= 90: # logs arrive continuously (e.g. within 1.5 minutes)
                profile = "realtime"
            elif avg_gap <= 600: # logs arrive regularly in small windows
                profile = "near_realtime"
            elif avg_gap <= 3600: # hourly runs
                profile = "batch"
            else:
                profile = "variable"

        # Calculate daily ingestion rate (approximate count of logs)
        total_logs = sum(volumes)
        days_span = (max(timestamps) - min(timestamps)) / 86400.0
        daily_avg_logs = total_logs / max(0.1, days_span)

        log_profiles[log_type] = {
            "log_type": log_type,
            "sla_profile": profile,
            "alert_window_seconds": SLA_PROFILES[profile]["alert_window_seconds"],
            "daily_avg_logs": int(daily_avg_logs),
            "volume_threshold": max(10, int(daily_avg_logs * 0.1)) # Alert when logs fall below 10% of avg rate
        }

    return log_profiles

def write_terraform_vars(profiles, filepath):
    """Writes mapped results to terraform.tfvars.json."""
    tf_data = {
        "monitors": profiles
    }
    
    # Ensure folder path exists
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(tf_data, f, indent=2)
    print(f"Successfully generated Terraform variables file at: {filepath}")

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run_profiler.py <GCP_PROJECT_ID>")
        sys.exit(1)
        
    project_id = sys.argv[1]
    profiles = analyze_ingestion_sla(project_id)
    
    # Target directory relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
    
    write_terraform_vars(profiles, tf_vars_path)

if __name__ == "__main__":
    main()
