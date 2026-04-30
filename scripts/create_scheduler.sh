#!/usr/bin/env bash
# Run once after the worker Cloud Run service is deployed.
# Prerequisites:
#   1. Grant the worker SA invoke rights on the worker service:
#      gcloud run services add-iam-policy-binding news-bot-worker \
#        --region=asia-south1 --project=$PROJECT_ID \
#        --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
#        --role="roles/run.invoker"
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID environment variable must be set}"
REGION="asia-south1"
SCHEDULER_SA="news-bot-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com"

WORKER_URL=$(gcloud run services describe news-bot-worker \
  --region "$REGION" --project "$PROJECT_ID" \
  --format "value(status.url)")

gcloud scheduler jobs create http news-bot-hourly-run \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule="0 * * * *" \
  --uri="${WORKER_URL}/run" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="${WORKER_URL}" \
  --time-zone="UTC"

echo "Scheduler job created. Worker will be called every hour at :00 UTC."
echo "Force a test run with:"
echo "  gcloud scheduler jobs run news-bot-hourly-run --location=${REGION} --project=${PROJECT_ID}"
