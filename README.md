# Video Downloader

A small app that runs on your computer for downloading YouTube videos as MP4 video or MP3/M4A audio.
Paste a link, pick a quality, and the file is saved to `~/Downloads/Video Downloader`.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), with a Flask server and a plain HTML/JS page.

## Start it

```bash
./start.sh
```

Or double-click **Start Video Downloader.command** in Finder.

The first run creates a Python virtual environment and installs everything. After that, each start
updates yt-dlp, then opens http://127.0.0.1:8765 in your browser. Press `Ctrl+C` (or close the Terminal
window) to stop.

### Requirements

- **Python 3.9+**
- **ffmpeg**, needed to merge HD video with its audio and to make MP3s: `brew install ffmpeg`
- **Node.js** or **Deno**, which YouTube now requires for solving its player challenges: `brew install node`

## What it does

- **Video:** every resolution the video offers, from 144p up to 4K, saved as MP4. At 1080p and below
  the video is H.264 with AAC audio, so it plays everywhere, including QuickTime, iPhone and TVs.
  1440p and 4K are only available as VP9/AV1, which may need VLC or IINA to play on older Macs.
- **Audio only:** MP3 (192 kbps) or M4A, with title, artist and cover art embedded.
- **Playlists:** paste a playlist link to download every video into its own folder, numbered in order.
  Private or removed videos are skipped.
- **Downloads list:** shows progress, speed and time left. Up to 3 downloads run at once and the rest wait
  in a queue. Each finished download has a button to show it in Finder.
- Remembers the quality you picked last time.

### Bookmarklet (optional)

To open the video you're watching straight in the downloader, create a browser bookmark with this as the URL:

```
javascript:location='http://127.0.0.1:8765/?url='+encodeURIComponent(location.href)
```

## Command line

The same downloader works without the browser:

```bash
.venv/bin/python downloader.py "https://www.youtube.com/watch?v=..."            # best quality
.venv/bin/python downloader.py "https://www.youtube.com/watch?v=..." -q 720     # 720p
.venv/bin/python downloader.py "https://www.youtube.com/watch?v=..." -q mp3     # audio
.venv/bin/python downloader.py "https://www.youtube.com/watch?v=..." -o ~/Movies
```

## Settings

| Environment variable | Default                       | Meaning                       |
| -------------------- | ----------------------------- | ----------------------------- |
| `DOWNLOAD_DIR`       | `~/Downloads/Video Downloader` | Where files are saved         |
| `PORT`               | `8765`                        | Port for the web app (`--port` works too) |

Example: `DOWNLOAD_DIR=~/Movies ./start.sh`

## Troubleshooting

- **"Sign in to confirm you're not a bot" or other YouTube errors:** restart the app so it updates yt-dlp.
  YouTube changes often, and fixes usually arrive within days.
- **MP3 or HD options are missing:** install ffmpeg (`brew install ffmpeg`) and restart.
- **Port already in use:** the app automatically tries the next free port. Check the address printed in the Terminal.

## Files

| File                       | What it is                                               |
| -------------------------- | -------------------------------------------------------- |
| `downloader.py`            | Download logic (yt-dlp options, progress, CLI)           |
| `app.py`                   | Local web server (API + download queue)                  |
| `static/`                  | The web page (`index.html`, `style.css`, `app.js`)       |
| `start.sh`                 | Setup and launch script                                  |

The server only listens on `127.0.0.1`, so other devices on your network can't reach it. It also rejects
requests that come from other websites.

---

Only download videos you own, that are freely licensed, or that you otherwise have permission to save.
Downloading may go against YouTube's Terms of Service.
