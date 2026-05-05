# News Bot

Invite-only multi-user AI news digest bot built with Google ADK, FastAPI, and GCP.

---

## Architecture Overview

The project is split into two independent Cloud Run services to prevent webhook timeouts and allow independent scaling.

```
Telegram ──► api (Cloud Run, public)
                └─► Firestore (user data, invite codes, feedback)

Cloud Scheduler ──► worker (Cloud Run, private)
                        ├─► /prepare  (LLM pipeline: curator + summariser, runs at :55)
                        └─► /deliver  (Telegram delivery from cache, runs at :00)
                        └─► Firestore (pending_digests, user_digest_history, last_digest_sent)
```

**Why two services?**
The `api` service handles real-time Telegram webhook events and must respond within 30 seconds. The `worker` service runs the heavy ADK pipeline (30–120 seconds per user). Separating them prevents webhook timeouts and allows each to scale independently.

**Worker scheduling (two-job flow):**
1. At `:55` — Cloud Scheduler calls `POST /prepare`: runs the ADK pipeline (curator + summariser) for all users scheduled for the next hour and caches results in `pending_digests`.
2. At `:00` — Cloud Scheduler calls `POST /deliver`: reads pre-built digests from `pending_digests` and sends them via Telegram. If `/prepare` is still running or a pending digest is missing, `/deliver` returns a retryable `503` so Scheduler retries within the hour. Use `POST /run` only for manual live recovery.

**Data flow:**
1. `api` receives `/start <code>` → validates invite (transactionally) → creates user in Firestore
2. Cloud Scheduler hits `POST /prepare` on worker at :55 UTC
3. Cloud Scheduler hits `POST /deliver` on worker at :00 UTC
4. Worker queries Firestore for users with `delivery_hour_utc == current_hour`
5. `fetch_articles_for_user` (Python) populates raw_articles → ADK SequentialAgent: curator → summariser
6. Worker reads `final_digest` from session state and delivers via Telegram Bot API
7. User taps 👍/👎 → `api` handles callback → updates topic weights in Firestore

---

## GCP Setup

Run all commands in order. Replace `YOUR_PROJECT_ID` throughout.

```bash
# 1. Set project
gcloud config set project YOUR_PROJECT_ID
export PROJECT_ID=YOUR_PROJECT_ID

# 2. Enable required APIs
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com

# 3. Create Artifact Registry repository
gcloud artifacts repositories create news-bot-repo \
  --repository-format=docker \
  --location=asia-south1

# 4. Create Firestore database (Native mode)
gcloud firestore databases create --region=asia-south1

# 5. Create service accounts
gcloud iam service-accounts create news-bot-api-sa \
  --display-name="News Bot API Service Account"

gcloud iam service-accounts create news-bot-worker-sa \
  --display-name="News Bot Worker Service Account"

# 6. Grant IAM roles
# API service: Firestore read/write + Secret Manager
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:news-bot-api-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:news-bot-api-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Worker service: Firestore read/write + Vertex AI + Secret Manager
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/datastore.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# 7. Create secrets in Secret Manager
echo -n "YOUR_TELEGRAM_BOT_TOKEN" | \
  gcloud secrets create TELEGRAM_BOT_TOKEN --data-file=-

echo -n "YOUR_WEBHOOK_SECRET_TOKEN" | \
  gcloud secrets create webhook-secret-token --data-file=-

echo -n "YOUR_ADMIN_TELEGRAM_ID" | \
  gcloud secrets create admin-telegram-id --data-file=-

echo -n "YOUR_NEWSDATA_API_KEY" | \
  gcloud secrets create NEWSDATA_API_KEY --data-file=-

# Generate a random HMAC salt for log pseudonymization (required for APP_ENV=prod)
openssl rand -hex 32 | \
  gcloud secrets create log-pseudonym-salt --data-file=-

# 8. Enable Firestore TTL policies on expiring collections
gcloud firestore fields ttls update expires_at \
  --collection-group=news_cache --project=$PROJECT_ID --enable-ttl
gcloud firestore fields ttls update expires_at \
  --collection-group=user_digest_history --project=$PROJECT_ID --enable-ttl
gcloud firestore fields ttls update expires_at \
  --collection-group=pending_digests --project=$PROJECT_ID --enable-ttl
gcloud firestore fields ttls update expires_at \
  --collection-group=curated_topics_v1 --project=$PROJECT_ID --enable-ttl
gcloud firestore fields ttls update expires_at \
  --collection-group=article_summaries_v1 --project=$PROJECT_ID --enable-ttl

# 9. Grant Cloud Scheduler permission to invoke the worker Cloud Run service
# (run after the worker service is first deployed)
gcloud run services add-iam-policy-binding news-bot-worker \
  --region=asia-south1 \
  --project=$PROJECT_ID \
  --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 10. Create Cloud Build trigger (triggers on push to main)
gcloud builds triggers create github \
  --repo-name=YOUR_GITHUB_REPO \
  --repo-owner=YOUR_GITHUB_USER \
  --branch-pattern="^main$" \
  --build-config=cloudbuild.yaml
```

---

## Firestore Composite Index

Required for the hourly user query. Create it once:

```bash
gcloud firestore indexes composite create \
  --collection-group=users \
  --field-config field-path=is_active,order=ascending \
  --field-config field-path=is_paused,order=ascending \
  --field-config field-path=delivery_hour_utc,order=ascending
```

---

## Deploying

```bash
gcloud builds submit --config=cloudbuild.yaml --project=$PROJECT_ID
```

---

## Post-Deploy Setup

Run these once after the first successful deploy.

```bash
# Register the Telegram webhook (uses Secret Manager to fetch credentials)
PROJECT_ID=$PROJECT_ID ./scripts/setup_webhook.sh

# Create Cloud Scheduler jobs (two-job flow: /prepare at :55, /deliver at :00)
PROJECT_ID=$PROJECT_ID ./scripts/create_scheduler.sh
```

The `news-bot-hourly-deliver` job is configured with retries bounded inside the
same UTC hour. Do not rely on Cloud Run request queuing between `/prepare` and
`/deliver`; missing pending digests are surfaced as retryable delivery attempts.

### Verify the deployment

```bash
# Health check
curl "$(gcloud run services describe news-bot-api \
  --region=asia-south1 --project=$PROJECT_ID --format='value(status.url)')/health"

# Force a Scheduler test run
gcloud scheduler jobs run news-bot-hourly-prepare --location=asia-south1 --project=$PROJECT_ID
gcloud scheduler jobs run news-bot-hourly-deliver --location=asia-south1 --project=$PROJECT_ID

# Confirm Cloud Run env config
gcloud run services describe news-bot-api --region=asia-south1 --project=$PROJECT_ID
gcloud run services describe news-bot-worker --region=asia-south1 --project=$PROJECT_ID
```

---

## Local Development

**Prerequisites:** Python 3.12, a GCP project with ADC configured (`gcloud auth application-default login`)

```bash
# Clone and set up
git clone https://github.com/YOUR_USER/news-bot
cd news-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in your .env
cp .env.example .env
# Edit .env: set GCP_PROJECT_ID, ADMIN_TELEGRAM_ID, WEBHOOK_SECRET_TOKEN,
# and for local dev set TELEGRAM_BOT_TOKEN and NEWSDATA_API_KEY directly

# Run the API service locally
uvicorn api.main:app --reload --port 8080

# Run the worker service locally
uvicorn worker.main:app --reload --port 8081

# Visual ADK agent debugger (requires ADK installed)
adk web
```

For local webhook testing, expose port 8080 with [ngrok](https://ngrok.com/) (local only — not used in production):
```bash
ngrok http 8080
# Then run setup_webhook.sh with the ngrok URL, or set the webhook manually
```

---

## Registering the Telegram Webhook

Use the provided script (reads credentials from Secret Manager automatically):

```bash
PROJECT_ID=$PROJECT_ID ./scripts/setup_webhook.sh
```

Or manually:

```bash
curl "https://api.telegram.org/bot{YOUR_BOT_TOKEN}/setWebhook" \
  -d "url=https://YOUR_API_URL/webhook" \
  -d "secret_token=YOUR_WEBHOOK_SECRET_TOKEN" \
  -d "allowed_updates=[\"message\",\"callback_query\"]"
```

The `api` service validates the `X-Telegram-Bot-Api-Secret-Token` header on every incoming request.

---

## Generating First Invite Codes

Once deployed, open Telegram and message your bot as the admin:

```
/gencode 5
```

The bot replies with 5 invite codes you can share. Each code can only be used once.

---

## Billing Alert Setup

Set a budget alert to avoid unexpected charges:

```bash
# Via the GCP Console: Billing > Budgets & alerts > Create budget
# Recommended: set a monthly alert at $20 with email notifications at 50%, 90%, 100%
```

---

## Environment Variables Reference

| Variable                    | Service      | Source          | Description |
|-----------------------------|--------------|-----------------|-------------|
| `GCP_PROJECT_ID`            | both         | env var         | GCP project ID |
| `VERTEX_AI_LOCATION`        | both         | env var         | Vertex AI region (default: `asia-south1`) |
| `APP_ENV`                   | both         | env var         | Set to `prod` in Cloud Run; `local` for dev |
| `GOOGLE_GENAI_USE_VERTEXAI` | worker       | env var         | Set to `1` to route ADK through Vertex AI |
| `GOOGLE_CLOUD_PROJECT`      | worker       | env var         | Project ID for ADK/Vertex AI client |
| `GOOGLE_CLOUD_LOCATION`     | worker       | env var         | Region for ADK/Vertex AI client |
| `ADMIN_TELEGRAM_ID`         | api          | Secret Manager  | Your Telegram user ID (integer as string) |
| `WEBHOOK_SECRET_TOKEN`      | api          | Secret Manager  | Random secret set in both settings and `setWebhook` |
| `TELEGRAM_BOT_TOKEN`        | both         | Secret Manager  | Telegram Bot API token |
| `NEWSDATA_API_KEY`          | worker       | Secret Manager  | NewsData.io API key |
| `LOG_PSEUDONYM_SALT`        | both         | Secret Manager  | HMAC key for log pseudonymisation; required when `APP_ENV=prod` |
