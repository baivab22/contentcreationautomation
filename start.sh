#!/usr/bin/env bash
# Startup script for Railway / Render free-tier deployment.
# Secrets are never committed to git. Instead, they are stored as
# environment variables in the platform dashboard and written to disk here.
set -e

echo "=== Instagram Scraper – startup ==="

# ── Create required directories ──────────────────────────────────────────────
mkdir -p secrets session fetch_exports downloaded_media/thumbnails tmp_publish

# ── Write config.json from env var ───────────────────────────────────────────
# Preferred: base64-encoded full JSON  (APP_CONFIG_JSON_B64)
# Fallback : raw JSON string           (APP_CONFIG_JSON)
if [ -n "$APP_CONFIG_JSON_B64" ]; then
    echo "$APP_CONFIG_JSON_B64" | base64 -d > config.json
    echo "✓ config.json written from APP_CONFIG_JSON_B64"
elif [ -n "$APP_CONFIG_JSON" ]; then
    printf '%s' "$APP_CONFIG_JSON" > config.json
    echo "✓ config.json written from APP_CONFIG_JSON"
elif [ -f config.json ]; then
    echo "✓ config.json already present"
else
    echo "ERROR: No config.json and no APP_CONFIG_JSON_B64/APP_CONFIG_JSON env var set." >&2
    exit 1
fi

# ── Write Google Sheets service-account key ───────────────────────────────────
# Store the whole JSON string in GOOGLE_SHEETS_CREDS_JSON env var.
if [ -n "$GOOGLE_SHEETS_CREDS_JSON" ]; then
    printf '%s' "$GOOGLE_SHEETS_CREDS_JSON" > secrets/autoscraper-489906-6efe766866da.json
    echo "✓ Sheets service-account key written"
fi

# ── Write Google Drive service-account key ────────────────────────────────────
if [ -n "$GOOGLE_DRIVE_CREDS_JSON" ]; then
    printf '%s' "$GOOGLE_DRIVE_CREDS_JSON" > secrets/ornate-grail-490114-f2-ad44024874d8.json
    echo "✓ Drive service-account key written"
fi

# ── Restore Instagram session (optional – avoids 2FA/challenge on cold start) ─
# Base64-encode your current session file and store it in IG_SESSION_FILE_B64.
#   Mac:  base64 -i session/session-baivab_bidari | pbcopy
#   Then paste result as env var IG_SESSION_FILE_B64 in the platform dashboard.
if [ -n "$IG_SESSION_FILE_B64" ]; then
    echo "$IG_SESSION_FILE_B64" | base64 -d > session/session-baivab_bidari
    echo "✓ Instagram session file restored"
fi
if [ -n "$IG_SESSION_JSON_B64" ]; then
    echo "$IG_SESSION_JSON_B64" | base64 -d > session/session-baivab_bidari.json
    echo "✓ Instagram session JSON restored"
fi

# ── Start gunicorn ────────────────────────────────────────────────────────────
# Single worker is required: the background monitor thread lives in process memory.
# Increasing workers would create multiple independent monitors that fight over
# the same Instagram session.
echo "=== Starting gunicorn on port ${PORT:-5001} ==="
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT:-5001}" \
    --workers 1 \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
