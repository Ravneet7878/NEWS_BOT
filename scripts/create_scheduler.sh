#!/usr/bin/env bash
# Run once after the worker Cloud Run service is deployed.
# Creates two hourly jobs:
#   - /prepare  at :55 (pre-builds digests before the hour)
#   - /deliver  at :00 (delivers pre-built digests; retries if /prepare is still running)
#
# Prerequisites:
#   Grant the worker SA invoke rights on the worker service:
#     gcloud run services add-iam-policy-binding news-bot-worker \
#       --region=asia-south1 --project=$PROJECT_ID \
#       --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
#       --role="roles/run.invoker"
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID environment variable must be set}"
REGION="asia-south1"
SCHEDULER_SA="news-bot-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com"

WORKER_URL=$(gcloud run services describe news-bot-worker \
  --region "$REGION" --project "$PROJECT_ID" \
  --format "value(status.url)")

gcloud scheduler jobs create http news-bot-hourly-prepare \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule="55 * * * *" \
  --uri="${WORKER_URL}/prepare" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="${WORKER_URL}" \
  --time-zone="UTC"

gcloud scheduler jobs create http news-bot-hourly-deliver \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule="0 * * * *" \
  --uri="${WORKER_URL}/deliver" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="${WORKER_URL}" \
  --time-zone="UTC" \
  --attempt-deadline=180s \
  --max-retry-attempts=5 \
  --min-backoff=300s \
  --max-backoff=600s \
  --max-doublings=1

echo "Scheduler jobs created:"
echo "  news-bot-hourly-prepare  → ${WORKER_URL}/prepare  (55 * * * * UTC)"
echo "  news-bot-hourly-deliver  → ${WORKER_URL}/deliver  (0 * * * * UTC, retries within the hour)"
echo ""
echo "Force a test run with:"
echo "  gcloud scheduler jobs run news-bot-hourly-prepare --location=${REGION} --project=${PROJECT_ID}"
echo "  gcloud scheduler jobs run news-bot-hourly-deliver --location=${REGION} --project=${PROJECT_ID}"
