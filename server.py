#!/usr/bin/env python3
"""
Local web server + live auto-sync API for Fight Night Countdown.
- Serves static site files (index.html, styles.css, app.js, data/events.json)
- GET  /api/events -> returns latest events.json
- POST /api/sync   -> triggers live publisher scrape (UFC, PFL, RAF, BKFC)
- Runs automatic background sync every SYNC_INTERVAL_MINUTES (default: 60)

Zero external dependencies (Python 3.9+ stdlib only).
"""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import time

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts"))
import sync_events  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))

_sync_lock = threading.Lock()


def perform_sync():
    with _sync_lock:
        payload, new_ids = sync_events.run_sync()
        return payload, new_ids


def background_sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL_MINUTES * 60)
        try:
            print(f"[auto-sync] Running scheduled publisher sync ({SYNC_INTERVAL_MINUTES}m interval)...")
            perform_sync()
        except Exception as exc:
            print(f"[auto-sync] Background sync error: {exc}", file=sys.stderr)


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT_DIR, **kwargs)

    def end_headers(self):
        # Always serve fresh event data and never stale-cache JSON/API responses
        if self.path.startswith("/data/") or self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/events":
            try:
                with open(sync_events.DATA_PATH, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                payload["liveSyncAvailable"] = True
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True, "liveSyncAvailable": True})
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/sync":
            try:
                payload, new_ids = perform_sync()
                payload["liveSyncAvailable"] = True
                payload["newEventIds"] = new_ids
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        self._send_json(404, {"error": "Not found"})


def main():
    # Start background auto-sync thread
    t = threading.Thread(target=background_sync_loop, daemon=True)
    t.start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), AppHandler)
    print(f"Fight Night Countdown running at http://localhost:{PORT}")
    print(f"Auto-sync enabled every {SYNC_INTERVAL_MINUTES} minutes (or via 'Sync Now' button).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
