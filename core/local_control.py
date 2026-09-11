"""
core/local_control.py — a loopback-only control channel for local companion
processes (currently: the Arc Sentinel widget).

WHY NO AUTH
    This binds to 127.0.0.1 exclusively — nothing outside this machine can
    ever reach it (unlike dashboard/server.py, which listens on the LAN and
    therefore needs the token/AES scheme it has). Anything already running
    locally on this machine already has full access to its microphone and
    filesystem, so a token here would protect against nothing real while
    adding a pairing step the widget has no UI to complete.

WHY NOT FASTAPI
    dashboard/server.py's fastapi+uvicorn stack is an optional dependency —
    this control channel needs to exist whether or not the user ever installs
    the dashboard. http.server is stdlib, so mic mute / interrupt work
    regardless.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

PORT = 8765


class LocalControlServer:
    """Started once by JarvisLive.run(). get_state/on_mute_toggle/on_interrupt
    are plain callables — this class only owns the HTTP plumbing."""

    def __init__(
        self,
        get_state: Callable[[], dict],
        on_mute_toggle: Callable[[], None],
        on_interrupt: Callable[[], None],
    ):
        self._get_state     = get_state
        self._on_mute_toggle = on_mute_toggle
        self._on_interrupt   = on_interrupt
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> bool:
        """Binds and starts serving in a daemon thread. Returns False (never
        raises) if the port is already taken — e.g. a second JARVIS instance
        is running; the first instance's control channel keeps working."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, code: int = 200) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def do_GET(self) -> None:
                if self.path == "/status":
                    try:
                        self._json(outer._get_state())
                    except Exception as e:
                        self._json({"error": str(e)}, 500)
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                if self.path == "/mute":
                    try:
                        outer._on_mute_toggle()
                    except Exception:
                        pass
                    self._json(outer._get_state())
                elif self.path == "/interrupt":
                    try:
                        outer._on_interrupt()
                    except Exception:
                        pass
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def log_message(self, *_args) -> None:
                pass   # keep JARVIS's console output clean

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:
            return False
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                          name="LocalControlServer").start()
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
