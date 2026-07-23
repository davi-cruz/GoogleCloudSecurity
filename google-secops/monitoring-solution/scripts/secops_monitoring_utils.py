#!/usr/bin/env python3
"""Google SecOps Monitoring Solution - Shared Utilities.

This module provides common functionality for Cloud Monitoring metrics querying,
SOAR webhook alert dispatching, duration formatting, and GCS output management.
"""

import json
import os
import sys
import time
import urllib.request
from google.cloud import storage

def format_human_duration(seconds):
    """Converts seconds into a human readable string like '6h 18m' or '5m' or '2d 4h'."""
    if not seconds or seconds < 60:
        return f"{seconds}s"
    
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    
    rem_hours = hours % 24
    rem_minutes = minutes % 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if rem_hours > 0:
        parts.append(f"{rem_hours}h")
    if rem_minutes > 0 or not parts:
        parts.append(f"{rem_minutes}m")
        
    return " ".join(parts)

def send_soar_webhook_alert(webhook_url, project_id, event_type, source_rule, message, description, severity="Warning", custom_fields=None):
    """Dispatches a SOAR-formatted webhook alert payload to the configured endpoint."""
    if not webhook_url:
        return False
        
    now_ms = int(time.time() * 1000)
    payload = {
        "StartTime": now_ms,
        "EndTime": now_ms,
        "product_type": "Google Cloud Monitoring",
        "event_type": event_type,
        "soar_alert_id": f"secops_alert_{project_id}_{int(time.time())}",
        "detection_time": now_ms,
        "source_rule": source_rule,
        "source_system_uri": f"https://console.cloud.google.com/monitoring?project={project_id}",
        "Message": message,
        "description": description,
        "Severity": severity,
        "CategoryOutcome": "open",
        "custom_fields": custom_fields or {}
    }
    
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            print(f"SOAR Webhook alert [{event_type}] successfully dispatched (HTTP {resp.status})", file=sys.stderr)
            return True
    except Exception as e:
        print(f"Failed to dispatch SOAR Webhook alert: {e}", file=sys.stderr)
        return False

def load_json_file(filepath):
    """Safely loads a JSON file from disk."""
    if not os.path.exists(filepath):
        print(f"Error: File '{filepath}' not found.", file=sys.stderr)
        return {}
    with open(filepath, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON file '{filepath}': {e}", file=sys.stderr)
            return {}

def write_json_output(data, filepath, gcs_bucket=None, gcs_blob_name=None):
    """Writes data as formatted JSON locally or uploads directly to a GCS bucket."""
    json_content = json.dumps(data, indent=2)
    
    if gcs_bucket and gcs_blob_name:
        print(f"Uploading output to GCS bucket '{gcs_bucket}' as '{gcs_blob_name}'...", file=sys.stderr)
        storage_client = storage.Client()
        bucket = storage_client.bucket(gcs_bucket)
        blob = bucket.blob(gcs_blob_name)
        blob.upload_from_string(json_content, content_type="application/json")
        print("GCS Upload successful.", file=sys.stderr)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w") as f:
            f.write(json_content)
        print(f"Successfully saved output locally to: {filepath}", file=sys.stderr)
