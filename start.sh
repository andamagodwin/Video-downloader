#!/usr/bin/env bash
# Starts the Video Downloader web app. Sets everything up on the first run.
# Usage: ./start.sh            (opens your browser)
#        ./start.sh --no-browser --port 9000
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/ or with: brew install python"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (about a minute)…"
  python3 -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
fi

# YouTube changes often and yt-dlp releases fixes quickly, so stay up to date.
echo "Checking for updates…"
.venv/bin/python -m pip install -q --upgrade -r requirements.txt \
  || echo "Couldn't check for updates (offline?). Continuing with the installed version."

command -v ffmpeg >/dev/null 2>&1 \
  || echo "Note: ffmpeg isn't installed, so HD video and MP3 won't work. Install it with: brew install ffmpeg"
command -v node >/dev/null 2>&1 || command -v deno >/dev/null 2>&1 \
  || echo "Note: YouTube needs Node.js or Deno to be installed. Install one with: brew install node"

exec .venv/bin/python app.py "$@"
