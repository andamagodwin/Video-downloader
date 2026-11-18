"""Core download logic built on yt-dlp (YouTube, Instagram, and other sites yt-dlp supports).

Used by the web app (app.py) and also runnable on its own as a command-line tool:

    python downloader.py "https://www.youtube.com/watch?v=..." -q 720
    python downloader.py "https://www.youtube.com/watch?v=..." -q mp3
    python downloader.py "https://www.instagram.com/reel/..." --cookies-from-browser chrome
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS
from yt_dlp.utils import DownloadCancelled, DownloadError

DEFAULT_DIR = Path(
    os.environ.get("DOWNLOAD_DIR") or Path.home() / "Downloads" / "Video Downloader"
).expanduser()

AUDIO_FORMATS = ("mp3", "m4a")
PLAYLIST_HEIGHTS = [1080, 720, 480, 360]
HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

# Codecs that QuickTime, Finder previews, iPhones and most TVs can play.
_PLAYABLE_VIDEO = {"h264", "hevc"}
_PLAYABLE_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3"}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

ProgressFn = Callable[[dict], None]
CancelFn = Callable[[], bool]


class DownloaderError(Exception):
    """An error with a message that is safe to show to the user."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def clean_error(err) -> str:
    """Turn a yt-dlp error into a short, readable message."""
    msg = _ANSI.sub("", str(err)).strip()
    msg = re.sub(r"^(ERROR|WARNING):\s*", "", msg)
    msg = re.sub(r"^\[[\w:]+\]\s*[\w-]+:\s*", "", msg)  # "[youtube] abc123: ..."
    msg = msg.split("; please report this issue")[0]
    return msg or "Something went wrong"


def is_valid_quality(quality: str) -> bool:
    return quality == "best" or quality in AUDIO_FORMATS or (quality.isdigit() and 100 <= int(quality) <= 4320)


def quality_label(quality: str) -> str:
    if quality in AUDIO_FORMATS:
        return f"{quality.upper()} audio"
    if quality == "best":
        return "Best quality"
    return f"{quality}p video"


# ---------------------------------------------------------------- sites and logins


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_youtube(url: str) -> bool:
    host = _host(url)
    return host in ("youtu.be", "youtube.com", "youtube-nocookie.com") or host.endswith(".youtube.com")


def site_name(url: str) -> str:
    host = _host(url)
    if is_youtube(url):
        return "YouTube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "Instagram"
    return host or "This site"


BROWSER_NAMES = {
    "chrome": "Chrome", "safari": "Safari", "firefox": "Firefox", "edge": "Edge", "brave": "Brave",
    "opera": "Opera", "vivaldi": "Vivaldi", "chromium": "Chromium", "whale": "Whale",
}
_MAC_APPS = {
    "chrome": "Google Chrome", "safari": "Safari", "firefox": "Firefox", "edge": "Microsoft Edge",
    "brave": "Brave Browser", "opera": "Opera", "vivaldi": "Vivaldi", "chromium": "Chromium", "whale": "Whale",
}


def available_browsers() -> list[dict]:
    """Browsers whose logins yt-dlp can use (on macOS, only the ones that are installed)."""
    found = []
    for key, name in BROWSER_NAMES.items():
        if key not in SUPPORTED_BROWSERS:
            continue
        if sys.platform == "darwin":
            app = f"{_MAC_APPS[key]}.app"
            if not any((d / app).exists() for d in (Path("/Applications"), Path.home() / "Applications")):
                continue
        elif key == "safari":
            continue
        found.append({"id": key, "name": name})
    return found


_LOGIN_HINTS = (
    "login required", "log in", "logged-in", "logged in", "empty media response", "rate-limit",
    "--cookies", "cookies-from-browser", "this content is unreachable", "private",
)


def explain_error(msg: str, url: str, cookies_browser: Optional[str]) -> DownloaderError:
    """Swap yt-dlp's command-line advice for something that makes sense in this app."""
    low = msg.lower()
    browser = BROWSER_NAMES.get(cookies_browser or "", cookies_browser)
    if "no video formats found" in low:
        return DownloaderError("There's no video here. Photos can't be downloaded, only videos.")
    if is_youtube(url) and "not a bot" in low:
        return DownloaderError(
            "YouTube wants to confirm you're not a bot. Wait a few minutes and try again, "
            "or restart the app so it can update."
        )
    if cookies_browser and ("operation not permitted" in low or "permission denied" in low):
        return DownloaderError(
            f"macOS blocked access to {browser}'s login data. Give your Terminal app Full Disk Access "
            "(System Settings → Privacy & Security → Full Disk Access) or choose another browser in Settings.",
            needs_login=True,
        )
    if cookies_browser and "could not find" in low and "cookies" in low:
        return DownloaderError(
            f"Couldn't find {browser}'s login data. Open {browser}, log in to the site, then try again "
            "— or choose another browser in Settings.",
            needs_login=True,
        )
    # Browser logins are never used for YouTube, so login advice only makes sense elsewhere.
    if not is_youtube(url) and any(hint in low for hint in _LOGIN_HINTS):
        site = site_name(url)
        if cookies_browser:
            return DownloaderError(
                f"{site} wouldn't share this, even with your {browser} login. Check that you're logged in to "
                f"{site} in {browser} and that the link opens there.",
                needs_login=True,
            )
        return DownloaderError(
            f"{site} only shows this to logged-in users, or is limiting logged-out downloads right now. "
            "Turn on “Use my browser’s login” in Settings, then try again.",
            needs_login=True,
        )
    return DownloaderError(msg)


class _Logger:
    """Collects yt-dlp's warnings and errors instead of printing them."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(clean_error(msg))

    def error(self, msg):
        self.errors.append(clean_error(msg))


def _base_opts(logger: _Logger, url: str, cookies_browser: Optional[str]) -> dict:
    opts = {
        "logger": logger,
        "quiet": True,
        "noprogress": True,
        # YouTube needs a JavaScript runtime to solve its player challenges.
        # Deno is preferred if installed, otherwise Node.js is used.
        "js_runtimes": {"deno": {}, "node": {}},
        # A watch link that also carries &list=... downloads only that video.
        "noplaylist": True,
    }
    # Use the browser's login (cookies) for sites like Instagram, but never for YouTube:
    # yt-dlp warns that using a Google account with it can get the account flagged.
    if cookies_browser and not is_youtube(url):
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


# Looking a video up takes several seconds (YouTube's player challenge has to be solved),
# so the result of fetch_info() is kept briefly and reused when the download starts.
_CACHE_TTL = 20 * 60
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_put(keys, info: dict):
    now = time.time()
    with _cache_lock:
        for url, (stamp, _) in list(_cache.items()):
            if now - stamp > _CACHE_TTL:
                del _cache[url]
        for key in keys:
            if key:
                _cache[key] = (now, info)


def _cache_get(url: str) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(url)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return copy.deepcopy(entry[1])  # yt-dlp modifies the dict while downloading
    return None


# ---------------------------------------------------------------- playable output


def _probe(path: str, timeout: float = 20) -> Optional[dict]:
    """Read the codecs, size and length of a video file with ffprobe."""
    if not HAS_FFPROBE:
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height:format=duration",
        "-of", "json", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    streams = data.get("streams") or []
    if out.returncode != 0 or not streams:
        return None
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        duration = float((data.get("format") or {}).get("duration") or 0) or None
    except ValueError:
        duration = None
    return {
        "vcodec": video.get("codec_name", "unknown") if video else "none",
        "acodec": audio.get("codec_name", "unknown") if audio else "none",
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "duration": duration,
    }


_encoder: Optional[str] = None


def _h264_encoder() -> Optional[str]:
    global _encoder
    if _encoder is None:
        try:
            listing = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            listing = ""
        _encoder = next((e for e in ("libx264", "h264_videotoolbox") if f" {e} " in listing), "")
    return _encoder or None


def make_playable(path: str, report: ProgressFn, should_cancel: Optional[CancelFn] = None) -> str:
    """Convert a video to H.264 + AAC if QuickTime can't play it (e.g. VP9 from Instagram or YouTube).

    The file is replaced in place. If anything goes wrong, the original is kept, since it still
    plays in VLC or IINA.
    """
    if not HAS_FFMPEG:
        return path
    info = _probe(path)
    if not info or info["vcodec"] == "none":
        return path
    fix_video = info["vcodec"] not in _PLAYABLE_VIDEO
    fix_audio = info["acodec"] not in _PLAYABLE_AUDIO and info["acodec"] != "none"
    if not (fix_video or fix_audio):
        return path
    sides = [s for s in (info["width"], info["height"]) if s]
    res = min(sides) if sides else 720
    if fix_video and res > 1080:
        return path  # 1440p and 4K only exist as VP9/AV1, and converting them would take a very long time

    if fix_video:
        encoder = _h264_encoder()
        if not encoder:
            return path
        if encoder == "libx264":
            video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
        else:
            rate = 2500 if res <= 480 else 5000 if res <= 720 else 8000
            video_args = ["-c:v", "h264_videotoolbox", "-b:v", f"{rate}k", "-pix_fmt", "yuv420p"]
    else:
        video_args = ["-c:v", "copy"]
    audio_args = ["-c:a", "aac", "-b:a", "192k"] if fix_audio else ["-c:a", "copy"]

    tmp = f"{os.path.splitext(path)[0]}.converting.mp4"
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", path,
        "-map", "0:v:0", "-map", "0:a?", *video_args, *audio_args,
        "-map_metadata", "0", "-movflags", "+faststart", "-progress", "pipe:1", tmp,
    ]
    step = "Converting so it plays everywhere"
    duration = info["duration"]
    report({"phase": "processing", "step": step, "percent": 0 if duration else None})

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            if should_cancel and should_cancel():
                raise DownloadCancelled()
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                report({"phase": "processing", "step": step, "percent": min(int(value) / 1e6 / duration * 100, 100)})
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    if proc.returncode != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        return path
    os.replace(tmp, path)
    return path


def _thumbnail(info: dict) -> Optional[str]:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    return thumbs[-1]["url"] if thumbs else None


def _resolution(fmt: dict) -> Optional[int]:
    """The "p" number of a format: its shorter side, so vertical videos read as 1080p, not 1920p."""
    sides = [s for s in (fmt.get("width"), fmt.get("height")) if s]
    return min(sides) if sides else None


def fetch_info(url: str, cookies_browser: Optional[str] = None) -> dict:
    """Look up a video or playlist (or an Instagram post with several videos) without downloading it."""
    logger = _Logger()
    opts = {**_base_opts(logger, url, cookies_browser), "skip_download": True, "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                info = ydl.sanitize_info(info)
    except DownloadError as e:
        raise explain_error(clean_error(e), url, cookies_browser) from None
    except Exception as e:  # e.g. the browser's login data couldn't be read
        raise explain_error(clean_error(e), url, cookies_browser) from None
    if not info:
        raise explain_error(logger.errors[-1] if logger.errors else "Could not read that link", url, cookies_browser)
    if info.get("_type") != "playlist":  # playlists are cheap to look up; their videos aren't fetched yet
        _cache_put((url, info.get("webpage_url")), info)

    site = site_name(url)
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries and not info.get("playlist_count"):
            raise explain_error("Nothing to download here: no videos found (it may be private)", url, cookies_browser)
        return {
            "type": "playlist",
            "site": site,
            "url": url,
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel"),
            "count": len(entries) or info.get("playlist_count"),
            "thumbnail": _thumbnail(info) or (entries and _thumbnail(entries[0])) or None,
            "heights": PLAYLIST_HEIGHTS,
        }

    heights = sorted(
        {
            r
            for f in info.get("formats") or []
            if f.get("vcodec") != "none" and (r := _resolution(f)) and r >= 144
        },
        reverse=True,
    )
    return {
        "type": "video",
        "site": site,
        "url": info.get("webpage_url") or url,
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "views": info.get("view_count"),
        "thumbnail": _thumbnail(info),
        "is_live": bool(info.get("is_live")),
        "heights": heights,
    }


def build_opts(
    quality: str, out_dir: Path, playlist: bool, logger: _Logger, url: str = "", cookies_browser: Optional[str] = None
) -> dict:
    opts = _base_opts(logger, url, cookies_browser)
    # Put the resolution in video names so a 720p copy doesn't count as "already downloaded"
    # when a 1080p copy of the same video exists.
    res_tag = f" ({quality}p)" if quality.isdigit() else ""
    name = f"%(title).150B{res_tag} [%(id)s].%(ext)s"
    if playlist:
        name = "%(playlist_title).100B/%(playlist_index)03d - " + name
        opts["noplaylist"] = False
        opts["ignoreerrors"] = True  # skip private/removed videos instead of stopping
    opts["outtmpl"] = {"default": str(out_dir / name)}
    opts["windowsfilenames"] = True  # avoid characters that break on other systems

    postprocessors = []
    if quality in AUDIO_FORMATS:
        if not HAS_FFMPEG:
            raise DownloaderError("ffmpeg is required to convert audio. Install it with: brew install ffmpeg")
        opts["format"] = "ba[ext=m4a]/ba/b" if quality == "m4a" else "ba/b"
        opts["writethumbnail"] = True
        postprocessors += [
            {"key": "FFmpegExtractAudio", "preferredcodec": quality, "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ]
    else:
        res = "res" if quality == "best" else f"res:{quality}"
        if HAS_FFMPEG:
            # Best video + best audio, merged into an MP4. Among equal resolutions, prefer
            # H.264 + AAC because it plays everywhere (QuickTime, phones, TVs).
            opts["format"] = "bv*+ba/b"
            opts["format_sort"] = [res, "vcodec:h264", "ext:mp4:m4a"]
            opts["merge_output_format"] = "mp4"
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        else:
            # Without ffmpeg, only single-file formats (usually up to 360p) can be used.
            opts["format"] = "b"
            opts["format_sort"] = [res, "ext:mp4"]
    opts["postprocessors"] = postprocessors
    return opts


_STEPS = {
    "Merger": "Merging video and audio",
    "ExtractAudio": "Converting audio",
    "FFmpegMetadata": "Adding metadata",
    "EmbedThumbnail": "Adding cover art",
    "MoveFiles": "Finishing up",
}


def download(
    url: str,
    quality: str = "best",
    out_dir: Path = DEFAULT_DIR,
    playlist: bool = False,
    on_progress: Optional[ProgressFn] = None,
    should_cancel: Optional[CancelFn] = None,
    cookies_browser: Optional[str] = None,
) -> list[str]:
    """Download a video (or every video in a playlist or post). Returns the saved file paths."""
    if not is_valid_quality(quality):
        raise DownloaderError(f"Unknown quality: {quality}")
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    report = on_progress or (lambda _: None)
    logger = _Logger()
    files: list[str] = []
    partials: set[str] = set()

    def progress_hook(d):
        if should_cancel and should_cancel():
            raise DownloadCancelled()
        info = d.get("info_dict") or {}
        if d.get("tmpfilename"):
            partials.add(d["tmpfilename"])
        if d["status"] != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        percent = done / total * 100 if total else None
        if percent is None and d.get("fragment_count"):
            percent = (d.get("fragment_index") or 0) / d["fragment_count"] * 100
        if info.get("vcodec") == "none":
            part = "audio"
        elif info.get("acodec") == "none":
            part = "video"
        else:
            part = "file"
        report({
            "phase": "downloading",
            "part": part,
            "percent": min(percent, 100) if percent is not None else None,
            "downloaded": done,
            "total": total,
            "speed": d.get("speed"),
            "eta": d.get("eta"),
            "index": info.get("playlist_index") if playlist else None,
            "count": (info.get("n_entries") or info.get("playlist_count")) if playlist else None,
            "item_title": info.get("title"),
        })

    def postprocessor_hook(d):
        name = d.get("postprocessor")
        if d["status"] == "started" and name in _STEPS:
            report({"phase": "processing", "step": _STEPS[name]})
        if d["status"] == "finished" and name == "MoveFiles":
            path = (d.get("info_dict") or {}).get("filepath")
            if path and path not in files:
                files.append(path)

    opts = build_opts(quality, out_dir, playlist, logger, url, cookies_browser)
    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [postprocessor_hook]

    cached = None if playlist else _cache_get(url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = None
            if cached:
                try:
                    result = ydl.process_ie_result(cached, download=True)
                except DownloadError:
                    result = None  # stale lookup (e.g. expired links): fall back to a fresh one
                    logger.errors.clear()
            if result is None:
                result = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        for p in partials:
            for leftover in (p, p + ".part", p + ".ytdl"):
                if os.path.isfile(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
        raise
    except DownloadError as e:
        raise explain_error(clean_error(e), url, cookies_browser) from None
    except Exception as e:  # e.g. the browser's login data couldn't be read
        raise explain_error(clean_error(e), url, cookies_browser) from None

    # Fall back to yt-dlp's own record of what it saved (e.g. files that already existed).
    entries = (result or {}).get("entries") if (result or {}).get("_type") == "playlist" else [result]
    for entry in entries or []:
        for rd in (entry or {}).get("requested_downloads") or []:
            path = rd.get("filepath")
            if path and path not in files and os.path.exists(path):
                files.append(path)

    files = [f for f in files if os.path.exists(f)]
    if not files:
        raise explain_error(logger.errors[-1] if logger.errors else "Nothing was downloaded", url, cookies_browser)
    if quality not in AUDIO_FORMATS:
        # Safety net: whatever the site sent, make sure the video plays in QuickTime.
        files = [make_playable(f, report, should_cancel) for f in files]
    return files


# ---------------------------------------------------------------- command line


def _fmt_bytes(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download YouTube or Instagram videos, or their audio.")
    parser.add_argument("url", help="video, playlist, reel or post link")
    parser.add_argument(
        "-q", "--quality", default="best",
        help="best (default), a resolution like 1080 / 720 / 480, or mp3 / m4a for audio only",
    )
    parser.add_argument("-o", "--output", default=str(DEFAULT_DIR), help=f"folder to save into (default: {DEFAULT_DIR})")
    parser.add_argument(
        "--cookies-from-browser", metavar="BROWSER", choices=sorted(SUPPORTED_BROWSERS),
        help="use your login from this browser for sites like Instagram (never used for YouTube)",
    )
    args = parser.parse_args(argv)
    browser = args.cookies_from_browser

    if not is_valid_quality(args.quality):
        parser.error("quality must be best, a number like 720, mp3 or m4a")

    try:
        print("Looking up link…")
        info = fetch_info(args.url, browser)
        if info["type"] == "playlist":
            print(f"{info['site']} {'post' if info['site'] == 'Instagram' else 'playlist'}: {info['title']} ({info['count']} videos)")
        else:
            print(f"{info['site']} video: {info['title']}")
        print(f"Format: {quality_label(args.quality)}  →  {args.output}")

        def show(p):
            if p["phase"] == "downloading":
                pct = p["percent"] or 0
                bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
                item = f"[{p['index']}/{p['count']}] " if p.get("index") else ""
                speed = f"{_fmt_bytes(p['speed'])}/s" if p.get("speed") else ""
                line = f"\r  {item}{p['part']:<5} {bar} {pct:5.1f}%  {_fmt_bytes(p['total'])}  {speed}"
                print(f"{line:<90}", end="", flush=True)
            else:
                pct = f" {p['percent']:.0f}%" if p.get("percent") is not None else ""
                print(f"\r  {p['step']}…{pct}{'':<60}", end="", flush=True)

        files = download(
            args.url, args.quality, Path(args.output), info["type"] == "playlist", show, cookies_browser=browser
        )
        print()
        for f in files:
            print(f"Saved: {f}")
        return 0
    except DownloaderError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
