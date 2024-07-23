# Imperva WAF 

This downloads Imperva Cloud WAF logs and then ingest them into Google SecOps directly.

It folows the guidance available at [Cloud WAF log Integration | Imperva](https://docs.imperva.com/bundle/cloud-application-security/page/settings/log-integration.htm).

## Platform Specific Environment Variables

| Variable                  | Description                                                                                                                                                    | Required | Default | Secret |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------- | ------ |
| IMPERVA_WAF_API_ID        | Imperva WAF API ID.                                                                                                                                            | Yes      | -       | No     |
| IMPERVA_WAF_API_SECRET    | Imperva WAF API Secret.                                                                                                                                        | Yes      | -       | Yes    |
| IMPERVA_WAF_TARGET_SERVER | URL for the Log Server.                                                                                                                                        | Yes      | -       | No     |
| IMPERVA_WAF_PRIVATE_KEY   | Private Key for to decrypt files, if log encryption is enabled.                                                                                                | Yes      | -       | Yes    |
| GCS_BUCKET_NAME           | Name of Google Cloud Storage bucket where a pointer file will be stored.                                                                                       | Yes      | -       | No     |
| STATE_FILE_PATH           | Path in the bucket, including the file name, where the pointer will be stored. Eg `imperva-waf/marker`                                                         | Yes      | -       | No     |
