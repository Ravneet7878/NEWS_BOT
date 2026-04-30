# News Bot

Invite-only multi-user AI news digest bot built with Google ADK, FastAPI, and GCP.

---

## Architecture Overview

The project is split into two independent Cloud Run services to prevent webhook timeouts and allow independent scaling.

```
Telegram ──► api (Cloud Run, public)
                └─► Firestore (user data, invite codes, feedback)

Cloud Scheduler ──► worker (Cloud Run, private)
                        └─► ADK Pipeline (SequentialAgent)
                                ├─► news_fetcher  (google_search + Firestore prefs)
                                ├─► news_curator  (dedup + scoring)
                                └─► news_summariser (prose summaries)
                        └─► Telegram Bot API (deliver digest)
                        └─► Firestore (update last_digest_sent, weights)
```

**Why two services?**
The `api` service handles real-time Telegram webhook events and must respond within 30 seconds. The `worker` service runs the heavy ADK pipeline (30–120 seconds per user). Separating them prevents webhook timeouts and allows each to scale independently.

**Data flow:**
1. `api` receives `/start <code>` → validates invite → creates user in Firestore
2. Cloud Scheduler hits `POST /run` on `worker` every hour (UTC)
3. Worker queries Firestore for users with `delivery_hour_utc == current_hour`
4. ADK SequentialAgent: fetcher → curator → summariser (state flows via session state keys)
5. Worker reads `final_digest` from session state and delivers via Telegram Bot API
6. User taps 👍/👎 → `api` handles callback → updates topic weights in Firestore

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
# API service: Firestore read/write
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

# 8. Grant Cloud Scheduler permission to invoke the worker Cloud Run service
gcloud run services add-iam-policy-binding news-bot-worker \
  --region=asia-south1 \
  --project=$PROJECT_ID \
  --member="serviceAccount:news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 9. Create Cloud Scheduler job (calls worker every hour via OIDC)
# Authentication is handled by Cloud Run IAM — no shared secret needed.
gcloud scheduler jobs create http news-bot-hourly-run \
  --location=asia-south1 \
  --schedule="0 * * * *" \
  --uri="https://YOUR_WORKER_URL/run" \
  --http-method=POST \
  --oidc-service-account-email="news-bot-worker-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --oidc-token-audience="https://YOUR_WORKER_URL" \
  --time-zone="UTC"

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
# and for local dev set TELEGRAM_BOT_TOKEN directly

# Run the API service locally
uvicorn api.main:app --reload --port 8080

# Run the worker service locally
uvicorn worker.main:app --reload --port 8081

# Visual ADK agent debugger (requires ADK installed)
adk web
```

For local webhook testing, expose port 8080 with [ngrok](https://ngrok.com/):
```bash
ngrok http 8080
```
Then register the webhook (see below) with the ngrok URL.

---

## Registering the Telegram Webhook

Run after every `api` service deployment:

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

| Variable              | Required | Description |
|-----------------------|----------|-------------|
| `GCP_PROJECT_ID`      | Yes      | GCP project ID |
| `VERTEX_AI_LOCATION`  | No       | Vertex AI region (default: `asia-south1`) |
| `ADMIN_TELEGRAM_ID`   | Yes      | Your Telegram user ID (integer as string) |
| `WEBHOOK_SECRET_TOKEN`| Yes      | Random secret set in both settings and `setWebhook` call |
| `TELEGRAM_BOT_TOKEN`  | Dev only | Loaded from Secret Manager on Cloud Run |
