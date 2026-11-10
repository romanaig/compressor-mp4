#!/usr/bin/env python3
"""
Compressor MP4 — local server (your PC only, 127.0.0.1).

Opens the UI in the browser. Dropped videos go to ../input/, results to ../output/.
Uses the system ffmpeg or the one from the imageio-ffmpeg package.

Usage: double-click OPEN_COMPRESSOR.bat, or
  python app/server.py [--port 8765] [--no-browser]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compression as cz                                                  # noqa: E402
from ffmpeg_engine import Ffmpeg, FfmpegError, FfmpegNotFound, find_ffmpeg  # noqa: E402
from files import VIDEO_EXTENSIONS, Folders, free_name, safe_name        # noqa: E402
from operations import Compressor                                        # noqa: E402

VERSION = "2.0.0"
WEB = Path(__file__).resolve().parent / "web"
CHUNK = 1024 * 1024
JSON_LIMIT = 100_000
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
# the browser only needs to know it is video; H.264 .mov plays as mp4 in Chrome/Edge
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm",
               ".mkv": "video/webm", ".avi": "video/x-msvideo"}
QUIET_ROUTES = {"/api/render", "/api/params", "/api/frame", "/api/video"}   # polling and timeline scrubbing


class Handler(BaseHTTPRequestHandler):
    server_version = f"CompressorMP4/{VERSION}"

    @property
    def app(self) -> Compressor:
        return self.server.compressor

    # ---------- console: actions and errors only ----------

    def log_message(self, fmt, *args):
        path = urlparse(self.path).path
        status = str(args[1]) if len(args) > 1 else ""
        if path in QUIET_ROUTES and not status.startswith(("4", "5")):
            return
        sys.stderr.write("  " + (fmt % args) + "\n")

    # ---------- responses ----------

    def _send(self, status: int, content_type: str, data: bytes, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status: int = 200):
        self._send(status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode())

    def _error(self, status: int, msg: str):
        self._json({"error": msg}, status)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > JSON_LIMIT:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- security: only the app's own page may talk to the server ----------

    def _trusted_origin(self) -> bool:
        """Blocks DNS rebinding (odd Host) and other websites in the browser calling the API (odd Origin)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        origin = self.headers.get("Origin")
        return host in LOCAL_HOSTS and (not origin or urlparse(origin).hostname in LOCAL_HOSTS)

    # ---------- dispatch ----------

    def do_GET(self):
        self._dispatch(GET_ROUTES, static=True)

    def do_POST(self):
        self._dispatch(POST_ROUTES, static=False)

    def _dispatch(self, routes: dict, static: bool):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if not self._trusted_origin():
                return self._error(403, "only the app's own page may use this server")
            action = routes.get(url.path)
            if action:
                action(self, query)
            elif static:
                self._static_file(url.path)
            else:
                self._error(404, "not found")
        except FileNotFoundError:
            self._error(404, "video not found in input/")
        except (ValueError, TypeError) as e:          # includes json.JSONDecodeError
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                      # browser cancelled (normal while playing/scrubbing video)
        except FfmpegError as e:
            self._error(500, str(e))
        except Exception as e:                        # unexpected: show it in the UI instead of crashing the server
            self._error(500, f"{type(e).__name__}: {e}")

    # ---------- GET ----------

    def _static_file(self, path: str):
        relative = "index.html" if path in ("/", "/index.html") else unquote(path).lstrip("/")
        file = (WEB / relative).resolve()
        if not file.is_relative_to(WEB) or file.suffix not in STATIC_TYPES or not file.is_file():
            return self._error(404, "not found")
        self._send(200, STATIC_TYPES[file.suffix], file.read_bytes())

    def _config(self, _q):
        self._json({"version": VERSION, "ffmpeg": self.server.ffmpeg_version, "extensions": list(VIDEO_EXTENSIONS),
                    "caps": list(cz.RESOLUTION_CAPS), "height_steps": [list(s) for s in sorted(cz.HEIGHT_STEPS)],
                    "min_fps": cz.MIN_FPS, "formats": {k: {"label": f.label, "extension": f.extension}
                                                       for k, f in cz.FORMATS.items()}})

    def _info(self, q):
        self._json(self.app.info(q.get("v", "")))

    def _params(self, q):
        self._json(self.app.params(q.get("v", ""), cz.Settings.from_request(q)).to_dict())

    def _frame(self, q):
        png = self.app.frame(q.get("v", ""), float(q.get("t", 0) or 0))
        self._send(200, "image/png", png)

    def _render_state(self, q):
        j = self.app.render_state(q.get("job", ""))
        if not j:
            return self._error(404, "render not found")
        self._json(j)

    def _video(self, q):
        """The original video for the ▶ button on the Raw screen. Supports Range (the browser seeks while playing)."""
        path = self.app.path(q.get("v", ""))
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        byte_range = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if byte_range and (byte_range[1] or byte_range[2]):
            if byte_range[1]:
                start, end = int(byte_range[1]), min(int(byte_range[2]) if byte_range[2] else size - 1, size - 1)
            else:                                     # "bytes=-500": the last 500
                start = max(0, size - int(byte_range[2]))
            if start > end:
                return self._send(416, "text/plain", b"", {"Content-Range": f"bytes */{size}"})
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", VIDEO_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---------- POST ----------

    def _preview(self, _q):
        body = self._json_body()
        self._json(self.app.preview(str(body.get("v", "")), float(body.get("t", 0) or 0), cz.Settings.from_request(body)))

    def _estimate(self, _q):
        body = self._json_body()
        self._json(self.app.estimate(str(body.get("v", "")), cz.Settings.from_request(body)))

    def _render(self, _q):
        body = self._json_body()
        self._json({"job": self.app.start_render(str(body.get("v", "")), cz.Settings.from_request(body))})

    def _open_output(self, _q):
        folder = self.app.folders.output
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)                      # Windows Explorer
        self._json({"folder": str(folder)})

    def _upload(self, _q):
        """Receives the dropped file and copies it to input/ (the browser does not expose the original path)."""
        n = int(self.headers.get("Content-Length") or 0)
        try:
            name = safe_name(unquote(self.headers.get("X-Filename", "")))
        except ValueError as e:
            self._discard(n)
            return self._error(400, str(e))
        if n <= 0:
            return self._error(400, "empty file")
        folder = self.app.folders.input
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if target.exists() and target.stat().st_size == n:
            self._discard(n)                          # the same file is already in input/
        else:
            target = free_name(target)
            self._save(target, n)
        try:
            info = self.app.info_for(target)
        except ValueError as e:
            return self._error(400, f"{target.name}: {e}")
        self._json({"name": target.name, **info})

    def _save(self, target: Path, n: int):
        partial = target.with_name(target.name + ".partial")
        remaining = n
        try:
            with partial.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(partial, target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _discard(self, n: int):
        while n > 0:
            chunk = self.rfile.read(min(CHUNK, n))
            if not chunk:
                break
            n -= len(chunk)


GET_ROUTES = {"/api/config": Handler._config, "/api/info": Handler._info, "/api/params": Handler._params,
              "/api/frame": Handler._frame, "/api/render": Handler._render_state, "/api/video": Handler._video}
POST_ROUTES = {"/api/upload": Handler._upload, "/api/preview": Handler._preview, "/api/estimate": Handler._estimate,
               "/api/render": Handler._render, "/api/open_output": Handler._open_output}


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets 2 processes share a port: disable it so a really free port is found.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True
    compressor: Compressor
    ffmpeg_version: str


def start(port: int = 8765, host: str = "127.0.0.1", folders: Folders | None = None,
          ffmpeg_exe: str | None = None) -> Server:
    """Creates the server on the first free port from `port` (does not serve yet: call serve_forever)."""
    folders = folders or Folders.default()
    folders.create()
    folders.clean_tmp()
    ffmpeg = Ffmpeg(ffmpeg_exe or find_ffmpeg())
    for p in range(port, port + 20):
        try:
            srv = Server((host, p), Handler)
        except OSError:
            continue
        srv.compressor = Compressor(ffmpeg, folders)
        srv.ffmpeg_version = ffmpeg.version()
        return srv
    raise OSError(f"no free port between {port} and {port + 19}")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    try:
        srv = start(a.port)
    except (FfmpegNotFound, OSError) as e:
        sys.exit(str(e))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Compressor MP4 {VERSION} running at {url}")
    print(f"ffmpeg {srv.ffmpeg_version}: {srv.compressor.ff.exe}")
    print("Close this window to stop.")
    if not a.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
