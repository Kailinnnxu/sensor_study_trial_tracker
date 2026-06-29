# Railway deployment guide

Deploy the **Tracker** folder as a Railway project with two services: a web dashboard and a daily cron job.

## Architecture on Railway

| Service | Purpose | Start command |
|---------|---------|---------------|
| **web** | Flask dashboard (always on) | `gunicorn` via `Procfile` |
| **cron** | Ingestion + reminders (daily) | `python scripts/run_daily.py` |

Both services share the same **volume** (`/data`) for the SQLite database and refreshed Gmail token.

---

## Step 1 — Push code to GitHub

Railway deploys from a git repo. Push the `Tracker` folder (or whole repo with root directory set to `Tracker`).

---

## Step 2 — Create Railway project

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repo
3. Set **Root Directory** to `Tracker` if the repo root is above it

---

## Step 3 — Web service (first deploy)

Railway auto-detects the `Procfile` and deploys the web app.

After deploy, note your public URL (e.g. `https://sensor-tracker.up.railway.app`).

---

## Step 4 — Add a persistent volume

1. Open the **web** service → **Volumes** → **Add Volume**
2. Mount path: `/data`

Attach the **same volume** to the cron service later.

Without a volume, the SQLite database and Gmail token are lost on every redeploy.

---

## Step 5 — Environment variables

Run locally (after OAuth setup) to generate base64 secrets:

```powershell
cd Tracker
python scripts/export_railway_secrets.py
```

In Railway → **Variables** (shared across services, or duplicated on each):

| Variable | Value |
|----------|-------|
| `GMAIL_CREDENTIALS_B64` | from export script |
| `GMAIL_TOKEN_B64` | from export script |
| `TRACKER_DATABASE_PATH` | `/data/tracker.db` |
| `GMAIL_TOKEN_PATH` | `/data/gmail_token.json` |
| `GMAIL_CREDENTIALS_PATH` | `/data/gmail_credentials.json` |
| `KAILIN_EMAIL` | `kailinxu@hsl.harvard.edu` |
| `SMTP_USER` | `klx5505@gmail.com` |
| `SMTP_PASSWORD` | Gmail app password |
| `FLASK_SECRET_KEY` | random string |
| `APP_URL` | your Railway web URL |

---

## Step 6 — Cron service (daily jobs)

### Create the second service

1. In the same Railway project → **+ New** → **GitHub Repo** → same repo
2. **Root Directory:** leave empty (repo root is already the Tracker code)
3. Rename this service something like `cron` so you can tell it apart from `web`

### Option A — Config file (easiest if you can't find Cron Schedule in the UI)

1. Click the **cron** service → **Settings**
2. Find **Railway Config File** (or **Config file path**)
3. Set it to: **`railway.cron.json`**
4. Redeploy

That file sets:
- Start command: `python scripts/run_daily.py`
- Cron schedule: `0 12 * * *` (12:00 UTC daily ≈ 7–8 AM Eastern)
- Restart policy: `NEVER` (required for cron jobs)

### Option B — Dashboard (if you see the field)

1. Click the **cron** service (not the web service)
2. **Settings** tab → scroll down (often below Deploy / Networking)
3. Look for **Cron Schedule** — enter `0 12 * * *`
4. **Deploy** section → **Start Command:** `python scripts/run_daily.py`
5. **Restart Policy:** `Never` or `ON_FAILURE` off — cron must exit when done

**Can't find Cron Schedule?** Common reasons:
- You're on the **web** service (gunicorn) — cron goes on the **second** service
- The field is far down on **Settings** — keep scrolling
- Use **Option A** (`railway.cron.json`) instead

### Finish cron setup

5. Attach the **same `/data` volume** to the cron service (`Ctrl+K` → search "volume")
6. Copy the same **Variables** from the web service

Cron services run the command on schedule, then exit. They do not stay running.

---

## Step 7 — Verify

1. Open `https://your-app.up.railway.app/health` → should return `{"status":"ok"}`
2. Open the dashboard at `/`
3. Trigger cron manually once: Railway → cron service → **Deployments** → **Redeploy** (or run locally with same env)
4. Check logs for ingestion/reminder output

---

## OAuth note

Gmail OAuth is done **once on your PC** (`python scripts/setup_gmail_oauth.py`). The resulting token is uploaded to Railway via `GMAIL_TOKEN_B64`. Google refreshes the token automatically; refreshed tokens are saved back to `/data/gmail_token.json` on the volume.

You do **not** run the browser OAuth flow on Railway.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Database resets on deploy | Add and mount `/data` volume |
| `Gmail credentials not found` | Set `GMAIL_CREDENTIALS_B64` |
| Token expired / auth errors | Re-run export script after local OAuth refresh |
| Cron never runs | Confirm cron schedule on cron service, not web service |
| Emails not sent | Check `SMTP_*` and `KAILIN_EMAIL` in cron service vars too |
