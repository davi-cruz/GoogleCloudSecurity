#!/usr/bin/env python3
"""Google SecOps Consumption Forecast Engine.

Uses Cloud Monitoring Ingestion metrics to project contract consumption velocity
and estimate remaining contract runway without requiring BigQuery. Supports
multi-year contracts with multiple terms configured at once.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import functions_framework
from google.cloud import monitoring_v3

from secops_monitoring_utils import send_soar_webhook_alert, write_json_output

def send_webhook_alert(report, webhook_url, project_id):
    """Sends a SOAR-formatted webhook alert if an ingestion overage is projected."""
    overage = report.get("projected_overage_gb", 0.0)
    committed = report.get("committed_volume_gb", 0.0)
    pct_consumed = report.get("pct_commitment_consumed", 0.0)
    pct_elapsed = report.get("pct_time_elapsed", 0.0)
    
    if overage <= 0 and pct_consumed <= pct_elapsed * 1.15:
        print("Forecast trajectory within bounds; no overage webhook required.", file=sys.stderr)
        return
        
    severity = "Critical" if overage > 0 else "Warning"
    message = f"Projected contract overage of {overage} GB. Ingestion consumed {pct_consumed}% of license vs {pct_elapsed}% term elapsed."
    description = f"Contract term ({report['active_term_start']} to {report['active_term_end']}): Committed {committed} GB, Ingested {report['cumulative_ingestion_gb']} GB. Projected term end volume: {report['projected_total_volume_gb']} GB."
    
    custom_fields = {
        "project_id": project_id,
        "committed_volume_gb": str(committed),
        "cumulative_ingestion_gb": str(report['cumulative_ingestion_gb']),
        "projected_overage_gb": str(overage),
        "pct_commitment_consumed": str(pct_consumed),
        "pct_time_elapsed": str(pct_elapsed)
    }
    
    send_soar_webhook_alert(
        webhook_url=webhook_url,
        project_id=project_id,
        event_type="Contract Overage Forecast Warning",
        source_rule="SecOps Contract Consumption Velocity Warning",
        message=message,
        description=description,
        severity=severity,
        custom_fields=custom_fields
    )

def find_active_term(terms):
    """Identifies the contract term active for the current date."""
    now = datetime.now(timezone.utc)
    for index, term in enumerate(terms):
        try:
            start = datetime.fromisoformat(term["start_date"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(term["end_date"].replace("Z", "+00:00"))
            if start <= now <= end:
                return term, start, end, index + 1
        except (ValueError, KeyError) as e:
            print(f"Error parsing contract term index {index}: {e}", file=sys.stderr)
            continue
    return None, None, None, None

def calculate_forecast(project_id, active_term, contract_start, contract_end):
    """Calculates linear usage runway and overage forecast for the active term."""
    client = monitoring_v3.MetricServiceClient()
    name = f"projects/{project_id}"
    committed_gb = float(active_term["committed_gb"])
    
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(now.timestamp()), "nanos": 0},
            "start_time": {"seconds": int(contract_start.timestamp()), "nanos": 0},
        }
    )
    
    print(f"Aggregating consumption for active term (Start: {active_term['start_date']} End: {active_term['end_date']} Quota: {committed_gb} GB)...", file=sys.stderr)
    
    results = client.list_time_series(
        request={
            "name": name,
            "filter": 'metric.type = "chronicle.googleapis.com/ingestion/log/bytes_count" AND resource.labels.log_type != "FORWARDER_HEARTBEAT"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    
    total_bytes = 0
    for series in results:
        for point in series.points:
            total_bytes += point.value.int64_value
            
    total_contract_days = (contract_end - contract_start).days
    days_elapsed = (now - contract_start).days
    days_remaining = (contract_end - now).days
    
    cumulative_gb = total_bytes / (1024 ** 3)
    avg_daily_gb = cumulative_gb / max(1, days_elapsed)
    
    projected_total_gb = cumulative_gb + (avg_daily_gb * max(0, days_remaining))
    projected_overage_gb = max(0.0, projected_total_gb - committed_gb)
    
    pct_time_elapsed = (days_elapsed / total_contract_days) * 100
    pct_commitment_consumed = (cumulative_gb / committed_gb) * 100
    
    ideal_cumulative_gb = (committed_gb / total_contract_days) * days_elapsed
    
    report = {
        "calculated_at": now.isoformat(),
        "active_term_start": active_term["start_date"],
        "active_term_end": active_term["end_date"],
        "committed_volume_gb": committed_gb,
        "cumulative_ingestion_gb": round(cumulative_gb, 2),
        "ideal_cumulative_gb": round(ideal_cumulative_gb, 2),
        "projected_total_volume_gb": round(projected_total_gb, 2),
        "projected_overage_gb": round(projected_overage_gb, 2),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "pct_time_elapsed": round(pct_time_elapsed, 2),
        "pct_commitment_consumed": round(pct_commitment_consumed, 2),
    }
    
    return report

def write_forecast_report(report, filepath, gcs_bucket=None, gcs_blob_name=None):
    """Writes forecast output to local file or GCS bucket."""
    write_json_output(report, filepath, gcs_bucket, gcs_blob_name)

def print_dry_run_report(report, term_index, total_terms, output_format="markdown"):
    """Prints the dry run report in markdown or HTML."""
    if output_format == "html":
        print("<h2>SecOps Consumption Forecast Engine Report</h2>")
        print(f"<p><b>Active Contract Term:</b> Year {term_index} of {total_terms}</p>")
        print("<table border='1'>")
        print(f"  <tr><td><b>Calculated At</b></td><td>{report['calculated_at']}</td></tr>")
        print(f"  <tr><td><b>Active Term Range</b></td><td>{report['active_term_start']} to {report['active_term_end']}</td></tr>")
        print(f"  <tr><td><b>Days Elapsed / Remaining</b></td><td>{report['days_elapsed']} days / {report['days_remaining']} days</td></tr>")
        print(f"  <tr><td><b>Committed License</b></td><td>{report['committed_volume_gb']} GB</td></tr>")
        print(f"  <tr><td><b>Cumulative Ingested</b></td><td>{report['cumulative_ingestion_gb']} GB ({report['pct_commitment_consumed']}% consumed)</td></tr>")
        print(f"  <tr><td><b>Ideal Volume Trajectory</b></td><td>{report['ideal_cumulative_gb']} GB ({report['pct_time_elapsed']}% time elapsed)</td></tr>")
        print(f"  <tr><td><b>Projected Volume at Term End</b></td><td>{report['projected_total_volume_gb']} GB</td></tr>")
        print(f"  <tr><td><b>Projected Overage Volume</b></td><td><font color='red'><b>{report['projected_overage_gb']} GB</b></font></td></tr>")
        print("</table>")
    else:
        # Markdown
        print("# SecOps Consumption Forecast Engine Report\n")
        print(f"**Active Contract Term:** Year {term_index} of {total_terms}\n")
        print("| Parameter | Value |")
        print("| :--- | :--- |")
        print(f"| **Calculated At** | {report['calculated_at']} |")
        print(f"| **Active Term Range** | {report['active_term_start']} to {report['active_term_end']} ({report['days_elapsed']} days elapsed, {report['days_remaining']} remaining) |")
        print(f"| **Committed License Volume** | {report['committed_volume_gb']} GB |")
        print(f"| **Cumulative Ingested** | {report['cumulative_ingestion_gb']} GB ({report['pct_commitment_consumed']}% of active quota) |")
        print(f"| **Ideal Target Volume** | {report['ideal_cumulative_gb']} GB ({report['pct_time_elapsed']}% of term) |")
        print(f"| **Projected Volume (Term End)** | {report['projected_total_volume_gb']} GB |")
        print(f"| **Estimated Overage** | **{report['projected_overage_gb']} GB** |")

@functions_framework.http
def main_http(request):
    """HTTP trigger entrypoint for Cloud Functions gen 2."""
    project_id = os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    gcs_blob_name = os.environ.get("OUTPUT_FORECAST_BLOB", "forecast_vars.json")
    terms_json = os.environ.get("CONTRACT_TERMS_JSON")
    webhook_url = os.environ.get("SOAR_WEBHOOK_URL")
    
    if not project_id or not gcs_bucket or not terms_json:
        return ("Missing GCP_PROJECT_ID, OUTPUT_GCS_BUCKET, or CONTRACT_TERMS_JSON environment variables.", 400)
        
    try:
        terms = json.loads(terms_json)
        active_term, start_dt, end_dt, _ = find_active_term(terms)
        if not active_term:
            return ("No active contract term configuration found for the current date.", 400)
            
        report = calculate_forecast(project_id, active_term, start_dt, end_dt)
        write_forecast_report(report, "", gcs_bucket, gcs_blob_name)
        if webhook_url:
            send_webhook_alert(report, webhook_url, project_id)
        return ("Ingestion forecast completed and report written to GCS.", 200)
    except Exception as e:
        return (f"Execution failed: {str(e)}", 500)

def main():
    parser = argparse.ArgumentParser(description="Google SecOps Consumption Forecast Engine (Multi-Term Support).")
    parser.add_argument("project_id", nargs="?", help="The GCP Project ID.")
    parser.add_argument("--terms-file", help="Path to local JSON file containing array of contract term configurations.")
    parser.add_argument("--webhook-url", help="Optional Webhook URL to dispatch overage alerts.")
    parser.add_argument("--dry-run", action="store_true", help="Print dry run reports directly to terminal.")
    parser.add_argument("--format", choices=["markdown", "html"], default="markdown", help="Format for dry-run reports.")
    
    args = parser.parse_args()
    
    project_id = args.project_id or os.environ.get("GCP_PROJECT_ID")
    gcs_bucket = os.environ.get("OUTPUT_GCS_BUCKET")
    gcs_blob_name = os.environ.get("OUTPUT_FORECAST_BLOB", "forecast_vars.json")
    webhook_url = args.webhook_url or os.environ.get("SOAR_WEBHOOK_URL")
    
    terms = []
    if args.terms_file:
        if os.path.exists(args.terms_file):
            with open(args.terms_file, "r") as f:
                terms = json.load(f)
        else:
            print(f"Error: Terms file '{args.terms_file}' not found.", file=sys.stderr)
            sys.exit(1)
    else:
        terms_json = os.environ.get("CONTRACT_TERMS_JSON")
        if terms_json:
            try:
                terms = json.loads(terms_json)
            except json.JSONDecodeError as e:
                print(f"Error decoding CONTRACT_TERMS_JSON environment variable: {e}", file=sys.stderr)
                sys.exit(1)
                
    if not project_id or not terms:
        print("Error: Missing project_id or contract terms configuration. Review CLI help or environment configurations.", file=sys.stderr)
        parser.print_help()
        sys.exit(1)
        
    active_term, start_dt, end_dt, term_index = find_active_term(terms)
    if not active_term:
        print("❌ Error: No active contract term configuration found for the current date.", file=sys.stderr)
        sys.exit(1)
        
    report = calculate_forecast(project_id, active_term, start_dt, end_dt)
    
    if args.dry_run:
        print_dry_run_report(report, term_index, len(terms), args.format)
    else:
        print("\n--- CONSUMPTION FORECAST ENGINE REPORT ---", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_output_path = os.path.abspath(os.path.join(script_dir, "../terraform/forecast_vars.json"))
        write_forecast_report(report, local_output_path, gcs_bucket, gcs_blob_name)
    
    if webhook_url:
        send_webhook_alert(report, webhook_url, project_id)
        
    if report["pct_commitment_consumed"] > report["pct_time_elapsed"] * 1.15:
        print("\n⚠️ WARNING: Ingestion rate exceeds ideal contract timeline trajectory.", file=sys.stderr)
        sys.exit(3)
        
    sys.exit(0)

if __name__ == "__main__":
    main()
