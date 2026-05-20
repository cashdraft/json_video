#!/usr/bin/env python3
"""Production-style entrypoint for systemd (no debug, no reloader)."""
import os
from pathlib import Path

from app import app

if __name__ == "__main__":
    import argparse

    pr = argparse.ArgumentParser(description="JSON Video production-style Flask server")
    pr.add_argument(
        "--cookies",
        metavar="FILE",
        help="cookies.txt (Netscape) → YT_COOKIES_PATH на время процесса (как у yt-dlp --cookies).",
    )
    args, _unknown = pr.parse_known_args()
    if args.cookies:
        os.environ["YT_COOKIES_PATH"] = str(Path(args.cookies).expanduser().resolve())
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
