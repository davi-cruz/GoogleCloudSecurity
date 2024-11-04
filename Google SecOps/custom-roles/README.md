# Google SecOps custom roles

As described in [Create and manage custom roles | IAM Documentation | Google Cloud](https://cloud.google.com/iam/docs/creating-custom-roles#gcloud) here's a list of custom roles that you can use in your SecOps deployment, ensuring the least privilege principle:
- `roles/secopsingestion.collector`: GCP role containing only the `chronicle.logs.import` permission, useful for ingestion scripts or the use with bindplane agent configuration

## Deployment

- Make a copy of the desired role configuration
- Login to your `gcloud` cli and define the context to Google SecOps project

```bash
gcloud auth login
gcloud config set project <project-id>
```

- Create the custom role

```bash
gcloud iam roles create secopsingestion.collector --file=secopsingestion.collector.yaml
```