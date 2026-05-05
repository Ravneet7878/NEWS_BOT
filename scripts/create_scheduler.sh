#!/usr/bin/env bash
# Run after the worker Cloud Run service is deployed.
# Creates or updates two per-minute jobs:
#   - /prepare  every minute — forward-looking window scan (PREPARE_BUFFER_MINUTES=5).
#               Idempotent: skips users that already have a pending doc, so overlapping
#               invocations are safe.
#   - /deliver  every minute — exact-minute match. Only delivers users whose
#               delivery_minute_utc matches the current UTC minute, so most ticks are
#               zero-op fast-returns.
#
# Prerequisites:
#   Grant the Scheduler invoker SA invoke rights on the worker service:
#     gcloud run services add-iam-policy-binding news-bot-worker \
#       --region=asia-south1 --project=$PROJECT_ID \
#       --member="serviceAccount:scheduler-invoker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
#       --role="roles/run.invoker"
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID environment variable must be set}"
REGION="asia-south1"
SCHEDULER_SA="scheduler-invoker-sa@${PROJECT_ID}.iam.gserviceaccount.com"
PREPARE_SCHEDULE="* * * * *"
DELIVER_SCHEDULE="* * * * *"

WORKER_URL=$(gcloud run services describe news-bot-worker \
  --region "$REGION" --project "$PROJECT_ID" \
  --format "value(status.url)")

upsert_http_job() {
  local job_name="$1"
  shift

  if gcloud scheduler jobs describe "$job_name" \
    --project="$PROJECT_ID" \
    --location="$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$job_name" \
      --project="$PROJECT_ID" \
      --location="$REGION" \
      "$@"
  else
    gcloud scheduler jobs create http "$job_name" \
      --project="$PROJECT_ID" \
      --location="$REGION" \
      "$@"
  fi
}

upsert_http_job news-bot-hourly-prepare \
  --schedule="$PREPARE_SCHEDULE" \
  --uri="${WORKER_URL}/prepare" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="${WORKER_URL}" \
  --time-zone="UTC" \
  --attempt-deadline=90s

upsert_http_job news-bot-hourly-deliver \
  --schedule="$DELIVER_SCHEDULE" \
  --uri="${WORKER_URL}/deliver" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="${WORKER_URL}" \
  --time-zone="UTC" \
  --attempt-deadline=180s \
  --max-retry-attempts=0

echo "Scheduler jobs created or updated:"
echo "  news-bot-hourly-prepare  → ${WORKER_URL}/prepare  (${PREPARE_SCHEDULE} UTC, per-minute)"
echo "  news-bot-hourly-deliver  → ${WORKER_URL}/deliver  (${DELIVER_SCHEDULE} UTC, per-minute, no retries — next tick handles recovery)"
echo ""
echo "Force a test run with:"
echo "  gcloud scheduler jobs run news-bot-hourly-prepare --location=${REGION} --project=${PROJECT_ID}"
echo "  gcloud scheduler jobs run news-bot-hourly-deliver --location=${REGION} --project=${PROJECT_ID}"
