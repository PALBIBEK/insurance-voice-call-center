"""Standalone static server for the web console.

The UI is a separate client process from the backend microservice: it serves
only static files and talks to the backend exclusively over its REST/WS API
(cross-origin, allowed by the backend's CORS config).

Usage:
    python serve.py              # http://localhost:3000, backend assumed at :8000
    python serve.py --port 3000
"""

import argparse
import functools
import http.server
import pathlib


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the web console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(pathlib.Path(__file__).parent),
    )
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Web console on http://localhost:{args.port} (expects backend API on :8000)")
    server.serve_forever()


if __name__ == "__main__":
    main()
