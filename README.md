# contentcreationautomation

## Run

```bash
python app.py
```

Open `http://localhost:5001`.

Install dependencies:

```bash
pip install -r requirements.txt
```

## Deployment Quick Start

Run this command on your server/hosting platform:

```bash
python app.py
```

Notes:

- The app binds to `PORT` automatically in cloud environments.
- Keep exactly one running instance to avoid duplicate scraping writes.
- For monitor-only deployments, you can run `python scraper.py` instead.

## Continuous Monitoring (Every 5 Minutes)

- The server now starts a background monitor loop automatically.
- Interval is controlled by `monitor_interval_seconds` in `config.json` (currently `300` = 5 minutes).
- It checks all enabled profiles and writes new rows to Excel and Google Sheets.

To disable auto-monitor for a process:

```bash
export ENABLE_BACKGROUND_MONITOR=false
python app.py
```

If you want monitor-only mode (without web UI):

```bash
python scraper.py
```

## Quick Recent Fetch (1h For All Accounts)

- In the **Time Range** card, click `1h (all)` (button shown before `24h`).
- This runs fetch for all listed enabled accounts in your config.
- The app creates a brand-new Excel file for that run only under `fetch_exports/`.
- The table preview and download buttons automatically switch to that run file after completion.
- Default date-range scraping still writes to the main `instagram_posts.xlsx` file.

## Confirm And Post Flow

1. In the UI, use **Canva Drive Designs**.
2. The app lists images and videos directly from your configured Google Drive Canva folder.
3. Click **Confirm**.
4. Edit caption, choose destinations (`Instagram`, `Facebook`), then click **Post**.

## Google Drive Setup

The publisher reads Canva assets from the Google Drive folder configured in `config.json` under `publisher.drive`.

- `folder_id`: Google Drive folder ID for Canva exports
- `credentials_file`: service account JSON path used to read that folder

## Google Sheets Setup (Required For Scraped Row Sync)

Scraped rows are appended to Google Sheets from the same scrape pipeline.

Configure `config.json -> publisher.sheets`:

- `enabled`: `true`
- `spreadsheet_id`: your Google Sheet ID
- `worksheet_name`: tab name, e.g. `Instagram Posts`
- `credentials_file`: service account JSON file path

Environment variables can override config:

```bash
export GOOGLE_SHEETS_ENABLED=true
export GOOGLE_SHEETS_SPREADSHEET_ID="<sheet_id>"
export GOOGLE_SHEETS_WORKSHEET="Instagram Posts"
export GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE="secrets/autoscraper-489906-6efe766866da.json"
```

Important:

- Share your Google Sheet with the service account email (`...@...iam.gserviceaccount.com`) as Editor.
- Install dependencies if missing: `google-auth` and `google-api-python-client`.

## Facebook Setup (Required For Facebook Posting)

Set either environment variables:

```bash
export FB_PAGE_ID="<your_page_id>"
export FB_PAGE_ACCESS_TOKEN="<your_page_access_token>"
export FB_API_VERSION="v22.0"
```

Or fill the values in `config.json` under `publisher.facebook`.

## Notes

- Instagram posting uses the logged-in account from `instagram_credentials`.
- The Confirm/Post gallery is sourced from Google Drive, not from scraped posts.
- For PNG/WebP images, the app converts to JPG before Instagram upload.
- Do not commit real credentials or tokens to git.

## AI Caption/OCR Rewrite

During scraping, each saved row now also includes:

- `generate caption/description`: SEO-focused rewritten caption with hashtags
- `newOcrText`: rewritten OCR text in better wording
- `postTItle`: concise meaningful title from rewritten content

OpenRouter settings are read from `config.json -> ai.openrouter` (or env vars `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`).
