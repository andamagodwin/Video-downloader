"""Local web app for downloading YouTube videos.

Run:  python app.py            (opens http://127.0.0.1:8765 in your browser)
      python app.py --no-browser --port 9000
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import flask.cli
from flask import Flask, abort, jsonify, request, send_from_directory
from yt_dlp.utils import DownloadCancelled

import downloader as dl

BASE_DIR = Path(__file__).resolve().parent
MAX_PARALLEL = 3

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_PARALLEL)
ACTIVE = {"queued", "starting", "downloading", "processing"}


# ----------------------------------------------------------------- helpers


@app.before_request
def only_local():
    # The server only listens on 127.0.0.1; also reject other Host headers (DNS rebinding).
    host = (request.host or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        abort(403)
    # Actions need a custom header, which other websites can't send without a CORS
    # preflight this server never approves, so they can't trigger downloads behind your back.
    if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "fetch":
        abort(403)


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and len(url) < 2000


def public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


def update(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def reveal(path: str):
    """Show a file in Finder / Explorer, or open a folder."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", path] if os.path.isfile(path) else ["open", path]
    elif os.name == "nt":
        cmd = ["explorer", f"/select,{path}"] if os.path.isfile(path) else ["explorer", path]
    else:
        cmd = ["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------- worker


def run_job(job_id: str):
    with jobs_lock:
        job = jobs[job_id]
        url, quality, playlist = job["url"], job["quality"], job["playlist"]

    with slots:
        if jobs[job_id]["_cancel"]:
            update(job_id, status="cancelled", finished=time.time())
            return
        update(job_id, status="starting")

        def on_progress(p: dict):
            if p["phase"] == "downloading":
                update(
                    job_id,
                    status="downloading",
                    percent=p["percent"],
                    downloaded=p["downloaded"],
                    total=p["total"],
                    speed=p["speed"],
                    eta=p["eta"],
                    part=p["part"],
                    index=p["index"],
                    count=p["count"] or jobs[job_id].get("count"),
                    step=None,
                )
            else:
                update(job_id, status="processing", step=p["step"])

        try:
            files = dl.download(
                url,
                quality,
                dl.DEFAULT_DIR,
                playlist=playlist,
                on_progress=on_progress,
                should_cancel=lambda: jobs[job_id]["_cancel"],
            )
            size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
            update(
                job_id,
                status="done",
                percent=100,
                files=[os.path.basename(f) for f in files],
                size=size,
                finished=time.time(),
                _paths=files,
            )
        except DownloadCancelled:
            update(job_id, status="cancelled", finished=time.time())
        except dl.DownloaderError as e:
            update(job_id, status="error", error=str(e), finished=time.time())
        except Exception as e:  # keep the server alive no matter what
            app.logger.exception("Download failed")
            update(job_id, status="error", error=dl.clean_error(e), finished=time.time())


# --------------------------------------------------------------- routes


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.get("/api/config")
def config():
    return jsonify({"download_dir": str(dl.DEFAULT_DIR), "ffmpeg": dl.HAS_FFMPEG})


@app.post("/api/info")
def info():
    url = str(body().get("url", "")).strip()
    if not valid_url(url):
        return error("Paste a full link that starts with https://")
    try:
        return jsonify(dl.fetch_info(url))
    except dl.DownloaderError as e:
        return error(str(e), 422)
    except Exception as e:
        app.logger.exception("Lookup failed")
        return error(dl.clean_error(e), 500)


@app.get("/api/downloads")
def list_downloads():
    with jobs_lock:
        items = sorted((public(j) for j in jobs.values()), key=lambda j: j["created"], reverse=True)
    return jsonify(items)


@app.post("/api/downloads")
def start_download():
    data = body()
    url = str(data.get("url", "")).strip()
    quality = str(data.get("quality", "best"))
    if not valid_url(url):
        return error("Invalid link")
    if not dl.is_valid_quality(quality):
        return error("Invalid quality")

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "quality": quality,
        "quality_label": dl.quality_label(quality),
        "playlist": bool(data.get("playlist")),
        "title": str(data.get("title") or url)[:300],
        "thumbnail": data.get("thumbnail") if isinstance(data.get("thumbnail"), str) else None,
        "count": data.get("count") if isinstance(data.get("count"), int) else None,
        "status": "queued",
        "percent": None,
        "created": time.time(),
        "_cancel": False,
    }
    with jobs_lock:
        jobs[job_id] = job
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return jsonify(public(job)), 201


@app.post("/api/downloads/<job_id>/cancel")
def cancel_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return error("Not found", 404)
        job["_cancel"] = True
        if job["status"] == "queued":
            job["status"] = "cancelled"
    return jsonify({"ok": True})


@app.post("/api/downloads/<job_id>/reveal")
def reveal_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        paths = list(job.get("_paths") or []) if job else []
    if not job:
        return error("Not found", 404)
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return error("The file is no longer there — it may have been moved or deleted.", 410)
    target = existing[0] if len(existing) == 1 else os.path.dirname(existing[0])
    reveal(target)
    return jsonify({"ok": True})


@app.post("/api/open-folder")
def open_folder():
    dl.DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    reveal(str(dl.DEFAULT_DIR))
    return jsonify({"ok": True})


@app.delete("/api/downloads")
def clear_finished():
    with jobs_lock:
        for job_id in [k for k, j in jobs.items() if j["status"] not in ACTIVE]:
            del jobs[job_id]
    return jsonify({"ok": True})


# --------------------------------------------------------------- startup


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit(f"No free port found between {start} and {start + 19}")


def main():
    parser = argparse.ArgumentParser(description="YouTube video downloader (web app)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Video Downloader is running at {url}")
    print(f"  Files are saved to {dl.DEFAULT_DIR}")
    if not dl.HAS_FFMPEG:
        print("  ! ffmpeg not found: video is limited to single-file formats and MP3 is unavailable.")
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()

    # Keep the terminal quiet: no per-request lines or "development server" banner.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *a, **k: None
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
