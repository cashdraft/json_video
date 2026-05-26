#!/usr/bin/env python3
"""Local dev server for Scenes Stock standalone bundle."""

from dotenv import load_dotenv

load_dotenv()

from app import app  # noqa: E402

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Scenes Stock standalone")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
