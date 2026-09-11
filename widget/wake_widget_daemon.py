"""
wake_widget_daemon.py — voice-triggered Arc Sentinel widget.

A standalone companion process, separate from main.py on purpose: it opens
its OWN microphone stream and runs its OWN small always-on-top window, so it
can be started/stopped independently of whichever engine (Cloud or Local)
JARVIS itself is running, and a crash here can never take the assistant down.

Say "Hey Jarvis"  -> the widget appears (pretrained openwakeword model, the
                      exact same one core/wake_word.py uses for the main
                      app's own wake gate).
Say "bye jarvis"  -> the widget disappears. There is no pretrained model for
                      this phrase, so it's spotted by transcribing short
                      rolling audio windows with faster-whisper (already a
                      dependency of core/stt.py) and checking for the words
                      "bye" and "jarvis" together. This only runs WHILE the
                      widget is visible, to keep it cheap the rest of the time.

Requires (not part of the main app's requirements.txt — install by hand):
    pip install pywebview openwakeword faster-whisper

Run it:
    pythonw widget/wake_widget_daemon.py     (no console window)
    python  widget/wake_widget_daemon.py     (console, for troubleshooting)

Or launch it from the JARVIS phone dashboard's new "WIDGET" button, which
starts this exact script as a detached background process on the machine
running JARVIS.
"""
from __future__ import annotations

import collections
import os
import sys
import threading
import time
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
WIDGET_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))   # so `core.*` imports work run from anywhere

SAMPLE_RATE   = 16000
CHUNK_SIZE    = 1024
BYE_WINDOW_S  = 3.0     # seconds of rolling audio checked for "bye jarvis"
BYE_POLL_S    = 1.4     # how often that window is transcribed while visible
PID_FILE      = WIDGET_DIR / ".wake_widget.pid"
DEFAULT_ACCENT = "#00d4ff"   # matches ui.py's DEFAULT_UI_COLOR (unthemed default)


def _get_accent_hex() -> str:
    """Read the user's chosen HUD accent colour (⚙ → CUSTOMISE ASSISTANT,
    stored as config/api_keys.json's "ui_color") so the widget matches
    whatever theme is live in the main window instead of a fixed blue. A
    plain read-only json parse — no config_manager caching needed since this
    runs once at daemon startup, not on a hot path."""
    try:
        import json
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        hex_ = (cfg.get("ui_color") or "").strip().lower()
        if hex_.startswith("#") and len(hex_) == 7:
            int(hex_[1:], 16)   # validates it's actually hex
            return hex_
    except Exception:
        pass
    return DEFAULT_ACCENT


def _check_singleton() -> bool:
    """Refuse to start a second daemon — two of these would fight over the
    microphone and pop two widgets. Returns True if it's safe to proceed."""
    try:
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text().strip())
            if _pid_alive(old_pid):
                print(f"[Widget] Already running (pid {old_pid}). Not starting a second copy.")
                return False
    except Exception:
        pass
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _check_deps() -> bool:
    missing = []
    try:
        import webview  # noqa: F401
    except ImportError:
        missing.append("pywebview")
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        missing.append("openwakeword")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        missing.append("faster-whisper")
    try:
        import sounddevice  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        missing.append("sounddevice numpy")
    if missing:
        print("[Widget] Missing dependencies:", ", ".join(missing))
        print(f"[Widget] Install with:  {sys.executable} -m pip install " + " ".join(missing))
        return False

    from core.wake_word import is_ready
    if not is_ready():
        print("[Widget] The 'Hey Jarvis' wake model isn't downloaded yet.")
        print("[Widget] Open JARVIS -> Settings -> WAKE WORD -> download once (shared with this widget).")
        return False
    return True


class WidgetController:
    """Owns the pywebview window and the two listening loops. Everything that
    touches audio runs off pywebview's own thread — window.show()/hide() are
    the only pywebview calls made from other threads, and both are
    documented as thread-safe."""

    def __init__(self):
        self.window   = None
        self.visible  = False
        self._ring    = collections.deque(maxlen=int(SAMPLE_RATE * BYE_WINDOW_S / CHUNK_SIZE) + 2)
        self._lock    = threading.Lock()
        self._whisper = None   # lazy — only loaded once "hey jarvis" actually fires
        self._bye_stop_evt = threading.Event()

    # ── wake / bye transitions ────────────────────────────────────────────

    def on_hey_jarvis(self) -> None:
        if self.visible or self.window is None:
            return
        print("[Widget] 'Hey Jarvis' detected — showing widget.")
        self.visible = True
        self._bye_stop_evt.clear()
        with self._lock:
            # Drop whatever's left from the previous session — it may still
            # contain the "bye jarvis" that just hid the widget, which would
            # otherwise get transcribed again the moment listening resumes
            # and immediately re-hide the widget it was meant to bring back.
            self._ring.clear()
        try:
            self.window.show()
        except Exception as e:
            print(f"[Widget] show() failed: {e}")
        threading.Thread(target=self._bye_listener_loop, daemon=True).start()

    def on_bye_jarvis(self) -> None:
        if not self.visible or self.window is None:
            return
        print("[Widget] 'Bye Jarvis' detected — hiding widget.")
        self.visible = False
        self._bye_stop_evt.set()
        try:
            self.window.hide()
        except Exception as e:
            print(f"[Widget] hide() failed: {e}")

    # ── mic feed (called from the sounddevice callback thread) ───────────

    def feed_audio(self, frame) -> None:
        with self._lock:
            self._ring.append(frame.copy())

    def _snapshot_audio(self):
        import numpy as np
        with self._lock:
            if not self._ring:
                return None
            chunks = list(self._ring)
        return np.concatenate(chunks).flatten()

    def _bye_listener_loop(self) -> None:
        """Runs only while the widget is visible. Polls the rolling audio
        buffer every BYE_POLL_S seconds and transcribes it — cheap relative
        to running Whisper continuously, since it's gated to the one window
        where the phrase actually matters."""
        if self._whisper is None:
            from core.stt import WhisperSTT
            print("[Widget] Loading local speech model for 'bye jarvis' (one-time)…")
            try:
                self._whisper = WhisperSTT(model_name="tiny", language="en")
            except Exception as e:
                print(f"[Widget] Could not load Whisper — 'bye jarvis' won't work "
                      f"this run (use the ✕ button instead): {e}")
                return

        while not self._bye_stop_evt.wait(BYE_POLL_S):
            audio = self._snapshot_audio()
            if audio is None or audio.size < SAMPLE_RATE:
                continue
            try:
                float_audio = audio.astype("float32") / 32768.0
                text = self._whisper.transcribe(float_audio).lower()
            except Exception as e:
                print(f"[Widget] Transcription error: {e}")
                continue
            if "bye" in text and "jarvis" in text:
                self.on_bye_jarvis()
                return


def _js_api(controller: WidgetController):
    class Api:
        def say_bye(self):
            controller.on_bye_jarvis()
    return Api()


def main() -> None:
    if not _check_deps():
        sys.exit(1)
    if not _check_singleton():
        sys.exit(0)

    import atexit
    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))

    import webview
    import sounddevice as sd
    from core.wake_word import WakeWordDetector
    from core import audio_devices
    from memory.config_manager import get_input_device

    # Reuse the SAME device-selection JARVIS itself uses (core/audio_devices.py
    # — the module that measures which host API a device actually opens
    # cleanly at, rather than guessing) and the SAME saved device name from
    # ⚙ → AUDIO DEVICES, instead of letting sounddevice grab whatever it calls
    # "default". Two processes reasoning about "the mic" differently is a
    # likelier source of conflicts than two processes sharing one mic in
    # WASAPI shared mode ever is. Kicked off now, in the background, so the
    # cache is warm well before start_backend() needs it (wake-model loading
    # alone already takes several seconds).
    audio_devices.configure(SAMPLE_RATE, SAMPLE_RATE)
    audio_devices.prefetch()

    controller = WidgetController()

    def on_wake_detected():
        controller.on_hey_jarvis()

    detector = WakeWordDetector(on_detect=on_wake_detected, logger=lambda m: print(f"[Widget] {m}"))

    def mic_callback(indata, frames, time_info, status):
        detector.feed(indata)
        if controller.visible:
            controller.feed_audio(indata[:, 0] if indata.ndim > 1 else indata)

    def _open_mic(dev):
        s = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=CHUNK_SIZE, device=dev, callback=mic_callback,
        )
        s.start()
        return s

    def start_backend():
        if not detector.start():
            print("[Widget] Wake-word model failed to load — the widget will never appear.")
            return

        mic_name = get_input_device()
        mic_dev  = audio_devices.resolve(mic_name, "input")
        try:
            _open_mic(mic_dev)
            print(f"[Widget] Listening for 'Hey Jarvis' on "
                  f"'{mic_name or 'system default'}'. Ctrl+C in this console to stop.")
        except Exception as e:
            # Same fallback main.py's own _listen_audio uses: a device that's
            # listed but momentarily refuses to open (exclusive mode, JARVIS
            # itself holding it, a driver hiccup) must not mean the widget can
            # never hear "Hey Jarvis" at all.
            if mic_dev is None:
                print(f"[Widget] Could not open the microphone: {e}")
                print("[Widget] If JARVIS's own session is holding it exclusively, "
                      "try again once it's idle, or pick a different input device "
                      "in ⚙ → AUDIO DEVICES (both this widget and JARVIS use that choice).")
                return
            print(f"[Widget] Mic '{mic_name}' unavailable ({e}) — trying system default…")
            try:
                _open_mic(None)
                print("[Widget] Listening for 'Hey Jarvis' on the system default microphone.")
            except Exception as e2:
                print(f"[Widget] Could not open any microphone: {e2}")

    # ── window: frameless, always-on-top, bottom-right corner, hidden until
    #    'Hey Jarvis' fires. webview.screens[] resolves before start(), so the
    #    window is created in its final spot instead of jumping there once
    #    loaded. ──────────────────────────────────────────────────────────
    width, height, margin = 320, 300, 24
    x = y = None
    try:
        screen = webview.screens[0]
        x = screen.x + screen.width  - width  - margin
        y = screen.y + screen.height - height - margin
    except Exception:
        pass   # no screen info — fall back to pywebview's own default placement

    from urllib.parse import quote
    page_url = f"{WIDGET_DIR / 'arc_sentinel_widget.html'}?accent={quote(_get_accent_hex())}"

    window = webview.create_window(
        "Arc Sentinel",
        url=page_url,
        width=width, height=height, x=x, y=y,
        frameless=True, on_top=True, easy_drag=True,
        js_api=_js_api(controller),
        hidden=True,
    )
    controller.window = window
    window.events.loaded += lambda: threading.Thread(target=start_backend, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
