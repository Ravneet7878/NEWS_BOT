#!/usr/bin/env bash
# Run once after the first Cloud Run deploy to register the Telegram webhook.
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID environment variable must be set}"
REGION="asia-south1"
SERVICE="news-bot-api"

SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --region "$REGION" --project "$PROJECT_ID" \
  --format "value(status.url)")

TOKEN=$(gcloud secrets versions access latest \
  --secret="TELEGRAM_BOT_TOKEN" --project="$PROJECT_ID")

SECRET=$(gcloud secrets versions access latest \
  --secret="webhook-secret-token" --project="$PROJECT_ID")

curl -sS -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  -d "url=${SERVICE_URL}/webhook" \
  -d "secret_token=${SECRET}" \
  -d "allowed_updates=[\"message\",\"callback_query\"]" | jq .

echo "Webhook set to: ${SERVICE_URL}/webhook"
