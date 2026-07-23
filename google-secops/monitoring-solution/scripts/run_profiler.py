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

from secops_monitoring_utils import format_human_duration, write_json_output

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
        labels = {}
        for k, v in series.resource.labels.items():
            labels[k] = v
        for k, v in series.metric.labels.items():
            labels[k] = v
            
        log_type = labels.get("log_type", "UNKNOWN")
        if not log_type or log_type == "FORWARDER_HEARTBEAT":
            continue

        timestamps = []
        volumes = []
        for point in series.points:
            timestamps.append(int(point.interval.end_time.timestamp()))
            volumes.append(point.value.int64_value)

        if not timestamps:
            continue

        timestamps.reverse()
        volumes.reverse()

        gaps = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
        
        if not gaps:
            profile = "variable"
            p99_gap = 28800
        else:
            gaps.sort()
            p99_idx = int(len(gaps) * 0.99)
            p99_gap = gaps[min(p99_idx, len(gaps) - 1)]
            avg_gap = sum(gaps) / len(gaps)
            
            if avg_gap <= 90:
                profile = "realtime"
            elif avg_gap <= 600:
                profile = "near_realtime"
            elif avg_gap <= 3600:
                profile = "batch"
            else:
                profile = "variable"

        # Calculate dynamic heartbeat window (1.5 * p99_gap clamped between 300s and 1209600s / 14 days)
        dynamic_alert_window = max(300, min(1209600, int(p99_gap * 1.5)))
        # Override with SLA profile window if smaller for realtime feeds
        alert_window = min(SLA_PROFILES[profile]["alert_window_seconds"], dynamic_alert_window) if profile == "realtime" else dynamic_alert_window

        total_logs = sum(volumes)
        days_span = (max(timestamps) - min(timestamps)) / 86400.0
        daily_avg_logs = total_logs / max(0.1, days_span)

        log_profiles[log_type] = {
            "log_type": log_type,
            "sla_profile": profile,
            "p99_gap_seconds": p99_gap,
            "alert_window_seconds": alert_window,
            "alert_window_human": format_human_duration(alert_window),
            "latency_p95_seconds": 1800,  # 30 minutes default P95 latency threshold
            "latency_p95_human": "30m",
            "daily_avg_logs": int(daily_avg_logs),
            "volume_threshold": max(10, int(daily_avg_logs * 0.1))
        }

    # Merge persistent manual exceptions if overrides.json exists
    script_dir = os.path.dirname(os.path.abspath(__file__))
    overrides_path = os.path.join(script_dir, "overrides.json")
    log_profiles, host_profiles = apply_manual_overrides(log_profiles, overrides_path)

    return log_profiles, host_profiles

def apply_manual_overrides(log_profiles, overrides_filepath):
    """Merges manual log type and host SLA overrides from overrides.json."""
    host_profiles = {}
    if not os.path.exists(overrides_filepath):
        return log_profiles, host_profiles
        
    try:
        with open(overrides_filepath, "r") as f:
            data = json.load(f)
            
        # Support legacy direct dictionary or nested "log_types" / "hosts" format
        log_overrides = data.get("log_types", data if not ("log_types" in data or "hosts" in data) else {})
        host_overrides = data.get("hosts", {})
            
        for log_type, override in log_overrides.items():
            if log_type in log_profiles:
                if "alert_window_seconds" in override:
                    w = override["alert_window_seconds"]
                    log_profiles[log_type]["alert_window_seconds"] = w
                    log_profiles[log_type]["alert_window_human"] = format_human_duration(w)
                if "latency_p95_seconds" in override:
                    l = override["latency_p95_seconds"]
                    log_profiles[log_type]["latency_p95_seconds"] = l
                    log_profiles[log_type]["latency_p95_human"] = format_human_duration(l)
                if "volume_threshold" in override:
                    log_profiles[log_type]["volume_threshold"] = override["volume_threshold"]
                if override.get("ignore", False):
                    del log_profiles[log_type]
            elif not override.get("ignore", False):
                log_profiles[log_type] = {
                    "log_type": log_type,
                    "sla_profile": override.get("sla_profile", "manual"),
                    "p99_gap_seconds": override.get("alert_window_seconds", 3600),
                    "alert_window_seconds": override.get("alert_window_seconds", 3600),
                    "alert_window_human": format_human_duration(override.get("alert_window_seconds", 3600)),
                    "latency_p95_seconds": override.get("latency_p95_seconds", 1800),
                    "latency_p95_human": format_human_duration(override.get("latency_p95_seconds", 1800)),
                    "daily_avg_logs": 0,
                    "volume_threshold": override.get("volume_threshold", 10)
                }

        for host_name, override in host_overrides.items():
            w = override.get("alert_window_seconds", 600)
            host_profiles[host_name] = {
                "host_name": host_name,
                "alert_window_seconds": w,
                "alert_window_human": format_human_duration(w),
                "ignore": override.get("ignore", False)
            }

    except Exception as e:
        print(f"Warning: Failed to load overrides file '{overrides_filepath}': {e}", file=sys.stderr)
        
    return log_profiles, host_profiles

def write_output(profiles, filepath, gcs_bucket=None, gcs_blob_name=None, host_profiles=None):
    """Writes results locally or uploads to a GCS bucket, merging contract terms and existing tfvars."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Read existing tfvars if present to preserve user project IDs/region
    tf_data = {}
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                tf_data = json.load(f)
        except Exception:
            tf_data = {}

    tf_data["monitors"] = profiles
    tf_data["host_monitors"] = host_profiles or {}

    # Auto-serialize contract_terms.json if present
    terms_path = os.path.join(script_dir, "contract_terms.json")
    if os.path.exists(terms_path):
        try:
            with open(terms_path, "r") as f:
                terms_data = json.load(f)
                tf_data["contract_terms_json"] = json.dumps(terms_data)
        except Exception as e:
            print(f"Warning: Could not auto-serialize contract_terms.json: {e}", file=sys.stderr)

    write_json_output(tf_data, filepath, gcs_bucket, gcs_blob_name)

def print_dry_run_report(profiles, output_format="markdown"):
    """Prints the dry run report to stdout in markdown or html format."""
    if output_format == "html":
        print("<h2>SecOps Log Ingestion & SLA Profiler Dry Run Report</h2>")
        print("<table border='1'>")
        print("  <tr><th>Log Type</th><th>SLA Profile</th><th>P99 Gap</th><th>Alert Window (Human)</th><th>Alert Window (sec)</th><th>P95 Latency Thresh</th><th>Daily Avg Logs</th><th>Volume Thresh</th></tr>")
        for log_type, p in sorted(profiles.items()):
            print(f"  <tr><td><b>{log_type}</b></td><td>{p['sla_profile']}</td><td>{format_human_duration(p['p99_gap_seconds'])}</td><td><b>{p['alert_window_human']}</b></td><td>{p['alert_window_seconds']}</td><td>{p['latency_p95_human']}</td><td>{p['daily_avg_logs']}</td><td>{p['volume_threshold']}</td></tr>")
        print("</table>")
    else:
        # Markdown default
        print("# SecOps Log Ingestion & SLA Profiler Dry Run Report\n")
        print("| Log Type | SLA Profile | P99 Gap | Alert Window (Human) | Alert Window (sec) | P95 Latency Thresh | Daily Avg Logs | Volume Threshold |")
        print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for log_type, p in sorted(profiles.items()):
            print(f"| **{log_type}** | {p['sla_profile']} | {format_human_duration(p['p99_gap_seconds'])} | **{p['alert_window_human']}** | {p['alert_window_seconds']} | {p['latency_p95_human']} | {p['daily_avg_logs']} | {p['volume_threshold']} |")

@functions_framework.http
def main_http(request):
    """HTTP trigger endpoint for Cloud Functions gen 2 / Cloud Run deployment."""
    project_id = os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    gcs_blob_name = os.environ.get("OUTPUT_GCS_BLOB", "terraform.tfvars.json")
    
    if not project_id or not gcs_bucket:
        return ("Missing GCP_PROJECT_ID or OUTPUT_GCS_BUCKET environment variables.", 400)
        
    try:
        profiles, host_profiles = analyze_ingestion_sla(project_id)
        write_output(profiles, "", gcs_bucket, gcs_blob_name, host_profiles)
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
        
    profiles, host_profiles = analyze_ingestion_sla(project_id)
    
    if args.dry_run:
        print_dry_run_report(profiles, args.format)
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        tf_vars_path = os.path.abspath(os.path.join(script_dir, "../terraform/terraform.tfvars.json"))
        write_output(profiles, tf_vars_path, gcs_bucket, gcs_blob_name, host_profiles)

if __name__ == "__main__":
    main()
