#!/usr/bin/env python3
"""Google SecOps Consumption Forecast Engine.

Uses Cloud Monitoring Ingestion metrics to project contract consumption velocity
and estimate remaining contract runway without requiring BigQuery.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from google.cloud import monitoring_v3

def calculate_forecast(project_id, committed_gb, contract_start_str, contract_end_str):
    """Calculates linear usage runway and overage forecast."""
    client = monitoring_v3.MetricServiceClient()
    name = f"projects/{project_id}"
    
    contract_start = datetime.fromisoformat(contract_start_str.replace("Z", "+00:00"))
    contract_end = datetime.fromisoformat(contract_end_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    
    # Query cumulative ingested bytes from contract start until now
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(now.timestamp()), "nanos": 0},
            "start_time": {"seconds": int(contract_start.timestamp()), "nanos": 0},
        }
    )
    
    print(f"Aggregating cumulative consumption since contract start: {contract_start_str}...")
    
    results = client.list_time_series(
        request={
            "name": name,
            "filter": 'metric.type = "chronicle.googleapis.com/ingestion/log/bytes_count" AND metric.labels.log_type != "FORWARDER_HEARTBEAT"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    
    total_bytes = 0
    for series in results:
        for point in series.points:
            total_bytes += point.value.int64_value
            
    # Calculate days
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
        "contract_start": contract_start_str,
        "contract_end": contract_end_str,
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

def main():
    if len(sys.argv) < 5:
        print("Usage: python3 forecast_engine.py <GCP_PROJECT_ID> <COMMITTED_GB> <CONTRACT_START_ISO> <CONTRACT_END_ISO>")
        print("Example: python3 forecast_engine.py my-project 365000 2026-01-01T00:00:00Z 2026-12-31T23:59:59Z")
        sys.exit(1)
        
    project_id = sys.argv[1]
    committed_gb = float(sys.argv[2])
    contract_start = sys.argv[3]
    contract_end = sys.argv[4]
    
    report = calculate_forecast(project_id, committed_gb, contract_start, contract_end)
    
    print("\n--- CONSUMPTION FORECAST ENGINE REPORT ---")
    print(json.dumps(report, indent=2))
    
    # Alert conditions
    if report["pct_commitment_consumed"] > report["pct_time_elapsed"] * 1.15:
        print("\n⚠️ WARNING: consumption rate is running 15% hotter than contract time trajectory.")
        print(f"Ideal trajectory volume: {report['ideal_cumulative_gb']} GB vs actual: {report['cumulative_ingestion_gb']} GB")
        sys.exit(3) # exit warning status
        
    print("\n✅ Consumption is within contract parameters.")
    sys.exit(0)

if __name__ == "__main__":
    main()
