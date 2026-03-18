#!/usr/bin/env bash
# Startup script for Railway / Render free-tier deployment.
# Secrets are injected via environment variables and written to local files.
set -euo pipefail

echo "=== Instagram Scraper startup ==="

# Create required directories.
mkdir -p secrets session fetch_exports downloaded_media/thumbnails tmp_publish

decode_base64_env_to_file() {
    local env_name="$1"
    local output_file="$2"
    python - "$env_name" "$output_file" <<'PY'
import base64
import os
import sys

name = sys.argv[1]
output_file = sys.argv[2]
raw = os.getenv(name, "")
if not raw:
    sys.exit(2)

# Remove whitespace and accidental shell prompt tail characters.
value = "".join(raw.strip().split())
if value.endswith("%"):
    value = value[:-1]

try:
    decoded = base64.b64decode(value, validate=False)
except Exception as exc:
    print(f"ERROR: Invalid base64 in {name}: {exc}", file=sys.stderr)
    sys.exit(1)

with open(output_file, "wb") as f:
    f.write(decoded)
PY
}

write_fallback_config() {
        cat > config.json <<'JSON'
{
    "instagram_credentials": {
        "username": "",
        "password": ""
    },
    "profiles": [],
    "monitor_interval_seconds": 300,
    "download_media": false,
    "excel_file": "instagram_posts.xlsx",
    "media_folder": "downloaded_media",
    "ai": {
        "openrouter": {
            "api_key": "",
            "model": "deepseek/deepseek-r1",
            "prompt": "",
            "timeout_seconds": 90,
            "temperature": 0.5
        }
    },
    "publisher": {
        "sheets": {
            "enabled": true,
            "spreadsheet_id": "",
            "worksheet_name": "Instagram Posts",
            "credentials_file": "secrets/autoscraper-489906-6efe766866da.json"
        },
        "facebook": {
            "page_id": "",
            "access_token": "",
            "api_version": "v22.0"
        }
    }
}
JSON
}

validate_json_file() {
        local file_path="$1"
        python - "$file_path" <<'PY'
import json
import sys

file_path = sys.argv[1]
try:
        with open(file_path, "r", encoding="utf-8") as f:
                json.load(f)
except Exception as exc:
        print(f"ERROR: Invalid JSON in {file_path}: {exc}", file=sys.stderr)
        sys.exit(1)
PY
}

write_json_secret_file() {
    local raw_env_name="$1"
    local b64_env_name="$2"
    local output_file="$3"
    local label="$4"

    local raw_value="${!raw_env_name:-}"
    local b64_value="${!b64_env_name:-}"

    if [[ -n "$b64_value" ]]; then
        if decode_base64_env_to_file "$b64_env_name" "$output_file" \
            && validate_json_file "$output_file"; then
            echo "$label written from $b64_env_name"
            return 0
        fi
        echo "WARNING: $b64_env_name is invalid base64/JSON. Ignoring it."
        rm -f "$output_file"
        return 1
    fi

    if [[ -n "$raw_value" ]]; then
        printf '%s' "$raw_value" > "$output_file"
        if validate_json_file "$output_file"; then
            echo "$label written from $raw_env_name"
            return 0
        fi
        echo "WARNING: $raw_env_name is invalid JSON. Ignoring it."
        rm -f "$output_file"
        return 1
    fi

    return 2
}

# Write config.json from env var.
# Preferred: base64-encoded full JSON  (APP_CONFIG_JSON_B64)
# Fallback : raw JSON string           (APP_CONFIG_JSON)
if decode_base64_env_to_file "APP_CONFIG_JSON_B64" "config.json"; then
    echo "config.json written from APP_CONFIG_JSON_B64"
elif [[ -n "${APP_CONFIG_JSON:-}" ]]; then
    printf '%s' "$APP_CONFIG_JSON" > config.json
    echo "config.json written from APP_CONFIG_JSON"
elif [[ -f config.json ]]; then
    echo "config.json already present"
else
        write_fallback_config
    echo "WARNING: No config env found. Started with a minimal fallback config."
fi

if ! validate_json_file "config.json"; then
        echo "WARNING: Replacing malformed config.json with fallback defaults."
        write_fallback_config
fi

# Write Google Sheets service-account key.
write_json_secret_file \
    "GOOGLE_SHEETS_CREDS_JSON" \
    "GOOGLE_SHEETS_CREDS_JSON_B64" \
    "secrets/autoscraper-489906-6efe766866da.json" \
    "Sheets service-account key" || true

# Restore Instagram session (optional).
if [[ -n "${IG_SESSION_FILE_B64:-}" ]]; then
    if decode_base64_env_to_file "IG_SESSION_FILE_B64" "session/session-baivab_bidari"; then
        echo "Instagram session file restored"
    else
        echo "WARNING: Failed to decode IG_SESSION_FILE_B64"
    fi
fi
if [[ -n "${IG_SESSION_JSON_B64:-}" ]]; then
    if decode_base64_env_to_file "IG_SESSION_JSON_B64" "session/session-baivab_bidari.json"; then
        echo "Instagram session JSON restored"
    else
        echo "WARNING: Failed to decode IG_SESSION_JSON_B64"
    fi
fi

# Start gunicorn.
# Keep one worker so only one background monitor thread runs.
echo "=== Starting gunicorn on port ${PORT:-5001} ==="
exec gunicorn app:app \
    --bind "0.0.0.0:${PORT:-5001}" \
    --workers 1 \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile -
