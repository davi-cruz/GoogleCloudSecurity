#!/usr/bin/env python3
"""Google SecOps Consumption Forecast Engine (Multi-Tenant & Multi-Term Support).

Uses Cloud Monitoring Ingestion metrics across one or multiple tenant BYOP projects
to project contract consumption velocity, verify percentage-based quota allocations
(fair-share consumption), and estimate remaining contract runway without requiring BigQuery.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
import functions_framework
from google.cloud import monitoring_v3

from secops_monitoring_utils import send_soar_webhook_alert, write_json_output, load_json_config

def send_webhook_alert(report, webhook_url, project_id):
    """Sends SOAR-formatted webhook alerts for global contract overage and tenant-level proportion anomalies."""
    if not webhook_url:
        return

    # 1. Global Master Contract Overage Alert
    global_overage = report.get("projected_overage_gb", 0.0)
    committed = report.get("committed_volume_gb", 0.0)
    pct_consumed = report.get("pct_commitment_consumed", 0.0)
    pct_elapsed = report.get("pct_time_elapsed", 0.0)

    if global_overage > 0 or pct_consumed > pct_elapsed * 1.15:
        severity = "Critical" if global_overage > 0 else "Warning"
        message = f"Projected global contract overage of {global_overage} GB. Ingestion consumed {pct_consumed}% of license vs {pct_elapsed}% term elapsed."
        description = (
            f"Master Contract term ({report['active_term_start']} to {report['active_term_end']}): "
            f"Committed {committed} GB, Ingested {report['cumulative_ingestion_gb']} GB. "
            f"Projected term end volume: {report['projected_total_volume_gb']} GB."
        )
        custom_fields = {
            "project_id": project_id,
            "committed_volume_gb": str(committed),
            "cumulative_ingestion_gb": str(report["cumulative_ingestion_gb"]),
            "projected_overage_gb": str(global_overage),
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

    # 2. Per-Tenant Overage and Proportion Drift Alerts
    if report.get("is_multi_tenant"):
        tenants = report.get("tenants", {})
        for t_id, t_info in tenants.items():
            status = t_info.get("status")
            t_name = t_info.get("name", t_id)
            t_proj_overage = t_info.get("projected_overage_gb", 0.0)
            t_dev = t_info.get("share_deviation_pct", 0.0)
            t_proj_vol = t_info.get("projected_total_volume_gb", 0.0)
            t_alloc = t_info.get("allocated_quota_gb", 0.0)
            t_actual_share = t_info.get("actual_share_pct", 0.0)
            t_quota_share = t_info.get("quota_share_pct", 0.0)
            t_project_id = t_info.get("byop_project_id", project_id)

            if status in ("CRITICAL_OVERAGE_AND_SKEW", "PROJECTED_OVERAGE"):
                severity = "Critical"
                event_type = "Tenant Ingestion Quota Overage Warning"
                rule_name = f"SecOps Tenant Quota Overage - {t_name}"
                msg = (
                    f"Tenant '{t_name}' ({t_id}) is projected to exceed allocated quota by {t_proj_overage} GB "
                    f"({t_proj_vol} GB projected vs {t_alloc} GB allocated). Actual share: {t_actual_share}% vs {t_quota_share}% contracted."
                )
                desc = (
                    f"Tenant {t_name} (Project: {t_project_id}) has consumed {t_info.get('cumulative_ingestion_gb')} GB "
                    f"at an average of {t_info.get('avg_daily_rate_gb')} GB/day. "
                    f"Projected total volume of {t_proj_vol} GB exceeds allocated quota of {t_alloc} GB."
                )
                custom_fields = {
                    "tenant_id": t_id,
                    "tenant_name": t_name,
                    "project_id": t_project_id,
                    "allocated_quota_gb": str(t_alloc),
                    "cumulative_ingestion_gb": str(t_info.get("cumulative_ingestion_gb")),
                    "actual_share_pct": str(t_actual_share),
                    "quota_share_pct": str(t_quota_share),
                    "share_deviation_pct": str(t_dev),
                    "projected_overage_gb": str(t_proj_overage),
                    "status": status
                }
                send_soar_webhook_alert(
                    webhook_url=webhook_url,
                    project_id=t_project_id,
                    event_type=event_type,
                    source_rule=rule_name,
                    message=msg,
                    description=desc,
                    severity=severity,
                    custom_fields=custom_fields
                )
            elif status == "OVER_PROPORTION_WARNING":
                severity = "Warning"
                event_type = "Tenant Consumption Proportion Drift Warning"
                rule_name = f"SecOps Tenant Proportion Drift - {t_name}"
                msg = (
                    f"Tenant '{t_name}' ({t_id}) is consuming {t_actual_share}% of master quota "
                    f"vs {t_quota_share}% contracted (+{t_dev}% deviation)."
                )
                desc = (
                    f"Tenant {t_name} (Project: {t_project_id}) consumption proportion has drifted significantly "
                    f"above contracted fair-share (+{t_dev}%). Investigate feed ingestion rate or rebalance quota."
                )
                custom_fields = {
                    "tenant_id": t_id,
                    "tenant_name": t_name,
                    "project_id": t_project_id,
                    "allocated_quota_gb": str(t_alloc),
                    "cumulative_ingestion_gb": str(t_info.get("cumulative_ingestion_gb")),
                    "actual_share_pct": str(t_actual_share),
                    "quota_share_pct": str(t_quota_share),
                    "share_deviation_pct": str(t_dev),
                    "projected_overage_gb": str(t_proj_overage),
                    "status": status
                }
                send_soar_webhook_alert(
                    webhook_url=webhook_url,
                    project_id=t_project_id,
                    event_type=event_type,
                    source_rule=rule_name,
                    message=msg,
                    description=desc,
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

def query_project_consumption(client, project_id, interval, actual_window_days):
    """Queries Cloud Monitoring for a single project's recent ingestion volume in GB."""
    name = f"projects/{project_id}"
    results = client.list_time_series(
        request={
            "name": name,
            "filter": 'metric.type = "chronicle.googleapis.com/ingestion/log/bytes_count"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    window_bytes = 0
    for series in results:
        labels = {}
        for k, v in series.resource.labels.items():
            labels[k] = v
        for k, v in series.metric.labels.items():
            labels[k] = v
        if labels.get("log_type") == "FORWARDER_HEARTBEAT":
            continue
        for point in series.points:
            window_bytes += point.value.int64_value
    window_gb = window_bytes / (1024 ** 3)
    avg_daily_gb = window_gb / actual_window_days
    return window_gb, avg_daily_gb

def calculate_forecast(
    project_id,
    active_term,
    contract_start,
    contract_end,
    gcs_bucket=None,
    ledger_blob_name="consumption_ledger.json",
    ledger_filepath=None,
    mock_consumption_data=None,
):
    """Calculates linear usage runway and overage forecast with multi-tenant proportion support."""
    client = None
    if not mock_consumption_data:
        client = monitoring_v3.MetricServiceClient()

    committed_gb = max(0.001, float(active_term.get("committed_gb", 1.0)))
    tenants_cfg = active_term.get("tenants", [])
    is_multi_tenant = bool(tenants_cfg)

    now = datetime.now(timezone.utc)
    total_contract_days = max(1, (contract_end - contract_start).days)
    days_elapsed = max(0, (now - contract_start).days)
    days_remaining = max(0, (contract_end - now).days)

    window_days = min(30, max(1, days_elapsed))
    window_start_sec = max(int(contract_start.timestamp()), int(now.timestamp() - window_days * 86400))
    actual_window_seconds = max(1, int(now.timestamp()) - window_start_sec)
    actual_window_days = max(1.0 / 86400.0, actual_window_seconds / 86400.0)

    interval = None
    if client:
        interval = monitoring_v3.TimeInterval(
            {
                "end_time": {"seconds": int(now.timestamp()), "nanos": 0},
                "start_time": {"seconds": window_start_sec, "nanos": 0},
            }
        )

    # Load persistent ledger
    ledger_data = load_json_config(filepath=ledger_filepath, gcs_bucket=gcs_bucket, gcs_blob_name=ledger_blob_name)
    stored_term = ledger_data.get("active_term_start")
    is_same_term = (stored_term == active_term["start_date"])

    if is_multi_tenant:
        print(
            f"Aggregating multi-tenant consumption for {len(tenants_cfg)} tenants over {actual_window_days:.1f} days (Total Quota: {committed_gb} GB)...",
            file=sys.stderr,
        )
        stored_tenants = ledger_data.get("tenants", {}) if is_same_term else {}
        tenant_calcs = {}

        for tenant in tenants_cfg:
            t_id = tenant.get("tenant_id", "default")
            t_name = tenant.get("name", t_id)
            t_project_id = tenant.get("byop_project_id") or project_id
            t_quota_share_pct = float(tenant.get("quota_share_pct", 100.0 / max(1, len(tenants_cfg))))

            # Ingestion Query
            if mock_consumption_data and t_project_id in mock_consumption_data:
                w_gb, avg_daily_gb = mock_consumption_data[t_project_id]
            elif client:
                w_gb, avg_daily_gb = query_project_consumption(client, t_project_id, interval, actual_window_days)
            else:
                w_gb, avg_daily_gb = 0.0, 0.0

            # Ledger accumulation per tenant
            t_stored = stored_tenants.get(t_id, {})
            if "accumulated_gb" in t_stored:
                stored_gb = float(t_stored["accumulated_gb"])
                last_updated_str = t_stored.get("last_updated")
                if last_updated_str:
                    try:
                        last_dt = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
                        delta_days = max(0.0, (now - last_dt).total_seconds() / 86400.0)
                        cumulative_gb = stored_gb + (avg_daily_gb * delta_days)
                    except Exception:
                        cumulative_gb = stored_gb
                else:
                    cumulative_gb = stored_gb
            else:
                prior_gb = float(tenant.get("prior_consumed_gb", 0.0))
                if prior_gb > 0:
                    cumulative_gb = prior_gb + (avg_daily_gb * days_elapsed)
                else:
                    cumulative_gb = avg_daily_gb * days_elapsed

            tenant_calcs[t_id] = {
                "name": t_name,
                "byop_project_id": t_project_id,
                "quota_share_pct": t_quota_share_pct,
                "cumulative_gb": cumulative_gb,
                "avg_daily_rate_gb": avg_daily_gb,
            }

        # Calculate totals
        total_cumulative_gb = sum(t["cumulative_gb"] for t in tenant_calcs.values())
        total_daily_rate_gb = sum(t["avg_daily_rate_gb"] for t in tenant_calcs.values())
        projected_total_gb = total_cumulative_gb + (total_daily_rate_gb * days_remaining)
        projected_overage_gb = max(0.0, projected_total_gb - committed_gb)

        # Calculate per-tenant proportions and statuses
        tenants_report = {}
        for t_id, t_data in tenant_calcs.items():
            t_alloc_quota_gb = committed_gb * (t_data["quota_share_pct"] / 100.0)
            actual_share_pct = (t_data["cumulative_gb"] / total_cumulative_gb * 100.0) if total_cumulative_gb > 0 else 0.0
            share_dev_pct = actual_share_pct - t_data["quota_share_pct"]
            t_projected_total = t_data["cumulative_gb"] + (t_data["avg_daily_rate_gb"] * days_remaining)
            t_projected_overage = max(0.0, t_projected_total - t_alloc_quota_gb)

            # Determine operational status
            if t_projected_overage > 0 and share_dev_pct >= 10.0:
                status = "CRITICAL_OVERAGE_AND_SKEW"
            elif t_projected_overage > 0:
                status = "PROJECTED_OVERAGE"
            elif share_dev_pct >= 10.0:
                status = "OVER_PROPORTION_WARNING"
            elif share_dev_pct <= -10.0:
                status = "UNDER_PROPORTION"
            else:
                status = "BALANCED"

            tenants_report[t_id] = {
                "name": t_data["name"],
                "byop_project_id": t_data["byop_project_id"],
                "quota_share_pct": round(t_data["quota_share_pct"], 2),
                "allocated_quota_gb": round(t_alloc_quota_gb, 2),
                "cumulative_ingestion_gb": round(t_data["cumulative_gb"], 2),
                "actual_share_pct": round(actual_share_pct, 2),
                "share_deviation_pct": round(share_dev_pct, 2),
                "avg_daily_rate_gb": round(t_data["avg_daily_rate_gb"], 2),
                "projected_total_volume_gb": round(t_projected_total, 2),
                "projected_overage_gb": round(t_projected_overage, 2),
                "status": status,
            }

        # Update persistent ledger
        updated_ledger = {
            "active_term_start": active_term["start_date"],
            "active_term_end": active_term["end_date"],
            "last_updated": now.isoformat(),
            "accumulated_gb": round(total_cumulative_gb, 4),
            "recent_daily_rate_gb": round(total_daily_rate_gb, 4),
            "tenants": {
                t_id: {
                    "accumulated_gb": round(t_data["cumulative_gb"], 4),
                    "recent_daily_rate_gb": round(t_data["avg_daily_rate_gb"], 4),
                    "last_updated": now.isoformat(),
                }
                for t_id, t_data in tenant_calcs.items()
            },
        }

    else:
        # Single-tenant backward-compatible mode
        print(f"Aggregating single-tenant consumption over {actual_window_days:.1f} days for active term (Quota: {committed_gb} GB)...", file=sys.stderr)
        if mock_consumption_data and project_id in mock_consumption_data:
            w_gb, avg_daily_gb = mock_consumption_data[project_id]
        elif client:
            w_gb, avg_daily_gb = query_project_consumption(client, project_id, interval, actual_window_days)
        else:
            w_gb, avg_daily_gb = 0.0, 0.0

        if is_same_term and "accumulated_gb" in ledger_data:
            stored_gb = float(ledger_data["accumulated_gb"])
            last_updated_str = ledger_data.get("last_updated")
            if last_updated_str:
                try:
                    last_dt = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
                    delta_days = max(0.0, (now - last_dt).total_seconds() / 86400.0)
                    total_cumulative_gb = stored_gb + (avg_daily_gb * delta_days)
                except Exception:
                    total_cumulative_gb = stored_gb
            else:
                total_cumulative_gb = stored_gb
        else:
            prior_gb = float(active_term.get("prior_consumed_gb", 0.0))
            if prior_gb > 0:
                total_cumulative_gb = prior_gb + (avg_daily_gb * days_elapsed)
            else:
                total_cumulative_gb = avg_daily_gb * days_elapsed

        total_daily_rate_gb = avg_daily_gb
        projected_total_gb = total_cumulative_gb + (total_daily_rate_gb * days_remaining)
        projected_overage_gb = max(0.0, projected_total_gb - committed_gb)
        tenants_report = {}

        updated_ledger = {
            "active_term_start": active_term["start_date"],
            "active_term_end": active_term["end_date"],
            "last_updated": now.isoformat(),
            "accumulated_gb": round(total_cumulative_gb, 4),
            "recent_daily_rate_gb": round(total_daily_rate_gb, 4),
        }

    # Save ledger
    if gcs_bucket or ledger_filepath:
        target_path = ledger_filepath or "consumption_ledger.json"
        write_json_output(updated_ledger, target_path, gcs_bucket, ledger_blob_name)

    pct_time_elapsed = min(100.0, (days_elapsed / total_contract_days) * 100)
    pct_commitment_consumed = (total_cumulative_gb / committed_gb) * 100
    ideal_cumulative_gb = (committed_gb / total_contract_days) * days_elapsed

    report = {
        "calculated_at": now.isoformat(),
        "active_term_start": active_term["start_date"],
        "active_term_end": active_term["end_date"],
        "committed_volume_gb": committed_gb,
        "cumulative_ingestion_gb": round(total_cumulative_gb, 2),
        "ideal_cumulative_gb": round(ideal_cumulative_gb, 2),
        "avg_daily_rate_gb": round(total_daily_rate_gb, 2),
        "projected_total_volume_gb": round(projected_total_gb, 2),
        "projected_overage_gb": round(projected_overage_gb, 2),
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "pct_time_elapsed": round(pct_time_elapsed, 2),
        "pct_commitment_consumed": round(pct_commitment_consumed, 2),
        "is_multi_tenant": is_multi_tenant,
        "tenants": tenants_report,
    }

    return report

def write_forecast_report(report, filepath, gcs_bucket=None, gcs_blob_name=None):
    """Writes forecast output to local file or GCS bucket."""
    write_json_output(report, filepath, gcs_bucket, gcs_blob_name)

def print_dry_run_report(report, term_index, total_terms, output_format="markdown"):
    """Prints the dry run report in markdown or HTML with multi-tenant proportion support."""
    is_multi_tenant = report.get("is_multi_tenant", False)
    tenants = report.get("tenants", {})

    if output_format == "html":
        print("<h2>Google SecOps Consumption Forecast Engine Report</h2>")
        print(f"<p><b>Active Contract Term:</b> Year {term_index} of {total_terms}</p>")
        print("<table border='1' cellpadding='5' style='border-collapse: collapse;'>")
        print(f"  <tr><td><b>Calculated At</b></td><td>{report['calculated_at']}</td></tr>")
        print(f"  <tr><td><b>Active Term Range</b></td><td>{report['active_term_start']} to {report['active_term_end']}</td></tr>")
        print(f"  <tr><td><b>Days Elapsed / Remaining</b></td><td>{report['days_elapsed']} days / {report['days_remaining']} days</td></tr>")
        print(f"  <tr><td><b>Committed License</b></td><td>{report['committed_volume_gb']} GB</td></tr>")
        print(f"  <tr><td><b>Cumulative Ingested</b></td><td>{report['cumulative_ingestion_gb']} GB ({report['pct_commitment_consumed']}% consumed)</td></tr>")
        print(f"  <tr><td><b>Ideal Volume Trajectory</b></td><td>{report['ideal_cumulative_gb']} GB ({report['pct_time_elapsed']}% time elapsed)</td></tr>")
        print(f"  <tr><td><b>Projected Volume at Term End</b></td><td>{report['projected_total_volume_gb']} GB</td></tr>")
        print(f"  <tr><td><b>Projected Overage Volume</b></td><td><font color='red'><b>{report['projected_overage_gb']} GB</b></font></td></tr>")
        print("</table>")

        if is_multi_tenant:
            print("<h3>Multi-Tenant Ingestion & Quota Allocation Breakdown</h3>")
            print("<table border='1' cellpadding='5' style='border-collapse: collapse;'>")
            print("  <tr><th>Tenant</th><th>Project ID</th><th>Contracted Share</th><th>Actual Share</th><th>Deviation</th><th>Allocated Quota</th><th>Consumed</th><th>Daily Rate</th><th>Projected Total</th><th>Projected Overage</th><th>Status</th></tr>")
            for t_id, t_info in tenants.items():
                dev_color = "red" if t_info["share_deviation_pct"] >= 10.0 else "green"
                overage_color = "red" if t_info["projected_overage_gb"] > 0 else "black"
                print(
                    f"  <tr>"
                    f"<td><b>{t_info['name']}</b> ({t_id})</td>"
                    f"<td>{t_info['byop_project_id']}</td>"
                    f"<td>{t_info['quota_share_pct']}%</td>"
                    f"<td>{t_info['actual_share_pct']}%</td>"
                    f"<td><font color='{dev_color}'>{t_info['share_deviation_pct']:+}%</font></td>"
                    f"<td>{t_info['allocated_quota_gb']} GB</td>"
                    f"<td>{t_info['cumulative_ingestion_gb']} GB</td>"
                    f"<td>{t_info['avg_daily_rate_gb']} GB/d</td>"
                    f"<td>{t_info['projected_total_volume_gb']} GB</td>"
                    f"<td><font color='{overage_color}'><b>{t_info['projected_overage_gb']} GB</b></font></td>"
                    f"<td><b>{t_info['status']}</b></td>"
                    f"</tr>"
                )
            print("</table>")
    else:
        # Markdown
        print("# Google SecOps Consumption Forecast Engine Report\n")
        print(f"**Active Contract Term:** Year {term_index} of {total_terms}\n")
        print("### Master Billing Contract Rollup\n")
        print("| Parameter | Value |")
        print("| :--- | :--- |")
        print(f"| **Calculated At** | {report['calculated_at']} |")
        print(f"| **Active Term Range** | {report['active_term_start']} to {report['active_term_end']} ({report['days_elapsed']} days elapsed, {report['days_remaining']} remaining) |")
        print(f"| **Master License Commitment** | {report['committed_volume_gb']} GB |")
        print(f"| **Total Ingested to Date** | {report['cumulative_ingestion_gb']} GB ({report['pct_commitment_consumed']}% of active quota) |")
        print(f"| **Ideal Target Volume** | {report['ideal_cumulative_gb']} GB ({report['pct_time_elapsed']}% of term) |")
        print(f"| **Projected Volume (Term End)** | {report['projected_total_volume_gb']} GB |")
        print(f"| **Estimated Global Overage** | **{report['projected_overage_gb']} GB** |\n")

        if is_multi_tenant:
            print("### Multi-Tenant Quota Allocation & Consumption Breakdown\n")
            print("| Tenant Name | BYOP Project ID | Quota Share % | Actual Share % | Deviation % | Allocated Quota | Consumed | Daily Rate | Projected Volume | Projected Overage | Status |")
            print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for t_id, t_info in tenants.items():
                dev_sign = "+" if t_info["share_deviation_pct"] > 0 else ""
                print(
                    f"| **{t_info['name']}** | `{t_info['byop_project_id']}` | {t_info['quota_share_pct']}% | "
                    f"{t_info['actual_share_pct']}% | {dev_sign}{t_info['share_deviation_pct']}% | "
                    f"{t_info['allocated_quota_gb']} GB | {t_info['cumulative_ingestion_gb']} GB | "
                    f"{t_info['avg_daily_rate_gb']} GB/d | {t_info['projected_total_volume_gb']} GB | "
                    f"**{t_info['projected_overage_gb']} GB** | `{t_info['status']}` |"
                )

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

        report = calculate_forecast(
            project_id,
            active_term,
            start_dt,
            end_dt,
            gcs_bucket=gcs_bucket,
            ledger_blob_name="consumption_ledger.json",
        )
        write_forecast_report(report, "", gcs_bucket, gcs_blob_name)
        if webhook_url:
            send_webhook_alert(report, webhook_url, project_id)
        return ("Ingestion forecast completed and report written to GCS.", 200)
    except Exception as e:
        return (f"Execution failed: {str(e)}", 500)

def main():
    parser = argparse.ArgumentParser(description="Google SecOps Consumption Forecast Engine (Multi-Tenant Support).")
    parser.add_argument("project_id", nargs="?", help="The primary GCP Project ID.")
    parser.add_argument("--terms-file", help="Path to local JSON file containing array of contract term configurations.")
    parser.add_argument("--webhook-url", help="Optional Webhook URL to dispatch overage alerts.")
    parser.add_argument("--dry-run", action="store_true", help="Print dry run reports directly to terminal.")
    parser.add_argument("--format", choices=["markdown", "html"], default="markdown", help="Format for dry-run reports.")
    parser.add_argument("--ledger-file", default="consumption_ledger.json", help="Path to local ledger file for persisting cumulative consumption.")

    args = parser.parse_args()

    project_id = args.project_id or os.environ.get("BYOP_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID")
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

    report = calculate_forecast(
        project_id,
        active_term,
        start_dt,
        end_dt,
        gcs_bucket=gcs_bucket,
        ledger_blob_name="consumption_ledger.json",
        ledger_filepath=args.ledger_file if not gcs_bucket else None,
    )

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
