#!/usr/bin/env python3
"""Google SecOps Log Profiler and SLA Generator.

This script queries Google Cloud Monitoring metrics to analyze ingestion latency
and volume baseline characteristics for each active log type, automatically
recommending and generating Terraform alert variables.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import functions_framework
from google.cloud import monitoring_v3
from google.cloud import storage

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

    print(f"Querying ingestion metrics for project '{project_id}' over last {lookback_days} days...", file=sys.stderr)

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

        timestamps = []
        volumes = []
        for point in series.points:
            timestamps.append(point.interval.end_time.seconds)
            volumes.append(point.value.int64_value)

        if not timestamps:
            continue

        timestamps.reverse()
        volumes.reverse()

        gaps = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
        
        if not gaps:
            profile = "variable"
        else:
            avg_gap = sum(gaps) / len(gaps)
            
            if avg_gap <= 90:
                profile = "realtime"
            elif avg_gap <= 600:
                profile = "near_realtime"
            elif avg_gap <= 3600:
                profile = "batch"
            else:
                profile = "variable"

        total_logs = sum(volumes)
        days_span = (max(timestamps) - min(timestamps)) / 86400.0
        daily_avg_logs = total_logs / max(0.1, days_span)

        log_profiles[log_type] = {
            "log_type": log_type,
            "sla_profile": profile,
            "alert_window_seconds": SLA_PROFILES[profile]["alert_window_seconds"],
            "daily_avg_logs": int(daily_avg_logs),
            "volume_threshold": max(10, int(daily_avg_logs * 0.1))
        }

    return log_profiles

def write_output(profiles, filepath, gcs_bucket=None, gcs_blob_name=None):
    """Writes results locally or uploads to a GCS bucket."""
    tf_data = {
        "monitors": profiles
    }
    json_content = json.dumps(tf_data, indent=2)
    
    if gcs_bucket and gcs_blob_name:
        print(f"Uploading output to GCS bucket '{gcs_bucket}' as '{gcs_blob_name}'...", file=sys.stderr)
        storage_client = storage.Client()
        bucket = storage_client.bucket(gcs_bucket)
        blob = bucket.blob(gcs_blob_name)
        blob.upload_from_string(json_content, content_type="application/json")
        print("Upload successful.", file=sys.stderr)
    else:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(json_content)
        print(f"Successfully generated Terraform variables file at: {filepath}", file=sys.stderr)

def print_dry_run_report(profiles, output_format="markdown"):
    """Prints the dry run report to stdout in markdown or html format."""
    if output_format == "html":
        print("<h2>SecOps SLA Profiler Dry Run Report</h2>")
        print("<table border='1'>")
        print("  <tr><th>Log Type</th><th>SLA Profile</th><th>Alert Window (sec)</th><th>Daily Avg Logs</th><th>Volume Threshold</th></tr>")
        for log_type, p in sorted(profiles.items()):
            print(f"  <tr><td><b>{log_type}</b></td><td>{p['sla_profile']}</td><td>{p['alert_window_seconds']}</td><td>{p['daily_avg_logs']}</td><td>{p['volume_threshold']}</td></tr>")
        print("</table>")
    else:
        # Markdown default
        print("# SecOps SLA Profiler Dry Run Report\n")
        print("| Log Type | SLA Profile | Alert Window (sec) | Daily Avg Logs | Volume Threshold |")
        print("| :--- | :--- | :--- | :--- | :--- |")
        for log_type, p in sorted(profiles.items()):
            print(f"| **{log_type}** | {p['sla_profile']} | {p['alert_window_seconds']} | {p['daily_avg_logs']} | {p['volume_threshold']} |")

@functions_framework.http
def main_http(request):
    """HTTP trigger endpoint for Cloud Functions gen 2 / Cloud Run deployment."""
    project_id = os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    gcs_blob_name = os.environ.get("OUTPUT_GCS_BLOB", "terraform.tfvars.json")
    
    if not project_id or not gcs_bucket:
        return ("Missing GCP_PROJECT_ID or OUTPUT_GCS_BUCKET environment variables.", 400)
        
    try:
        profiles = analyze_ingestion_sla(project_id)
        write_output(profiles, "", gcs_bucket, gcs_blob_name)
        return ("SLA Profiling completed and tfvars written to GCS.", 200)
    except Exception as e:
        return (f"Execution failed: {str(e)}", 500)

def main():
    parser = argparse.ArgumentParser(description="Google SecOps Log Profiler and SLA Generator.")
    parser.add_argument("project_id", nargs="?", help="The GCP Project ID to query metrics from.")
    parser.add_argument("--dry-run", action="store_true", help="Print dry run reports directly to terminal without saving files.")
    parser.add_argument("--format", choices=["markdown", "html"], default="markdown", help="Formatting style for the dry-run output (markdown or html).")
    
    args = parser.parse_args()
    
    project_id = args.project_id or os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    gcs_blob_name = os.environ.get("OUTPUT_GCS_BLOB", "terraform.tfvars.json")
    
    if not project_id:
        parser.print_help()
        sys.exit(1)
        
    profiles = analyze_ingestion_sla(project_id)
    
    if args.dry_run:
        print_dry_run_report(profiles, args.format)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
        write_output(profiles, tf_vars_path, gcs_bucket, gcs_blob_name)

if __name__ == "__main__":
    main()
