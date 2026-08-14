"""
Locker control proxy (Смысл 1) — zero dependencies, stdlib only.

Run:  py server.py            # DRY_RUN on (safe, default)
      DRY_RUN=0 py server.py  # LIVE — open commands actually sent

Sits on a separate machine, talks to the terminal's open web API over LAN.
Writes NOTHING to the terminal except cell-open commands, and even those are
suppressed while DRY_RUN is on (the default).

Safety:
  - DRY_RUN defaults ON. Open commands are logged, not sent, until DRY_RUN=0.
  - Only two upstream endpoints are ever called: GET /GetBoxList, POST /OpenBox.
    Dangerous routes (/AllOpenBox, /uploadApk, /RestartShutdown, biometrics,
    port 14035) are deliberately not wired in.
  - Biometric fields are stripped before cells leave this server.
  - Upstream calls bypass any system proxy (LAN target).
"""
import os
import json
import time
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

TERMINAL = os.environ.get("TERMINAL_URL", "http://192.168.11.242:8088")
DRY_RUN = os.environ.get("DRY_RUN", "1").strip().lower() not in ("0", "false", "no", "off")
TIMEOUT = float(os.environ.get("TERMINAL_TIMEOUT", "8"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
CELL_MIN, CELL_MAX = 1, 43
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("locker")

# Never route LAN calls through a corporate proxy.
_opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))


def _upstream(path, method="GET", body=None, retries=5):
    """Call the terminal API. AndServer resets ~30% of connections at random,
    so retry a few times. A User-Agent header materially cuts the reset rate."""
    url = TERMINAL + path
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urlrequest.Request(url, data=data, method=method)
            req.add_header("User-Agent", "LockerProxy/1.0")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            with _opener.open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except (ConnectionResetError, URLError, TimeoutError) as e:
            last = e
            log.warning("upstream %s %s attempt %d/%d failed: %s", method, path, attempt, retries, e)
            time.sleep(0.25)
    raise last


def _clean_cell(raw):
    """Normalize one cell from /GetBoxList; drop biometric fields."""
    try:
        addr = json.loads(raw.get("lockAddress", "{}"))
    except (ValueError, TypeError):
        addr = {}
    return {
        "boxNumber": raw.get("boxNumber"),
        "address": addr.get("address"),
        "number": addr.get("number"),
        "state": raw.get("state"),      # 0 free, 1 occupied, 2 special
        "type": raw.get("type"),        # 0/1/2 size
        "lock": raw.get("lock"),
        "inventory": raw.get("inventory"),
        "occupied": bool(raw.get("personidArray") and raw.get("personidArray") != "[]"),
    }


def get_cells():
    payload = _upstream("/GetBoxList")
    if payload.get("code") != 1:
        raise RuntimeError("terminal returned error code")
    cells = [_clean_cell(c) for c in payload.get("data", [])]
    cells.sort(key=lambda c: (c["boxNumber"] is None, c["boxNumber"]))
    return cells


def open_cell(n):
    if DRY_RUN:
        log.info("DRY_RUN: would open cell %d (not sent)", n)
        return {"cell": n, "sent": False, "dryRun": True}
    log.warning("OPENING cell %d (live)", n)
    resp = _upstream("/OpenBox", method="POST", body=[n])
    return {"cell": n, "sent": True, "dryRun": False, "response": resp}


class Handler(BaseHTTPRequestHandler):
    server_version = "LockerProxy/1.0"

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8" if full.endswith(".html") else "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/health":
            try:
                cells = get_cells()
                return self._json({"terminal": TERMINAL, "reachable": True, "dryRun": DRY_RUN, "count": len(cells)})
            except (OSError, RuntimeError, ValueError) as e:
                return self._json({"terminal": TERMINAL, "reachable": False, "error": str(e)}, 502)
        if self.path == "/api/cells":
            try:
                cells = get_cells()
                return self._json({"dryRun": DRY_RUN, "count": len(cells), "cells": cells})
            except (OSError, RuntimeError, ValueError) as e:
                return self._json({"error": f"terminal unreachable: {e}"}, 502)
        return self._static(self.path)

    def do_POST(self):
        if self.path.startswith("/api/open/"):
            try:
                n = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                return self._json({"error": "bad cell number"}, 400)
            if not (CELL_MIN <= n <= CELL_MAX):
                return self._json({"error": f"cell must be {CELL_MIN}..{CELL_MAX}"}, 400)
            try:
                return self._json(open_cell(n))
            except (OSError, RuntimeError, ValueError) as e:
                return self._json({"error": f"open failed: {e}"}, 502)
        return self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    mode = "DRY_RUN (safe)" if DRY_RUN else "LIVE — opens for real"
    log.info("Locker proxy on http://%s:%d  ->  %s  [%s]", HOST, PORT, TERMINAL, mode)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
