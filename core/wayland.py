"""
Wayland-native backend for computer control (Hyprland-first).

Why this exists: the default primitives in actions/computer_control.py speak
X11 (PyAutoGUI) and the screen capture in actions/screen_processor.py speaks
X11 (mss). Under a rootless XWayland session both silently misbehave:

  * mss captures XWayland's empty root window (pure black frame),
  * PyAutoGUI moves an X11 ghost pointer — position() even confirms moves
    that never reach the real Wayland cursor.

This module routes the same operations through Wayland-native tools instead:

  * eyes    → grim (real compositor frame)
  * mouse   → ydotool daemon, absolute mode with auto-calibrated scale
              (ydotool's absolute range rarely matches the screen 1:1;
              we probe once and cache the factor)
  * typing  → wtype
  * keys    → ydotool raw evdev keycodes (names mapped below)
  * windows → hyprctl dispatch
  * clipboard → wl-copy / wl-paste

Everything here shells out to small CLI tools and returns plain strings, so
callers can prefer it and fall back to the X11 path when it is unavailable.
No new Python dependencies.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

# ── detection ────────────────────────────────────────────────────────────────

_HYPRCTL = shutil.which("hyprctl")
_GRIM = shutil.which("grim")
_YDOTOOL = shutil.which("ydotool")
_WTYPE = shutil.which("wtype")
_WL_COPY = shutil.which("wl-copy")
_WL_PASTE = shutil.which("wl-paste")


def is_wayland() -> bool:
    """True when a Wayland compositor session owns the display."""
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def is_hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and bool(_HYPRCTL)


def backend_available() -> bool:
    """True when the full Wayland backend (eyes + hands) is usable."""
    return bool(is_wayland() and _GRIM and _YDOTOOL and _WTYPE)


def _run(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


# ── eyes ─────────────────────────────────────────────────────────────────────

def screen_size() -> tuple[int, int]:
    """Focused monitor size in screen pixels (falls back to 1366×768)."""
    if _HYPRCTL:
        try:
            monitors = json.loads(_run([_HYPRCTL, "monitors", "-j"]).stdout or b"[]")
            for m in monitors:
                if m.get("focused"):
                    return int(m["width"]), int(m["height"])
            if monitors:
                return int(monitors[0]["width"]), int(monitors[0]["height"])
        except Exception:
            pass
    return 1366, 768


def screenshot_png() -> bytes:
    """Capture the real compositor frame; raises on failure."""
    if not _GRIM:
        raise RuntimeError("grim not installed (pacman -S grim)")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        result = _run([_GRIM, path])
        if result.returncode != 0:
            raise RuntimeError(f"grim failed: {result.stderr.decode()[:200]}")
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── mouse ────────────────────────────────────────────────────────────────────

_SCALE: float | None = None


def _abs_scale() -> float:
    """Probe ydotool's absolute range with a two-point move.

    A single-point probe breaks when the cursor starts right of the probe
    point (negative delta -> negative scale -> clamped garbage). Two
    same-direction moves give a sign-correct factor. Result cached."""
    global _SCALE
    if _SCALE is not None:
        return _SCALE
    _SCALE = 1.0
    try:
        _run([_YDOTOOL, "mousemove", "-a", "-x", "200", "-y", "200"])
        time.sleep(0.3)
        x1, _ = cursor_pos()
        _run([_YDOTOOL, "mousemove", "-a", "-x", "400", "-y", "400"])
        time.sleep(0.3)
        x2, _ = cursor_pos()
        dx = x2 - x1
        if abs(dx) > 5:
            _SCALE = dx / 200.0
    except Exception:
        pass
    return _SCALE


def cursor_pos() -> tuple[int, int]:
    if _HYPRCTL:
        try:
            out = _run([_HYPRCTL, "cursorpos"]).stdout.decode().strip()
            x, y = out.split(",")
            return int(x.strip()), int(y.strip())
        except Exception:
            pass
    return 0, 0


def mouse_move(x: int, y: int) -> str:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    scale = _abs_scale()
    ax, ay = int(x / scale), int(y / scale)
    result = _run([_YDOTOOL, "mousemove", "-a", "-x", str(ax), "-y", str(ay)])
    if result.returncode != 0:
        raise RuntimeError(f"ydotool mousemove failed: {result.stderr.decode()[:200]}")
    time.sleep(0.15)
    px, py = cursor_pos()
    return f"Mouse → ({x}, {y}) [at {px},{py}]"


_CLICK_BUTTONS = {"left": "C0", "right": "C1", "middle": "C2"}


def mouse_down(button: str = "left") -> None:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    code = {"left": "0x40", "right": "0x41", "middle": "0x42"}.get(button, "0x40")
    result = _run([_YDOTOOL, "click", code])
    if result.returncode != 0:
        raise RuntimeError(f"ydotool button-down failed: {result.stderr.decode()[:200]}")


def mouse_up(button: str = "left") -> None:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    code = {"left": "0x80", "right": "0x81", "middle": "0x82"}.get(button, "0x80")
    result = _run([_YDOTOOL, "click", code])
    if result.returncode != 0:
        raise RuntimeError(f"ydotool button-up failed: {result.stderr.decode()[:200]}")


def mouse_click(button: str = "left", clicks: int = 1) -> str:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    code = _CLICK_BUTTONS.get(button, "C0")
    args = [_YDOTOOL, "click"]
    if clicks > 1:
        args += ["-r", str(clicks)]
    args.append(code)
    result = _run(args)
    if result.returncode != 0:
        raise RuntimeError(f"ydotool click failed: {result.stderr.decode()[:200]}")
    label = "Double-click" if clicks == 2 else "Clicked"
    return f"{label} [{button}]"


def scroll(direction: str = "down", amount: int = 3) -> str:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    if direction == "up":
        dx, dy = 0, -amount
    elif direction == "down":
        dx, dy = 0, amount
    elif direction == "left":
        dx, dy = -amount, 0
    else:  # right
        dx, dy = amount, 0
    result = _run([_YDOTOOL, "mousemove", "-w", "--", str(dx), str(dy)])
    if result.returncode != 0:
        raise RuntimeError(f"ydotool wheel failed: {result.stderr.decode()[:200]}")
    return f"Scrolled {direction} ×{amount}"


# ── keyboard ─────────────────────────────────────────────────────────────────

# evdev keycodes (KEY_*): ydotool key speaks raw codes, not names.
_KEYCODES = {
    "esc": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10, "0": 11, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22,
    "i": 23, "o": 24, "p": 25, "enter": 28, "ctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36,
    "k": 37, "l": 38, "semicolon": 39, "shift": 42, "z": 44, "x": 45,
    "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, "comma": 51,
    "dot": 52, "period": 52, "slash": 53, "alt": 56, "space": 57,
    "f5": 63, "home": 102, "up": 103, "pageup": 104, "left": 105,
    "right": 106, "end": 107, "down": 108, "pagedown": 109,
    "delete": 111, "super": 125, "win": 125, "command": 125,
}


def _key_seq(names: list[str], down: bool = True) -> list[str]:
    seq: list[str] = []
    for name in names:
        code = _KEYCODES.get(name.lower())
        if code is None:
            raise ValueError(f"unknown key name for ydotool: '{name}'")
        seq.append(f"{code}:{'1' if down else '0'}")
    return seq


def press_key(key: str) -> str:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    seq = _key_seq([key])
    result = _run([_YDOTOOL, "key"] + seq + _key_seq([key], down=False))
    if result.returncode != 0:
        raise RuntimeError(f"ydotool key failed: {result.stderr.decode()[:200]}")
    return f"Pressed: {key}"


def hotkey(*keys: str) -> str:
    if not _YDOTOOL:
        raise RuntimeError("ydotool not installed / daemon not running")
    names = list(keys)
    seq = _key_seq(names, down=True) + _key_seq(list(reversed(names)), down=False)
    result = _run([_YDOTOOL, "key"] + seq)
    if result.returncode != 0:
        raise RuntimeError(f"ydotool hotkey failed: {result.stderr.decode()[:200]}")
    return f"Hotkey: {'+'.join(names)}"


def type_text(text: str) -> str:
    if not _WTYPE:
        raise RuntimeError("wtype not installed (pacman -S wtype)")
    time.sleep(0.2)
    result = _run([_WTYPE, "--", text])
    if result.returncode != 0:
        raise RuntimeError(f"wtype failed: {result.stderr.decode()[:200]}")
    clipped = text[:60] + ("…" if len(text) > 60 else "")
    return f"Typed: {clipped}"


def clear_field() -> str:
    hotkey("ctrl", "a")
    time.sleep(0.1)
    press_key("delete")
    return "Field cleared"


# ── clipboard ────────────────────────────────────────────────────────────────

def clipboard_get() -> str:
    if not _WL_PASTE:
        raise RuntimeError("wl-paste not installed (pacman -S wl-clipboard)")
    result = _run([_WL_PASTE])
    if result.returncode != 0:
        raise RuntimeError(f"wl-paste failed: {result.stderr.decode()[:200]}")
    return result.stdout.decode()


def clipboard_paste(text: str) -> str:
    if not _WL_COPY:
        raise RuntimeError("wl-copy not installed (pacman -S wl-clipboard)")
    proc = subprocess.run([_WL_COPY], input=text.encode(),
                          capture_output=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"wl-copy failed: {proc.stderr.decode()[:200]}")
    time.sleep(0.1)
    hotkey("ctrl", "v")
    clipped = text[:60] + ("…" if len(text) > 60 else "")
    return f"Pasted: {clipped}"


# ── windows & workspaces ───────────────────────────────────────────────────
# NOTE (Hyprland ≥0.56): plain `hyprctl dispatch <name> <args>` is broken
# upstream (the CLI wraps it in a Lua shorthand that fails to parse), and the
# Lua table has no direct workspace-goto. We drive the compositor through its
# own default keybinds (SUPER+1..9) instead — robust across builds.

def _hypr_query(*args: str):
    if not _HYPRCTL:
        raise RuntimeError("hyprctl not available (not on Hyprland?)")
    result = _run([_HYPRCTL, *args])
    if result.returncode != 0:
        raise RuntimeError(f"hyprctl failed: {result.stderr.decode()[:200]}")
    return json.loads(result.stdout or b"null")


def workspace(number: int) -> str:
    if not 1 <= int(number) <= 9:
        raise ValueError("workspace number must be 1..9 (SUPER+1..9 binds)")
    hotkey("super", str(int(number)))
    time.sleep(0.4)
    return f"Switched to workspace {int(number)}"


def focus_window(title: str) -> str:
    # No working focus dispatch on this build: locate the window's workspace
    # via the (working) query API and jump there.
    clients = _hypr_query("clients", "-j") or []
    needle = title.lower()
    for client in clients:
        hay = f"{client.get('title', '')} {client.get('class', '')}".lower()
        if needle and needle in hay:
            ws = client.get("workspace", {}).get("id")
            if ws:
                workspace(int(ws))
                return f"Focused window: {title} (workspace {ws})"
    return f"No window matching '{title}' found"


# ── task manager ─────────────────────────────────────────────────────────────

_TASK_MANAGER_CHAIN = [
    ["missioncenter"],
    ["gnome-system-monitor"],
    ["xfce4-taskmanager"],
]


def open_task_manager() -> str:
    for cmd in _TASK_MANAGER_CHAIN:
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return f"Opened task manager: {cmd[0]}"
    for term in ("kitty", "foot", "xdg-terminal-exec"):
        if shutil.which(term):
            if term == "xdg-terminal-exec":
                subprocess.Popen([term, "btop"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([term, "-e", "btop"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            return f"Opened task manager: btop in {term}"
    return "No task manager found (missioncenter/gnome-system-monitor/btop)"
