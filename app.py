#!/usr/bin/env python3
"""
Flask Web UI for Instagram Profile Monitor
Run: python app.py
Open: http://localhost:5000
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

from flask import Flask, render_template, request, jsonify, send_file

from scraper import (
    BASE_DIR, CONFIG_FILE, load_config, get_loader,
    get_or_create_workbook, scrape_profile_in_range,
    load_seen_posts, save_seen_posts, monitor_profile,
    wait_with_jitter, append_post_to_excel, download_post_media,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

# ─── Global state ─────────────────────────────────────────────────────────────
_loader = None
_loader_lock = threading.Lock()
_config = None

# Job tracking
_jobs = {}  # job_id -> {status, logs, progress, total, done}
_job_counter = 0
_job_lock = threading.Lock()


def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_instaloader():
    """Get or create a shared Instaloader instance (thread-safe)."""
    global _loader
    with _loader_lock:
        if _loader is None:
            config = get_config()
            _loader = get_loader(config)
        return _loader


def new_job_id():
    global _job_counter
    with _job_lock:
        _job_counter += 1
        return f"job-{_job_counter}"


# ─── Routes ───────────────────────────────────────────────────────────────────

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
    data = request.get_json()
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

    job_id = new_job_id()
    _jobs[job_id] = {
        "status": "running",
        "logs": deque(maxlen=500),
        "total_profiles": len(selected_profiles),
        "profiles_done": 0,
        "posts_found": 0,
        "current_profile": "",
    }

    def run_job():
        config = get_config()
        excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
        media_folder = BASE_DIR / config.get("media_folder", "downloaded_media")
        media_folder.mkdir(exist_ok=True)
        get_or_create_workbook(excel_path)

        job = _jobs[job_id]

        try:
            L = get_instaloader()
        except Exception as e:
            job["logs"].append(f"Login failed: {e}")
            job["status"] = "error"
            return

        for i, username in enumerate(selected_profiles):
            job["current_profile"] = username
            job["logs"].append(f"--- Starting @{username} ({i+1}/{len(selected_profiles)}) ---")

            def progress_cb(msg):
                job["logs"].append(msg)

            try:
                count = scrape_profile_in_range(
                    L, username, date_from, date_to,
                    excel_path, media_folder, progress_cb=progress_cb
                )
                job["posts_found"] += count
                job["logs"].append(f"@{username}: {count} posts scraped")
            except Exception as e:
                job["logs"].append(f"Error for @{username}: {e}")

            job["profiles_done"] = i + 1

            # Delay between profiles
            if i < len(selected_profiles) - 1:
                import random
                delay = random.randint(15, 30)
                job["logs"].append(f"Waiting {delay}s before next profile...")
                time.sleep(delay)

        job["status"] = "completed"
        job["current_profile"] = ""
        job["logs"].append(f"=== Done! {job['posts_found']} total posts scraped ===")

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


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
    config = get_config()
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    if not os.path.exists(excel_path):
        return jsonify({"exists": False, "rows": 0, "size_kb": 0})

    try:
        from openpyxl import load_workbook as _lw
        wb = _lw(excel_path)
        ws = wb.active
        rows = max(0, ws.max_row - 1)
    except Exception:
        rows = 0

    size_kb = round(os.path.getsize(excel_path) / 1024, 1)
    return jsonify({"exists": True, "rows": rows, "size_kb": size_kb})


@app.route("/download")
def download_excel():
    """Download the Excel file."""
    config = get_config()
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    if not os.path.exists(excel_path):
        return "No Excel file yet. Run a scrape first.", 404
    return send_file(excel_path, as_attachment=True,
                     download_name="instagram_posts.xlsx")


@app.route("/api/excel-data")
def api_excel_data():
    """Return Excel rows as JSON for the data table preview."""
    config = get_config()
    excel_path = str(BASE_DIR / config.get("excel_file", "instagram_posts.xlsx"))
    if not os.path.exists(excel_path):
        return jsonify({"rows": [], "total": 0})

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, max(1, request.args.get("per_page", 25, type=int)))

    try:
        from openpyxl import load_workbook as _lw
        wb = _lw(excel_path)
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

        return jsonify({"rows": rows, "total": total_rows, "page": page, "per_page": per_page})
    except Exception as e:
        return jsonify({"rows": [], "total": 0, "error": str(e)})


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


if __name__ == "__main__":
    print("=" * 50)
    print("  Instagram Monitor — Web UI")
    print("  Open http://localhost:5001")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False)
