# contentcreationautomation

## Run

```bash
python app.py
```

Open `http://localhost:5001`.

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
