# SPU MS Public Notices Scraper — Setup

## What This Does
Automatically scrapes foreclosure notices from mspublicnotices.org every Mon/Wed/Fri at 6AM CT.
Parses borrower names, DOT dates, attorneys, auction info.
Pushes new entries to your Google Sheet. Skips duplicates. Emails you a summary.

**Cost: $0.** Runs on GitHub Actions free tier (2,000 min/month for private repos).

---

## Setup Steps (15 minutes, one-time)

### 1. Create a GitHub Repository
- Go to github.com > New Repository
- Name it `ms-notices-scraper` (private)
- Push all files from this folder to the repo

### 2. Google Cloud Service Account
This is how the script writes to your Google Sheet without needing your login.

1. Go to https://console.cloud.google.com
2. Create a new project (or use existing) — name it "SPU Scraper"
3. Enable these APIs (search for them in the API Library):
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **IAM & Admin > Service Accounts**
5. Click **Create Service Account**
   - Name: `spu-scraper`
   - Click Create > Done
6. Click into the new service account
7. Go to **Keys** tab > **Add Key > Create New Key > JSON**
8. Download the JSON file — you'll need its contents in Step 3

### 3. Share the Google Sheet
Open your SPU_MS_Public_Notices_Monitor sheet.
Click **Share** and add the service account email as an **Editor**.
The email looks like: `spu-scraper@your-project.iam.gserviceaccount.com`
(find it in the JSON file under `client_email`)

### 4. Add GitHub Secrets
In your GitHub repo: **Settings > Secrets and variables > Actions > New repository secret**

Add these secrets:

| Secret Name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the ENTIRE contents of the JSON key file |
| `NOTIFY_EMAIL` | `paul@southernpropertiesunited.com` |
| `SMTP_USER` | Your sending email (e.g., Gmail address) |
| `SMTP_PASSWORD` | App password for that email (NOT your regular password) |
| `SMTP_HOST` | `smtp.gmail.com` (for Gmail) |
| `SMTP_PORT` | `587` |

**Gmail App Password:** Google Account > Security > 2-Step Verification > App passwords > Generate.

### 5. Test It
- Go to your repo on GitHub
- Click **Actions** tab
- Click **MS Foreclosure Scrape** workflow
- Click **Run workflow** button
- Watch the logs to confirm it works

---

## File Structure
```
ms-notices-scraper/
  scraper.py              # Main script
  requirements.txt        # Python dependencies
  .github/
    workflows/
      scrape.yml          # GitHub Actions schedule
  SETUP.md                # This file
```

## Schedule
- Runs Mon/Wed/Fri at 6:00 AM Central
- Cron is set to 11:00 UTC (CDT). Change to 12:00 UTC in winter (CST).
- To change schedule, edit the `cron` line in `.github/workflows/scrape.yml`

## Manual Run
GitHub Actions UI > Actions tab > MS Foreclosure Scrape > Run workflow

## Troubleshooting
- **"No Google credentials found"** — GOOGLE_SERVICE_ACCOUNT_JSON secret not set
- **"Tab not found"** — County tab name in sheet doesn't match COUNTIES list in scraper.py
- **No new notices** — All current notices are already in your sheet (working as intended)
- **Email not sending** — Check SMTP credentials, make sure App Password is correct
