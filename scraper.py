#!/usr/bin/env python3
"""
Instagram Profile Monitor — Core scraper module (instagrapi-based)
- Uses Instagram's Private Mobile API (fewer requests, less rate-limiting)
- One-time login with session persistence
- Fetches posts within a date range efficiently
- Downloads post images/videos
- Extracts text from images via OCR
- Saves everything to an Excel file with embedded media
"""

import json
import os
import sys
import time
import random
import hashlib
import shutil
import threading
import requests as _requests
from datetime import datetime, timezone
from pathlib import Path

from instagrapi import Client
from instagrapi.types import Media
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
SESSION_DIR = BASE_DIR / "session"
SEEN_FILE = BASE_DIR / "seen_posts.json"

# ─── Load config ──────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

# ─── Session management ──────────────────────────────────────────────────────
def get_loader(config):
    """Create an instagrapi Client and login with session persistence."""
    cl = Client()
    cl.delay_range = [2, 5]

    username = config["instagram_credentials"]["username"]
    password = config["instagram_credentials"]["password"]
    session_file = SESSION_DIR / f"session-{username}.json"
    SESSION_DIR.mkdir(exist_ok=True)

    # 1) Try loading saved session
    if session_file.exists():
        print(f"[*] Loading saved session for @{username}...")
        try:
            cl.load_settings(str(session_file))
            cl.login(username, password)
            cl.get_timeline_feed()
            print(f"[+] Session valid for @{username}")
            return cl
        except Exception as e:
            print(f"[!] Saved session invalid ({e}), doing fresh login...")
            session_file.unlink(missing_ok=True)

    # 2) Fresh login
    print(f"[*] Logging in as @{username}...")
    cl.login(username, password)
    cl.dump_settings(str(session_file))
    print(f"[+] Login successful. Session saved.")
    return cl


# ─── Seen-posts tracking ─────────────────────────────────────────────────────
def load_seen_posts():
    if SEEN_FILE.exists():
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_posts(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ─── OCR ──────────────────────────────────────────────────────────────────────
def extract_text_from_image(image_path: str) -> str:
    """Run Tesseract OCR on an image and return extracted text."""
    if not HAS_TESSERACT:
        return ""
    try:
        img = PILImage.open(image_path)
        text = pytesseract.image_to_string(img)
        return text.strip()
    except Exception as e:
        return f"[OCR Error: {e}]"


# ─── Media download ──────────────────────────────────────────────────────────
def _download_url(url: str, filepath: Path):
    """Download a URL to a local file."""
    if filepath.exists():
        return
    resp = _requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(filepath, "wb") as f:
        f.write(resp.content)


def download_post_media(cl, media: Media, media_folder: Path) -> list[dict]:
    """Download all media (images/videos) for a post. Returns list of {path, type}."""
    media_folder.mkdir(parents=True, exist_ok=True)
    media_files = []

    code = media.code
    date_str = media.taken_at.strftime("%Y%m%d_%H%M%S")
    owner = media.user.username if media.user else "unknown"
    base_name = f"{owner}_{date_str}_{code}"

    if media.media_type == 8:  # Album / carousel
        for idx, resource in enumerate(media.resources or [], 1):
            if resource.video_url:
                ext = "mp4"
                url = str(resource.video_url)
                mtype = "video"
            else:
                ext = "jpg"
                url = str(resource.thumbnail_url)
                mtype = "image"
            filename = f"{base_name}_{idx}.{ext}"
            filepath = media_folder / filename
            _download_url(url, filepath)
            media_files.append({"path": str(filepath), "type": mtype})
    elif media.media_type == 2 and media.video_url:  # Video
        filename = f"{base_name}.mp4"
        filepath = media_folder / filename
        _download_url(str(media.video_url), filepath)
        media_files.append({"path": str(filepath), "type": "video"})
    else:  # Photo
        filename = f"{base_name}.jpg"
        filepath = media_folder / filename
        url = str(media.thumbnail_url)
        _download_url(url, filepath)
        media_files.append({"path": str(filepath), "type": "image"})

    return media_files


# ─── Excel management ────────────────────────────────────────────────────────
HEADERS = [
    "Profile",
    "Post Date (UTC)",
    "Shortcode",
    "Caption / Description",
    "Media Type",
    "Media File Path",
    "Embedded Image",
    "OCR Text from Image",
    "Post URL",
    "Scraped At",
]

HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)


def get_or_create_workbook(excel_path: str):
    """Open existing workbook or create a new one with headers."""
    if os.path.exists(excel_path):
        wb = load_workbook(excel_path)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Instagram Posts"
        for col_idx, header in enumerate(HEADERS, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = THIN_BORDER

        col_widths = [18, 22, 16, 50, 12, 40, 25, 50, 40, 22]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        wb.save(excel_path)

    return wb, ws


def make_thumbnail(image_path: str, max_size: int = 150) -> str | None:
    """Create a thumbnail for embedding in Excel."""
    try:
        thumb_dir = Path(image_path).parent / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        thumb_path = thumb_dir / f"thumb_{Path(image_path).name}"

        if thumb_path.exists():
            return str(thumb_path)

        img = PILImage.open(image_path)
        img.thumbnail((max_size, max_size))

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        thumb_path_jpg = thumb_path.with_suffix(".jpg")
        img.save(thumb_path_jpg, "JPEG", quality=85)
        return str(thumb_path_jpg)
    except Exception as e:
        print(f"  [!] Thumbnail error: {e}")
        return None


def append_post_to_excel(excel_path: str, row_data: dict, media_files: list[dict]):
    """Append one or more rows (one per media item) for a post to the Excel file."""
    wb, ws = get_or_create_workbook(excel_path)

    for media in media_files:
        next_row = ws.max_row + 1
        media_path = media["path"]
        media_type = media["type"]

        ocr_text = ""
        if media_type == "image":
            ocr_text = extract_text_from_image(media_path)

        values = [
            row_data["profile"],
            row_data["post_date"],
            row_data["shortcode"],
            row_data["caption"],
            media_type,
            media_path,
            "",
            ocr_text,
            row_data["post_url"],
            row_data["scraped_at"],
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=next_row, column=col_idx, value=val)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = THIN_BORDER

        if media_type == "image":
            thumb_path = make_thumbnail(media_path)
            if thumb_path:
                try:
                    img = XlImage(thumb_path)
                    img.width = 120
                    img.height = 120
                    cell_ref = f"G{next_row}"
                    ws.add_image(img, cell_ref)
                    ws.row_dimensions[next_row].height = 100
                except Exception as e:
                    print(f"  [!] Could not embed image: {e}")
        else:
            ws.cell(row=next_row, column=7, value=f"[Video: {Path(media_path).name}]")

    wb.save(excel_path)
    print(f"  [Excel] Saved to {excel_path}")


# ─── Profile scraping ────────────────────────────────────────────────────────
def wait_with_jitter(base_seconds: float, jitter: float = 0.5):
    """Sleep for base_seconds +/- jitter fraction."""
    actual = base_seconds * (1 + random.uniform(-jitter, jitter))
    time.sleep(max(1, actual))


def scrape_profile_in_range(cl, username: str,
                            date_from: datetime, date_to: datetime,
                            excel_path: str, media_folder: Path,
                            progress_cb=None):
    """
    Scrape posts from a profile within a date range using instagrapi.
    Uses user_medias_paginated — fetches ~20 posts per API call,
    far fewer requests than the web GraphQL API.
    Returns number of posts scraped.
    """
    count = 0
    def log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    try:
        user_id = cl.user_id_from_username(username)
        user_info = cl.user_info(user_id)
        log(f"Checking @{username} ({user_info.media_count} total posts)...")

        end_cursor = ""
        while True:
            medias, end_cursor = cl.user_medias_paginated(user_id, 20, end_cursor=end_cursor)

            if not medias:
                break

            reached_older = False
            for media in medias:
                post_date = media.taken_at
                if post_date.tzinfo is None:
                    post_date = post_date.replace(tzinfo=timezone.utc)

                if post_date > date_to:
                    continue

                if post_date < date_from:
                    reached_older = True
                    break

                code = media.code
                log(f"  Found: {code} ({post_date.strftime('%Y-%m-%d %H:%M:%S')})")

                media_files = download_post_media(cl, media, media_folder)
                caption = media.caption_text or ""
                row_data = {
                    "profile": username,
                    "post_date": post_date.strftime("%Y-%m-%d %H:%M:%S"),
                    "shortcode": code,
                    "caption": caption[:2000],
                    "post_url": f"https://www.instagram.com/p/{code}/",
                    "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                }
                append_post_to_excel(excel_path, row_data, media_files)
                count += 1
                log(f"  Saved post {count}: {code}")

            if reached_older or not end_cursor:
                break

            wait_with_jitter(3)

    except Exception as e:
        log(f"Error for @{username}: {e}")

    return count


def monitor_profile(cl, username: str, seen: set,
                    excel_path: str, media_folder: Path, max_posts: int = 5):
    """Check a profile for new posts not yet in seen set."""
    new_count = 0
    try:
        user_id = cl.user_id_from_username(username)
        user_info = cl.user_info(user_id)
        print(f"\n[*] Checking @{username} ({user_info.media_count} posts)...")

        medias, _ = cl.user_medias_paginated(user_id, max_posts * 2)

        for media in medias:
            post_id = str(media.pk)
            if post_id in seen:
                break

            code = media.code
            print(f"  [+] New post: {code} ({media.taken_at})")

            media_files = download_post_media(cl, media, media_folder)
            caption = media.caption_text or ""
            row_data = {
                "profile": username,
                "post_date": media.taken_at.strftime("%Y-%m-%d %H:%M:%S"),
                "shortcode": code,
                "caption": caption[:2000],
                "post_url": f"https://www.instagram.com/p/{code}/",
                "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
            append_post_to_excel(excel_path, row_data, media_files)

            seen.add(post_id)
            new_count += 1

            if new_count >= max_posts:
                print(f"  [*] Reached max {max_posts} new posts for this cycle.")
                break

    except Exception as e:
        print(f"  [!] Error for @{username}: {e}")

    return new_count


# ─── Main loop ────────────────────────────────────────────────────────────────
def main():
    config = load_config()
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    media_folder = BASE_DIR / config.get("media_folder", "downloaded_media")
    interval = config.get("monitor_interval_seconds", 300)
    media_folder.mkdir(exist_ok=True)

    profiles = [p["username"] for p in config["profiles"] if p.get("enabled") and p.get("username")]

    if not profiles:
        print("[!] No enabled profiles found in config.json")
        sys.exit(1)

    print(f"[*] Monitoring {len(profiles)} profiles: {', '.join(profiles)}")
    print(f"[*] Check interval: {interval}s")
    print(f"[*] Excel file: {excel_path}")
    print(f"[*] Media folder: {media_folder}")
    print()

    cl = get_loader(config)
    get_or_create_workbook(excel_path)

    seen = load_seen_posts()
    print(f"[*] Already seen {len(seen)} posts from previous runs.")

    cycle = 0
    while True:
        cycle += 1
        print(f"\n{'='*60}")
        print(f"  MONITOR CYCLE #{cycle}  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        total_new = 0
        for i, username in enumerate(profiles):
            try:
                new = monitor_profile(cl, username, seen, excel_path, media_folder)
                total_new += new
                save_seen_posts(seen)
            except Exception as e:
                print(f"[!] Error monitoring @{username}: {e}")

            delay = random.randint(15, 30)
            print(f"  [*] Waiting {delay}s before next profile...")
            time.sleep(delay)

        print(f"\n[*] Cycle #{cycle} complete. {total_new} new posts found.")
        print(f"[*] Total tracked posts: {len(seen)}")
        print(f"[*] Next check in {interval} seconds... (Ctrl+C to stop)")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[*] Stopping monitor. Goodbye!")
            save_seen_posts(seen)
            break


if __name__ == "__main__":
    main()
