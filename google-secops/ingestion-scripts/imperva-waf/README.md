# Imperva WAF

> ⚠️ **Deprecation Notice / Alternative Available:**  
> This custom ingestion mechanism is **no longer required**. Imperva now natively supports direct **HTTP Push** for access logs to any HTTP endpoint. Refer to the [Imperva HTTP Push Connector Release Notes (Beta)](https://docs-cybersec.thalesgroup.com/bundle/cloud-application-security/page/release-notes/2026-08-16.htm#SIEMSendlogsdirectlytoanyHTTPendpointwiththenewgenericHTTPpushconnectorBeta) to configure direct log streaming into Google SecOps via an HTTP webhook/infeed instead of polling and downloading logs.

---

## Overview

This repository provides an automated pipeline that downloads Imperva Cloud WAF logs and ingests them into Google SecOps directly.

It follows the legacy guidance available at [Cloud WAF Log Integration | Imperva](https://docs.imperva.com/bundle/cloud-application-security/page/settings/log-integration.htm).

## Platform-Specific Environment Variables

| Variable | Description | Required | Default | Secret |
|---|---|---|---|---|
| `IMPERVA_WAF_API_ID` | Imperva WAF API ID. | Yes | - | No |
| `IMPERVA_WAF_API_SECRET` | Imperva WAF API Secret. | Yes | - | Yes |
| `IMPERVA_WAF_TARGET_SERVER` | URL for the Log Server. | Yes | - | No |
| `IMPERVA_WAF_PRIVATE_KEY` | Private Key to decrypt files, if log encryption is enabled. | Yes | - | Yes |
| `GCS_BUCKET_NAME` | Name of Google Cloud Storage bucket where the pointer file is stored. | Yes | - | No |
| `STATE_FILE_PATH` | Path in the bucket, including the file name, where the pointer is stored (e.g., `imperva-waf/marker`). | Yes | - | No |
