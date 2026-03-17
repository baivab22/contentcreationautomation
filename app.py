#!/usr/bin/env python3
"""
Flask Web UI for Instagram Profile Monitor
Run: python app.py
Open: http://localhost:5001
"""

import json
import io
import mimetypes
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import deque

import requests as http_requests
from flask import Flask, render_template, request, jsonify, send_file

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    service_account = None
    build = None
    HttpError = Exception
    MediaIoBaseDownload = None

from scraper import (
    BASE_DIR, CONFIG_FILE, load_config, get_loader,
    get_or_create_workbook, scrape_profile_in_range,
    load_seen_posts, save_seen_posts, monitor_profile,
    wait_with_jitter, append_post_to_excel, download_post_media,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

# ─── Global state ─────────────────────────────────────────────────────────────
_loader = None
_loader_identity = None
_loader_lock = threading.Lock()
_config = None
_config_mtime = None

# Job tracking
_jobs = {}  # job_id -> {status, logs, progress, total, done}
_job_counter = 0
_job_lock = threading.Lock()

# Publishing state and Drive-backed publisher settings
PUBLISH_STATE_FILE = BASE_DIR / "publish_state.json"
PUBLISH_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
PUBLISH_VIDEO_EXTS = {".mp4", ".mov"}
PUBLISH_ALLOWED_EXTS = PUBLISH_IMAGE_EXTS | PUBLISH_VIDEO_EXTS
DEFAULT_CANVA_DRIVE_FOLDER_ID = "144dVGG-_osDUavsJiMMAshG4_EVZ7skC"
INSTAGRAM_MIN_ASPECT_RATIO = 4 / 5
INSTAGRAM_MAX_ASPECT_RATIO = 1.91
INSTAGRAM_MAX_WIDTH = 1080
INSTAGRAM_MAX_HEIGHT = 1350
INSTAGRAM_MAX_CAPTION_LENGTH = 2200
FETCH_EXPORTS_DIR = BASE_DIR / "fetch_exports"

_publish_lock = threading.Lock()
_instagram_upload_lock = threading.Lock()
_scrape_runtime_lock = threading.Lock()
_monitor_thread = None
_monitor_lock = threading.Lock()
_monitor_stop_event = threading.Event()


def _load_publish_state():
    if not PUBLISH_STATE_FILE.exists():
        return {"confirmed": {}, "posted": {}}
    try:
        with open(PUBLISH_STATE_FILE, "r") as f:
            data = json.load(f)
        return {
            "confirmed": data.get("confirmed", {}),
            "posted": data.get("posted", {}),
        }
    except Exception:
        return {"confirmed": {}, "posted": {}}


_publish_state = _load_publish_state()


def get_config():
    global _config, _config_mtime
    config_mtime = None
    try:
        config_mtime = CONFIG_FILE.stat().st_mtime
    except FileNotFoundError:
        pass

    if _config is None or _config_mtime != config_mtime:
        _config = load_config()
        _config_mtime = config_mtime
    return _config


def _instagram_identity(config: dict):
    creds = config.get("instagram_credentials", {})
    return (
        (creds.get("username") or "").strip(),
        creds.get("password") or "",
    )


def get_instaloader():
    """Get or create a shared Instaloader instance (thread-safe)."""
    global _loader, _loader_identity
    config = get_config()
    identity = _instagram_identity(config)

    with _loader_lock:
        if _loader is None or _loader_identity != identity:
            _loader = get_loader(config)
            _loader_identity = identity
        return _loader


def reset_instagram_loader():
    global _loader, _loader_identity
    with _loader_lock:
        _loader = None
        _loader_identity = None


def new_job_id():
    global _job_counter
    with _job_lock:
        _job_counter += 1
        return f"job-{_job_counter}"


def get_enabled_profile_usernames():
    config = get_config()
    return [
        p["username"] for p in config.get("profiles", [])
        if p.get("username") and p.get("enabled")
    ]


def get_excel_reference(excel_path: Path) -> str:
    try:
        return excel_path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        return excel_path.name


def resolve_excel_path(file_ref: str | None) -> Path:
    if not file_ref:
        config = get_config()
        return (BASE_DIR / config.get("excel_file", "instagram_posts.xlsx")).resolve()

    candidate = Path(file_ref.strip())
    if candidate.is_absolute():
        raise ValueError("Absolute file paths are not allowed")

    resolved = (BASE_DIR / candidate).resolve()
    if not resolved.is_relative_to(BASE_DIR.resolve()):
        raise ValueError("Invalid Excel file path")
    if resolved.suffix.lower() != ".xlsx":
        raise ValueError("Only .xlsx files are supported")
    return resolved


def build_recent_fetch_excel_path(hours: int) -> Path:
    FETCH_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FETCH_EXPORTS_DIR / f"recent_{hours}h_{stamp}.xlsx"


def create_scrape_job(selected_profiles, date_from: datetime, date_to: datetime,
                      excel_path: Path, job_label: str, fresh_output: bool = False):
    excel_path = excel_path.resolve()
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    excel_ref = get_excel_reference(excel_path)

    job_id = new_job_id()
    _jobs[job_id] = {
        "status": "running",
        "logs": deque(maxlen=500),
        "total_profiles": len(selected_profiles),
        "profiles_done": 0,
        "posts_found": 0,
        "current_profile": "",
        "excel_file": excel_ref,
        "job_label": job_label,
    }

    def run_job():
        config = get_config()
        media_folder = BASE_DIR / config.get("media_folder", "downloaded_media")
        media_folder.mkdir(exist_ok=True)
        download_enabled = bool(config.get("download_media", True))

        if fresh_output and excel_path.exists():
            try:
                excel_path.unlink()
                job = _jobs[job_id]
                job["logs"].append("Previous export file cleared. Starting fresh output.")
            except Exception as e:
                job = _jobs[job_id]
                job["logs"].append(f"Could not clear old export file: {e}")

        get_or_create_workbook(str(excel_path))

        job = _jobs[job_id]

        try:
            with _scrape_runtime_lock:
                get_instaloader()
        except Exception as e:
            job["logs"].append(f"Login failed: {e}")
            job["status"] = "error"
            return

        for i, username in enumerate(selected_profiles):
            job["current_profile"] = username
            job["logs"].append(f"--- Starting @{username} ({i+1}/{len(selected_profiles)}) ---")

            def progress_cb(msg):
                job["logs"].append(msg)

            relogin_attempted = False
            while True:
                try:
                    with _scrape_runtime_lock:
                        L = get_instaloader()
                        count = scrape_profile_in_range(
                            L, username, date_from, date_to,
                            str(excel_path), media_folder, progress_cb=progress_cb,
                            download_enabled=download_enabled
                        )
                    job["posts_found"] += count
                    job["logs"].append(f"@{username}: {count} posts scraped")
                    break
                except Exception as e:
                    if is_challenge_error(e):
                        job["logs"].append(
                            f"Instagram requires manual verification. "
                            "Please open the Instagram app or instagram.com, approve the "
                            "security check for the scraper account, then try again."
                        )
                        job["status"] = "error"
                        return
                    if (not relogin_attempted) and is_retryable_instagram_error(e):
                        relogin_attempted = True
                        job["logs"].append(
                            "Session issue detected. Re-authenticating and retrying this profile once..."
                        )
                        try:
                            with _scrape_runtime_lock:
                                reset_instagram_loader()
                                get_instaloader()
                            job["logs"].append("Re-authentication successful. Retrying now...")
                            continue
                        except Exception as relogin_error:
                            if is_challenge_error(relogin_error):
                                job["logs"].append(
                                    "Instagram requires manual verification. "
                                    "Please open the Instagram app or instagram.com, approve the "
                                    "security check for the scraper account, then try again."
                                )
                                job["status"] = "error"
                                return
                            job["logs"].append(f"Re-authentication failed: {relogin_error}")

                    job["logs"].append(f"Error for @{username}: {e}")
                    break

            job["profiles_done"] = i + 1

            if i < len(selected_profiles) - 1:
                import random
                delay = random.randint(15, 30)
                job["logs"].append(f"Waiting {delay}s before next profile...")
                time.sleep(delay)

        job["status"] = "completed"
        job["current_profile"] = ""
        job["logs"].append(f"=== Done! {job['posts_found']} total posts scraped ===")
        job["logs"].append(f"Output Excel: {excel_ref}")

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    return job_id, excel_ref


def utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def should_run_background_monitor() -> bool:
    env_value = os.getenv("ENABLE_BACKGROUND_MONITOR")
    if env_value is None:
        return True
    return as_bool(env_value, default=True)


def run_background_monitor_loop():
    config = get_config()
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    media_folder = BASE_DIR / config.get("media_folder", "downloaded_media")
    media_folder.mkdir(exist_ok=True)
    download_enabled = bool(config.get("download_media", True))
    get_or_create_workbook(excel_path)

    seen = load_seen_posts()
    cycle = 0

    print("[Monitor] Background monitoring enabled.")
    print(f"[Monitor] Tracking {len(get_enabled_profile_usernames())} enabled profiles.")

    while not _monitor_stop_event.is_set():
        cycle += 1
        profiles = get_enabled_profile_usernames()

        total_new = 0
        if not profiles:
            print("[Monitor] No enabled profiles configured. Waiting for next cycle.")

        for index, username in enumerate(profiles):
            if _monitor_stop_event.is_set():
                break

            relogin_attempted = False
            while True:
                try:
                    with _scrape_runtime_lock:
                        L = get_instaloader()
                        new_count = monitor_profile(
                            L,
                            username,
                            seen,
                            excel_path,
                            media_folder,
                            download_enabled=download_enabled,
                        )

                    total_new += new_count
                    save_seen_posts(seen)
                    break
                except Exception as e:
                    if is_challenge_error(e):
                        print(
                            "[Monitor] Instagram requires manual verification. "
                            "Approve login in the app/web, then restart monitoring."
                        )
                        break

                    if (not relogin_attempted) and is_retryable_instagram_error(e):
                        relogin_attempted = True
                        print(f"[Monitor] Session issue for @{username}. Re-authenticating once...")
                        try:
                            with _scrape_runtime_lock:
                                reset_instagram_loader()
                                get_instaloader()
                            continue
                        except Exception as relogin_error:
                            print(f"[Monitor] Re-authentication failed for @{username}: {relogin_error}")

                    print(f"[Monitor] Error for @{username}: {e}")
                    break

            if index < len(profiles) - 1 and not _monitor_stop_event.is_set():
                wait_with_jitter(12, jitter=0.25)

        save_seen_posts(seen)
        interval = int(get_config().get("monitor_interval_seconds", 300))
        print(
            f"[Monitor] Cycle #{cycle} complete. New posts: {total_new}. "
            f"Next cycle in {interval}s."
        )

        slept = 0
        while slept < interval and not _monitor_stop_event.is_set():
            time.sleep(1)
            slept += 1


def start_background_monitor():
    global _monitor_thread

    if not should_run_background_monitor():
        print("[Monitor] Disabled via ENABLE_BACKGROUND_MONITOR.")
        return

    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return

        _monitor_stop_event.clear()
        _monitor_thread = threading.Thread(
            target=run_background_monitor_loop,
            name="instagram-background-monitor",
            daemon=True,
        )
        _monitor_thread.start()


def normalize_instagram_caption(caption: str) -> str:
    cleaned = (caption or "").replace("\r\n", "\n").strip()
    return cleaned[:INSTAGRAM_MAX_CAPTION_LENGTH]


def is_challenge_error(error: Exception) -> bool:
    """Errors that require manual human verification — must NOT be auto-retried."""
    msg = str(error).lower()
    name = type(error).__name__.lower()
    return (
        "challengeunknownstep" in name
        or "challenge" in msg
        or "checkpoint" in msg
        or "manual verification" in msg
    )


def is_retryable_instagram_error(error: Exception) -> bool:
    if is_challenge_error(error):
        return False  # challenge errors need manual action, never auto-retry
    message = str(error).lower()
    markers = (
        "login_required",
        "badpassword",
        "please wait a few minutes",
        "feedback_required",
        "sentry_block",
        "consent_required",
        "timed out",
        "connection reset",
        "connection aborted",
        "temporary failure",
    )
    return any(marker in message for marker in markers)


def _save_publish_state_locked():
    with open(PUBLISH_STATE_FILE, "w") as f:
        json.dump(_publish_state, f, indent=2)


def require_drive_client():
    if service_account is None or build is None or MediaIoBaseDownload is None:
        raise RuntimeError(
            "Google Drive dependencies are missing. Install google-auth and "
            "google-api-python-client in the active Python environment."
        )


def find_default_service_account_file():
    secrets_dir = BASE_DIR / "secrets"
    if not secrets_dir.exists():
        return ""

    for candidate in sorted(secrets_dir.glob("*.json")):
        return candidate.relative_to(BASE_DIR).as_posix()

    return ""


def get_publisher_config():
    config = get_config()
    publisher = config.get("publisher", {})
    drive_cfg = publisher.get("drive", {})
    sheets_cfg = publisher.get("sheets", {})
    return {
        "drive": {
            "folder_id": os.getenv("CANVA_DRIVE_FOLDER_ID") or drive_cfg.get("folder_id") or DEFAULT_CANVA_DRIVE_FOLDER_ID,
            # Drive can safely reuse the Sheets service account JSON if needed.
            "credentials_file": (
                os.getenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE")
                or drive_cfg.get("credentials_file")
                or os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE")
                or sheets_cfg.get("credentials_file")
                or find_default_service_account_file()
            ),
        },
        "facebook": publisher.get("facebook", {}),
    }


def get_drive_credentials_path() -> Path:
    config = get_config()
    publisher = config.get("publisher", {})
    drive_cfg = publisher.get("drive", {})
    sheets_cfg = publisher.get("sheets", {})

    raw_candidates = [
        os.getenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE", "").strip(),
        str(drive_cfg.get("credentials_file") or "").strip(),
        os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE", "").strip(),
        str(sheets_cfg.get("credentials_file") or "").strip(),
        find_default_service_account_file().strip(),
    ]

    checked_paths = []
    seen = set()
    for raw_path in raw_candidates:
        if not raw_path or raw_path in seen:
            continue
        seen.add(raw_path)

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate

        candidate = candidate.resolve()
        checked_paths.append(str(candidate))
        if candidate.exists() and candidate.is_file():
            return candidate

    if not checked_paths:
        raise RuntimeError(
            "Google Drive service account file is not configured. "
            "Set GOOGLE_DRIVE_CREDS_JSON_B64 or GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE."
        )

    raise RuntimeError(
        "Google Drive credentials file not found. Checked: "
        + ", ".join(checked_paths)
    )


def get_canva_drive_folder_id() -> str:
    folder_id = get_publisher_config().get("drive", {}).get("folder_id", "").strip()
    if not folder_id:
        raise RuntimeError("Google Drive Canva folder ID is not configured.")
    return folder_id


def get_drive_service():
    require_drive_client()
    credentials_path = get_drive_credentials_path()
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(credentials_path),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
    except Exception as e:
        raise RuntimeError(
            "Google Drive service account JSON is invalid or empty. "
            "Set GOOGLE_DRIVE_CREDS_JSON_B64 (preferred) or GOOGLE_DRIVE_CREDS_JSON "
            "in Render to a valid JSON key file."
        ) from e
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def is_supported_drive_mime_type(mime_type: str) -> bool:
    return mime_type.startswith("image/") or mime_type.startswith("video/")


def build_publish_item_key(file_id: str) -> str:
    return f"drive:{file_id}"


def format_drive_timestamp(value: str) -> str:
    if not value:
        return ""

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value


def get_drive_file_metadata(file_id: str) -> dict:
    if not file_id:
        raise ValueError("file_id is required")

    service = get_drive_service()
    try:
        metadata = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,modifiedTime,size",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        if getattr(e, "resp", None) and getattr(e.resp, "status", None) == 404:
            raise FileNotFoundError(f"Drive file not found: {file_id}") from e
        raise RuntimeError(f"Google Drive metadata lookup failed: {e}") from e

    mime_type = metadata.get("mimeType", "")
    if not is_supported_drive_mime_type(mime_type):
        raise ValueError("Unsupported Google Drive media type for publishing")

    return metadata


def discover_publish_items(limit: int = 60):
    service = get_drive_service()
    folder_id = get_canva_drive_folder_id()
    page_size = min(max(limit, 100), 1000)

    try:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType,modifiedTime,size)",
            orderBy="modifiedTime desc",
            pageSize=page_size,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"Google Drive folder listing failed: {e}") from e

    items = []
    for drive_file in response.get("files", []):
        mime_type = drive_file.get("mimeType", "")
        if not is_supported_drive_mime_type(mime_type):
            continue

        file_id = drive_file["id"]
        state_key = build_publish_item_key(file_id)

        with _publish_lock:
            confirmed_info = _publish_state.get("confirmed", {}).get(state_key)
            posted_info = _publish_state.get("posted", {}).get(state_key, {})

        items.append({
            "file_id": file_id,
            "filename": drive_file.get("name", file_id),
            "mime_type": mime_type,
            "media_type": "video" if mime_type.startswith("video/") else "image",
            "modified_at": format_drive_timestamp(drive_file.get("modifiedTime", "")),
            "confirmed": bool(confirmed_info),
            "confirmed_at": confirmed_info.get("confirmed_at") if confirmed_info else "",
            "last_posted_at": posted_info.get("posted_at", ""),
            "posted_targets": posted_info.get("targets", []),
        })

        if len(items) >= limit:
            break

    return items


def download_drive_file_bytes(file_id: str):
    metadata = get_drive_file_metadata(file_id)
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False

    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    return metadata, buffer


def guess_publish_suffix(filename: str, mime_type: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix:
        return suffix

    guessed = mimetypes.guess_extension(mime_type or "") or ""
    if guessed == ".jpe":
        return ".jpg"
    return guessed


def download_drive_file_to_temp(file_id: str):
    metadata, buffer = download_drive_file_bytes(file_id)
    temp_dir = BASE_DIR / "tmp_publish"
    temp_dir.mkdir(exist_ok=True)

    suffix = guess_publish_suffix(metadata.get("name", ""), metadata.get("mimeType", ""))
    temp_path = temp_dir / f"drive_{file_id}_{int(time.time() * 1000)}{suffix}"

    with open(temp_path, "wb") as f:
        f.write(buffer.getbuffer())

    return metadata, temp_path


def prepare_instagram_photo(file_path: Path):
    try:
        from PIL import Image as PILImage
    except Exception as e:
        raise RuntimeError(f"Pillow required for PNG/WebP upload: {e}") from e

    temp_dir = BASE_DIR / "tmp_publish"
    temp_dir.mkdir(exist_ok=True)
    converted = temp_dir / f"{file_path.stem}_{int(time.time())}.jpg"

    with PILImage.open(file_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        width, height = img.size
        if width <= 0 or height <= 0:
            raise RuntimeError("Invalid image dimensions for Instagram upload")

        ratio = width / height
        if ratio < INSTAGRAM_MIN_ASPECT_RATIO:
            padded_width = int(round(height * INSTAGRAM_MIN_ASPECT_RATIO))
            canvas = PILImage.new("RGB", (padded_width, height), color=(255, 255, 255))
            x_offset = (padded_width - width) // 2
            canvas.paste(img, (x_offset, 0))
            img = canvas
        elif ratio > INSTAGRAM_MAX_ASPECT_RATIO:
            padded_height = int(round(width / INSTAGRAM_MAX_ASPECT_RATIO))
            canvas = PILImage.new("RGB", (width, padded_height), color=(255, 255, 255))
            y_offset = (padded_height - height) // 2
            canvas.paste(img, (0, y_offset))
            img = canvas

        width, height = img.size
        if width > INSTAGRAM_MAX_WIDTH or height > INSTAGRAM_MAX_HEIGHT:
            scale = min(
                INSTAGRAM_MAX_WIDTH / width,
                INSTAGRAM_MAX_HEIGHT / height,
            )
            resized = (
                max(1, int(round(width * scale))),
                max(1, int(round(height * scale))),
            )
            if hasattr(PILImage, "Resampling"):
                img = img.resize(resized, PILImage.Resampling.LANCZOS)
            else:
                img = img.resize(resized, PILImage.LANCZOS)

        img.save(converted, "JPEG", quality=95)

    return converted, converted


def post_to_instagram(file_path: Path, caption: str):
    caption = normalize_instagram_caption(caption)
    cleanup_file = None
    upload_path = file_path

    try:
        is_video = file_path.suffix.lower() in PUBLISH_VIDEO_EXTS
        if not is_video:
            upload_path, cleanup_file = prepare_instagram_photo(file_path)

        with _instagram_upload_lock:
            cl = get_instaloader()
            try:
                if is_video:
                    media = cl.video_upload(str(upload_path), caption=caption)
                else:
                    media = cl.photo_upload(str(upload_path), caption=caption)
            except Exception as first_error:
                if not is_retryable_instagram_error(first_error):
                    raise

                reset_instagram_loader()
                cl = get_instaloader()
                if is_video:
                    media = cl.video_upload(str(upload_path), caption=caption)
                else:
                    media = cl.photo_upload(str(upload_path), caption=caption)

        code = getattr(media, "code", "")
        return {
            "id": str(getattr(media, "pk", "")),
            "code": code,
            "url": f"https://www.instagram.com/p/{code}/" if code else "",
        }
    except Exception as e:
        raise RuntimeError(f"Instagram publish failed: {e}") from e
    finally:
        if cleanup_file and cleanup_file.exists():
            cleanup_file.unlink(missing_ok=True)


def post_to_facebook(file_path: Path, caption: str):
    fb_cfg = get_publisher_config().get("facebook", {})
    page_id = os.getenv("FB_PAGE_ID") or fb_cfg.get("page_id")
    access_token = os.getenv("FB_PAGE_ACCESS_TOKEN") or fb_cfg.get("access_token")
    api_version = os.getenv("FB_API_VERSION") or fb_cfg.get("api_version", "v22.0")

    if not page_id or not access_token:
        raise RuntimeError(
            "Facebook config missing. Set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN "
            "or configure config.json -> publisher.facebook"
        )

    is_video = file_path.suffix.lower() in PUBLISH_VIDEO_EXTS
    endpoint = (
        f"https://graph.facebook.com/{api_version}/{page_id}/videos"
        if is_video
        else f"https://graph.facebook.com/{api_version}/{page_id}/photos"
    )

    payload = {"access_token": access_token}
    payload["description" if is_video else "caption"] = caption

    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    with open(file_path, "rb") as media_file:
        response = http_requests.post(
            endpoint,
            data=payload,
            files={"source": (file_path.name, media_file, content_type)},
            timeout=300,
        )

    try:
        data = response.json()
    except ValueError:
        data = {"error": {"message": response.text}}

    if response.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message", response.text)
        raise RuntimeError(f"Facebook publish failed: {message}")

    return {
        "id": data.get("id", ""),
        "post_id": data.get("post_id", ""),
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route("/")
def index():
    config = get_config()
    profiles = [p for p in config["profiles"] if p.get("username") and p.get("enabled")]
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    excel_exists = os.path.exists(excel_path)

    # Count rows in excel
    row_count = 0
    if excel_exists:
        try:
            from openpyxl import load_workbook as _lw
            wb = _lw(excel_path)
            ws = wb.active
            row_count = max(0, ws.max_row - 1)  # minus header
        except Exception:
            pass

    return render_template("index.html",
                           profiles=profiles,
                           excel_exists=excel_exists,
                           row_count=row_count)


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """Start a scraping job for selected profiles within a time range."""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No JSON data provided"}), 400

    selected_profiles = data.get("profiles", [])
    date_from_str = data.get("date_from", "")
    date_to_str = data.get("date_to", "")

    if not selected_profiles:
        return jsonify({"error": "No profiles selected"}), 400
    if not date_from_str or not date_to_str:
        return jsonify({"error": "Both date_from and date_to are required"}), 400

    try:
        date_from = datetime.strptime(date_from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        date_to = datetime.strptime(date_to_str, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    if date_from > date_to:
        return jsonify({"error": "date_from must be before date_to"}), 400

    excel_path = resolve_excel_path(None)
    job_id, excel_ref = create_scrape_job(
        selected_profiles,
        date_from,
        date_to,
        excel_path=excel_path,
        job_label="custom-range",
        fresh_output=True,
    )

    return jsonify({"job_id": job_id, "status": "started", "excel_file": excel_ref})


@app.route("/api/fetch/recent", methods=["POST"])
def api_fetch_recent():
    """Fetch recent posts for all listed (enabled) accounts into a fresh Excel file."""
    data = request.get_json(silent=True) or {}

    try:
        hours = int(data.get("hours", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "hours must be an integer"}), 400

    if hours < 1 or hours > 168:
        return jsonify({"error": "hours must be between 1 and 168"}), 400

    selected_profiles = get_enabled_profile_usernames()
    if not selected_profiles:
        return jsonify({"error": "No enabled profiles available in config"}), 400

    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(hours=hours)
    excel_path = build_recent_fetch_excel_path(hours)

    job_id, excel_ref = create_scrape_job(
        selected_profiles,
        date_from,
        date_to,
        excel_path=excel_path,
        job_label=f"recent-{hours}h-all",
        fresh_output=True,
    )

    return jsonify({
        "job_id": job_id,
        "status": "started",
        "hours": hours,
        "profiles": len(selected_profiles),
        "excel_file": excel_ref,
    })


@app.route("/api/job/<job_id>")
def api_job_status(job_id):
    """Get status of a scraping job."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status": job["status"],
        "logs": list(job["logs"]),
        "total_profiles": job["total_profiles"],
        "profiles_done": job["profiles_done"],
        "posts_found": job["posts_found"],
        "current_profile": job["current_profile"],
        "excel_file": job.get("excel_file", ""),
        "job_label": job.get("job_label", ""),
    })


@app.route("/api/profiles")
def api_profiles():
    """Get list of configured profiles."""
    config = get_config()
    profiles = [p for p in config["profiles"] if p.get("username") and p.get("enabled")]
    return jsonify(profiles)


@app.route("/api/excel-info")
def api_excel_info():
    """Get info about current Excel file."""
    file_ref = (request.args.get("file") or "").strip()
    try:
        excel_path = resolve_excel_path(file_ref or None)
    except ValueError as e:
        return jsonify({"exists": False, "rows": 0, "size_kb": 0, "error": str(e)}), 400

    excel_ref = get_excel_reference(excel_path)
    if not os.path.exists(excel_path):
        return jsonify({"exists": False, "rows": 0, "size_kb": 0, "file": excel_ref})

    try:
        from openpyxl import load_workbook as _lw
        wb = _lw(str(excel_path))
        ws = wb.active
        rows = max(0, ws.max_row - 1)
    except Exception:
        rows = 0

    size_kb = round(os.path.getsize(str(excel_path)) / 1024, 1)
    return jsonify({"exists": True, "rows": rows, "size_kb": size_kb, "file": excel_ref})


@app.route("/api/publisher/items")
def api_publisher_items():
    """List publishable media files from the Canva Google Drive folder."""
    limit = min(200, max(1, request.args.get("limit", 60, type=int)))
    try:
        items = discover_publish_items(limit=limit)
    except Exception as e:
        return jsonify({"error": str(e), "items": [], "count": 0}), 500
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/publisher/confirm", methods=["POST"])
def api_publisher_confirm():
    """Mark one Canva Drive media file as confirmed before posting."""
    data = request.get_json(silent=True) or {}
    file_id = (data.get("file_id") or "").strip()
    if not file_id:
        return jsonify({"error": "file_id is required"}), 400

    try:
        metadata = get_drive_file_metadata(file_id)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    item_key = build_publish_item_key(file_id)

    with _publish_lock:
        _publish_state.setdefault("confirmed", {})[item_key] = {
            "confirmed_at": utc_now_text(),
            "filename": metadata.get("name", file_id),
            "file_id": file_id,
        }
        _save_publish_state_locked()

    return jsonify({
        "status": "confirmed",
        "filename": metadata.get("name", file_id),
        "file_id": file_id,
    })


@app.route("/api/publisher/post", methods=["POST"])
def api_publisher_post():
    """Post a confirmed Canva Drive media file to Instagram and/or Facebook."""
    data = request.get_json(silent=True) or {}
    file_id = (data.get("file_id") or "").strip()
    caption = (data.get("caption") or "").strip()
    post_instagram = as_bool(data.get("post_instagram"), default=True)
    post_facebook = as_bool(data.get("post_facebook"), default=True)

    if not file_id:
        return jsonify({"error": "file_id is required"}), 400
    if not post_instagram and not post_facebook:
        return jsonify({"error": "Select at least one destination"}), 400

    try:
        metadata, file_path = download_drive_file_to_temp(file_id)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    item_key = build_publish_item_key(file_id)

    with _publish_lock:
        is_confirmed = item_key in _publish_state.get("confirmed", {})
    if not is_confirmed:
        file_path.unlink(missing_ok=True)
        return jsonify({"error": "Please confirm this item before posting."}), 400

    results = {}
    errors = {}

    try:
        if post_instagram:
            try:
                results["instagram"] = post_to_instagram(file_path, caption)
            except Exception as e:
                errors["instagram"] = str(e)

        if post_facebook:
            try:
                results["facebook"] = post_to_facebook(file_path, caption)
            except Exception as e:
                errors["facebook"] = str(e)
    finally:
        file_path.unlink(missing_ok=True)

    with _publish_lock:
        _publish_state.setdefault("posted", {})[item_key] = {
            "posted_at": utc_now_text(),
            "file_id": file_id,
            "filename": metadata.get("name", file_id),
            "targets": list(results.keys()),
            "results": results,
            "errors": errors,
        }
        if results:
            _publish_state.get("confirmed", {}).pop(item_key, None)
        _save_publish_state_locked()

    if results and errors:
        status = "partial"
        code = 200
    elif results:
        status = "success"
        code = 200
    else:
        status = "error"
        code = 502

    return jsonify({
        "status": status,
        "file_id": file_id,
        "filename": metadata.get("name", file_id),
        "results": results,
        "errors": errors,
    }), code


@app.route("/download")
def download_excel():
    """Download the Excel file."""
    file_ref = (request.args.get("file") or "").strip()
    try:
        excel_path = resolve_excel_path(file_ref or None)
    except ValueError as e:
        return str(e), 400

    if not os.path.exists(excel_path):
        return "No Excel file yet. Run a scrape first.", 404

    download_name = excel_path.name if file_ref else "instagram_posts.xlsx"
    return send_file(str(excel_path), as_attachment=True,
                     download_name=download_name)


@app.route("/api/excel-data")
def api_excel_data():
    """Return Excel rows as JSON for the data table preview."""
    file_ref = (request.args.get("file") or "").strip()
    try:
        excel_path = resolve_excel_path(file_ref or None)
    except ValueError as e:
        return jsonify({"rows": [], "total": 0, "error": str(e)}), 400

    excel_ref = get_excel_reference(excel_path)
    if not os.path.exists(excel_path):
        return jsonify({"rows": [], "total": 0, "file": excel_ref})

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(1, request.args.get("per_page", 25, type=int)))

    try:
        from openpyxl import load_workbook as _lw
        wb = _lw(str(excel_path))
        ws = wb.active
        total_rows = max(0, ws.max_row - 1)

        headers = [cell.value for cell in ws[1]]
        # Read rows in reverse (newest first)
        start_row = max(2, ws.max_row - (page * per_page) + 1)
        end_row = min(ws.max_row, ws.max_row - ((page - 1) * per_page))

        rows = []
        for r in range(end_row, start_row - 1, -1):
            row_data = {}
            for col_idx, header in enumerate(headers, 1):
                val = ws.cell(row=r, column=col_idx).value
                if header and header != "Embedded Image":
                    row_data[header] = str(val) if val is not None else ""
            rows.append(row_data)

        return jsonify({
            "rows": rows,
            "total": total_rows,
            "page": page,
            "per_page": per_page,
            "file": excel_ref,
        })
    except Exception as e:
        return jsonify({"rows": [], "total": 0, "error": str(e), "file": excel_ref})


@app.route("/media/<path:filename>")
def serve_media(filename):
    """Serve downloaded media files."""
    config = get_config()
    media_folder = BASE_DIR / config.get("media_folder", "downloaded_media")
    filepath = media_folder / filename
    # Prevent path traversal
    if not filepath.resolve().is_relative_to(media_folder.resolve()):
        return "Forbidden", 403
    if not filepath.exists():
        return "File not found", 404
    return send_file(str(filepath))


@app.route("/publisher-media/drive/<file_id>")
def serve_publisher_media(file_id):
    """Serve preview media directly from the Canva Google Drive folder."""
    try:
        metadata, buffer = download_drive_file_bytes(file_id)
    except FileNotFoundError:
        return "File not found", 404
    except ValueError:
        return "Unsupported media type", 400
    except Exception:
        return "Failed to load Google Drive media", 500

    return send_file(
        buffer,
        mimetype=metadata.get("mimeType", "application/octet-stream"),
        download_name=metadata.get("name", file_id),
    )


if __name__ == "__main__":
    print("=" * 50)
    print("  Instagram Monitor — Web UI")
    print("  Open http://localhost:5001")
    print("=" * 50)
    start_background_monitor()
    app_port = int(os.getenv("PORT", "5001"))
    app.run(host="0.0.0.0", port=app_port, debug=False)
