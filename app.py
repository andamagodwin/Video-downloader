"""Local web app for downloading YouTube and Instagram videos.

Run:  python app.py            (opens http://127.0.0.1:8765 in your browser)
      python app.py --no-browser --port 9000
"""

from __future__ import annotations

import argparse
import json
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
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from yt_dlp.utils import DownloadCancelled

import downloader as dl

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
MAX_PARALLEL = 3

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_PARALLEL)
ACTIVE = {"queued", "starting", "downloading", "processing"}


# ----------------------------------------------------------------- helpers


PROJECT_URL = "https://github.com/andamagodwin/Video-downloader"

# Shown when the app is reached at any address other than this computer, e.g. if it has been
# deployed to a host like Vercel. Self-contained, because /static is blocked there too.
NOT_LOCAL_PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Video Downloader runs on your computer</title>
<style>
  :root {{ --bg:#f5f5f2; --surface:#fff; --text:#18181b; --muted:#6b6b74; --border:#e3e3dd; --code:#f0f0eb; --accent:#e11d48; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f0f11; --surface:#18181c; --text:#ededf0; --muted:#9c9ca6; --border:#2c2c33; --code:#232329; --accent:#fb3f64; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:620px; margin:0 auto; padding:64px 16px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:28px; }}
  .logo {{ width:46px; height:46px; border-radius:13px; background:var(--accent); color:#fff;
          display:grid; place-items:center; margin-bottom:18px; }}
  h1 {{ margin:0 0 8px; font-size:22px; line-height:1.3; }}
  p {{ margin:0 0 14px; }}
  .muted {{ color:var(--muted); }}
  ul {{ margin:0 0 18px; padding-left:20px; color:var(--muted); }}
  li {{ margin-bottom:4px; }}
  pre {{ margin:0 0 14px; padding:14px 16px; border-radius:10px; background:var(--code);
        font:13px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-x:auto; }}
  a {{ color:var(--accent); font-weight:600; }}
</style>
</head>
<body>
<main>
  <div class="card">
    <div class="logo" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="24" height="24"><path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M6 19.5h12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <h1>Video Downloader runs on your own computer</h1>
    <p class="muted">It isn't a website, so it can't be used from a hosted address like this one.
      It saves videos to the computer it runs on, which a web host can't do:</p>
    <ul>
      <li>Hosts like Vercel stop the app as soon as a page has loaded, so downloads would be cut off.</li>
      <li>Files would be saved on the host's server instead of your computer.</li>
      <li>The tools it needs for HD video, MP3 and YouTube (ffmpeg and Node.js) aren't available there.</li>
      <li>YouTube and Instagram block downloads from cloud servers.</li>
    </ul>
    <p><strong>To use it</strong>, download it and run it on your Mac or Linux computer:</p>
    <pre>git clone {PROJECT_URL}.git
cd Video-downloader
./start.sh</pre>
    <p class="muted">It opens in your browser at http://127.0.0.1:8765.</p>
    <p><a href="{PROJECT_URL}">View the project and instructions on GitHub →</a></p>
  </div>
</main>
</body>
</html>
"""


def not_local_response():
    message = f"Video Downloader only runs on your own computer. See {PROJECT_URL}"
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 403
    return Response(NOT_LOCAL_PAGE, status=403, mimetype="text/html")


@app.before_request
def only_local():
    # The server only listens on 127.0.0.1, so any other Host header means it was either
    # deployed somewhere (e.g. Vercel) or is a DNS-rebinding attempt. Either way, refuse,
    # but explain why in case a person is reading.
    host = (request.host or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return not_local_response()
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


def load_settings() -> dict:
    settings = {"cookies_browser": None}
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        if isinstance(saved, dict):
            settings.update({k: v for k, v in saved.items() if k in settings})
    except (OSError, ValueError):
        pass
    return settings


settings = load_settings()


def cookies_browser() -> str | None:
    browser = settings.get("cookies_browser")
    return browser if browser in {b["id"] for b in dl.available_browsers()} else None


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
                cookies_browser=cookies_browser(),
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
            update(job_id, status="error", error=str(e), needs_login=e.needs_login, finished=time.time())
        except Exception as e:  # keep the server alive no matter what
            app.logger.exception("Download failed")
            update(job_id, status="error", error=dl.clean_error(e), finished=time.time())


# --------------------------------------------------------------- routes


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.get("/api/config")
def config():
    return jsonify({
        "download_dir": str(dl.DEFAULT_DIR),
        "ffmpeg": dl.HAS_FFMPEG,
        "platform": sys.platform,
        "browsers": dl.available_browsers(),
        "cookies_browser": cookies_browser(),
    })


@app.post("/api/settings")
def save_settings():
    browser = body().get("cookies_browser") or None
    if browser is not None and browser not in {b["id"] for b in dl.available_browsers()}:
        return error("That browser isn't available")
    settings["cookies_browser"] = browser
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError as e:
        return error(f"Couldn't save settings: {e}", 500)
    return jsonify({"cookies_browser": browser})


@app.post("/api/info")
def info():
    url = str(body().get("url", "")).strip()
    if not valid_url(url):
        return error("Paste a full link that starts with https://")
    try:
        return jsonify(dl.fetch_info(url, cookies_browser()))
    except dl.DownloaderError as e:
        return jsonify({"error": str(e), "needs_login": e.needs_login}), 422
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
    parser = argparse.ArgumentParser(description="YouTube and Instagram video downloader (web app)")
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
