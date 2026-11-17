# Video Downloader

A small app that runs on your computer for downloading **YouTube** and **Instagram** videos as MP4 video
or MP3/M4A audio. Paste a link, pick a quality, and the file is saved to `~/Downloads/Video Downloader`.

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
- **Instagram:** reels, video posts, and posts with several videos (saved together in one folder).
  Story highlights work too. See [Instagram](#instagram) below.
- **Downloads list:** shows progress, speed and time left. Up to 3 downloads run at once and the rest wait
  in a queue. Each finished download has a button to show it in Finder.
- Remembers the quality you picked last time.

## Instagram

Public reels and posts download without logging in. Instagram hides some content from logged-out visitors
(private accounts you follow, some stories), and sometimes blocks logged-out downloads for a while. For
those, open **Settings** and turn on **Use my browser's login**, choosing a browser where you're logged in
to Instagram.

- Your login (the browser's cookies) is read on your computer and only sent to the site you're downloading
  from. It is **never used for YouTube**, because yt-dlp warns that using a Google account with it can get
  that account flagged.
- **Chrome, Edge, Brave and similar browsers:** the first time, macOS asks for permission to use
  "Chrome Safe Storage" (or the equivalent for your browser). Enter your Mac password and choose
  **Always Allow**.
- **Safari:** macOS protects Safari's data, so your Terminal app needs **Full Disk Access**
  (System Settings → Privacy & Security → Full Disk Access).
- Photo-only posts can't be downloaded, only videos.

The setting is saved in `settings.json` in this folder, which is not committed to git.

### Bookmarklet (optional)

To open the video you're watching (on YouTube or Instagram) straight in the downloader, create a browser
bookmark with this as the URL:

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
.venv/bin/python downloader.py "https://www.instagram.com/reel/..."                # Instagram reel
.venv/bin/python downloader.py "https://www.instagram.com/p/..." --cookies-from-browser chrome
```

## Settings

| Environment variable | Default                       | Meaning                       |
| -------------------- | ----------------------------- | ----------------------------- |
| `DOWNLOAD_DIR`       | `~/Downloads/Video Downloader` | Where files are saved         |
| `PORT`               | `8765`                        | Port for the web app (`--port` works too) |

Example: `DOWNLOAD_DIR=~/Movies ./start.sh`

## Troubleshooting

- **"Confirm you're not a bot" or other YouTube errors:** restart the app so it updates yt-dlp.
  YouTube changes often, and fixes usually arrive within days.
- **"Instagram only shows this to logged-in users":** turn on **Use my browser's login** in Settings
  (see [Instagram](#instagram)). If it's already on, check you're logged in to Instagram in that browser.
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
Downloading may go against YouTube's and Instagram's Terms of Service.
