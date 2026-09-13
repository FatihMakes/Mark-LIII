"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import time
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
PUBLIC_TUNNEL_ENABLED = False
MAX_UPLOAD_MB = 500



def _ensure_local_tls(ip_address: str) -> bool:
    """Create a persistent local CA and a LAN server certificate.

    HTTP :8000 remains available for bootstrap/login and CA installation.
    HTTPS :8000 serves chat, voice, files, and WebSockets on one secure origin.
    The CA is generated once and kept stable; the leaf cert is regenerated when
    the current LAN IP is missing from its SANs.
    """
    cert_dir = BASE_DIR / "config" / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_key_p = cert_dir / "jarvis-ca.key"
    ca_crt_p = cert_dir / "jarvis-ca.crt"
    srv_key_p = cert_dir / "jarvis.key"
    srv_crt_p = cert_dir / "jarvis.crt"

    try:
        import ipaddress
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

        # Reuse a stable CA when present.
        if ca_key_p.exists() and ca_crt_p.exists():
            ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), password=None)
            ca_cert = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
        else:
            ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Local Voice CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
            ])
            now = datetime.now(timezone.utc)
            ca_cert = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True, key_encipherment=False,
                        content_commitment=False, data_encipherment=False,
                        key_agreement=False, key_cert_sign=True, crl_sign=True,
                        encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(ca_key, hashes.SHA256())
            )
            ca_key_p.write_bytes(ca_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
            ca_crt_p.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

        # Check whether the current leaf certificate already covers this LAN IP.
        regenerate = True
        if srv_key_p.exists() and srv_crt_p.exists():
            try:
                cert = x509.load_pem_x509_certificate(srv_crt_p.read_bytes())
                san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                ips = {str(v) for v in san.get_values_for_type(x509.IPAddress)}
                regenerate = ip_address not in ips or "127.0.0.1" not in ips
            except Exception:
                regenerate = True

        if regenerate:
            srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Local Remote"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
            ])
            now = datetime.now(timezone.utc)
            sans = [
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(ip_address)))
            except ValueError:
                pass
            srv_cert = (
                x509.CertificateBuilder()
                .subject_name(subject).issuer_name(ca_cert.subject)
                .public_key(srv_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                    critical=False,
                )
                .sign(ca_key, hashes.SHA256())
            )
            srv_key_p.write_bytes(srv_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
            srv_crt_p.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))

        return True
    except Exception as exc:
        print(f"[Dashboard] Local HTTPS unavailable: {exc}")
        return False



def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._phone_audio_clients: set[WebSocket] = set()
        self._uploads_dir                 = UPLOADS_DIR
        self._ready                       = False
        self._serve_error: str | None     = None
        self._startup_done                = False
        self._public_url: str | None      = None
        self._tunnel_process              = None
        self._secure_ready                = False
        self._secure_error: str | None    = None
        self._secure_server_task          = None
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_secure_url(self) -> str | None:
        if not self._ssl_enabled() or not self._secure_ready:
            return None
        return f"https://{self._ip}:{PORT}"

    def secure_ready(self) -> bool:
        return bool(self._secure_ready)

    def secure_error(self) -> str | None:
        return self._secure_error

    def get_url(self) -> str:
        if PUBLIC_TUNNEL_ENABLED and self._public_url:
            return self._public_url
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_local_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://127.0.0.1:{PORT}"

    def is_ready(self) -> bool:
        # Verify the actual listening socket, not only Uvicorn's internal flag.
        # This method is called from the Qt UI thread, so keep it synchronous
        # and very short.
        if not self._ready:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.25):
                return True
        except OSError:
            return False

    def serve_error(self) -> str | None:
        return self._serve_error

    def startup_done(self) -> bool:
        return self._startup_done

    def public_enabled(self) -> bool:
        return PUBLIC_TUNNEL_ENABLED

    def public_ready(self) -> bool:
        # Disabled is an intentional local/LAN mode, not a pending state.
        return (not PUBLIC_TUNNEL_ENABLED) or bool(self._public_url)

    def get_manual_url(self) -> str:
        if PUBLIC_TUNNEL_ENABLED and self._public_url:
            return self._public_url
        proto = "https" if self._ssl_enabled() else "http"
        return f"{self._ip}:{PORT}" if proto == "http" else f"https://{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def send_phone_audio(self, data: bytes) -> int:
        """Send Gemini PCM output to every active Remote Voice client.

        This must be a DashboardServer method (not a local function inside
        _build_app), because main.py calls self._dashboard.send_phone_audio().
        Returns the number of browser clients that successfully received it.
        """
        if not data or not self._phone_audio_clients:
            return 0

        sent = 0
        dead = []
        for client in tuple(self._phone_audio_clients):
            try:
                await client.send_bytes(data)
                sent += 1
            except Exception:
                dead.append(client)

        for client in dead:
            self._phone_audio_clients.discard(client)
        return sent

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/voice-ca.crt")
        async def voice_ca():
            path = BASE_DIR / "config" / "certs" / "jarvis-ca.crt"
            if not path.exists():
                return JSONResponse({"error": "Local voice CA not available"}, status_code=404)
            return FileResponse(
                str(path),
                media_type="application/x-x509-ca-cert",
                filename="jarvis-local-voice-ca.crt",
            )

        @app.get("/api/secure-health")
        async def secure_health():
            return JSONResponse({
                "ok": bool(self._secure_ready),
                "port": PORT,
                "error": self._secure_error,
                "url": f"https://{self._ip}:{PORT}",
            })

        @app.post("/api/secure-link")
        async def secure_link(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            secure = self.get_secure_url()
            if not secure:
                detail = self._secure_error or (
                    f"Secure JARVIS server is not listening on port {PORT} yet"
                )
                return JSONResponse(
                    {
                        "ok": False,
                        "error": detail,
                        "secure_ready": False,
                        "port": PORT,
                    },
                    status_code=503,
                )
            key = self.new_key(expiry_secs=600)
            return JSONResponse({
                "ok": True,
                "url": f"{secure}/auto-login?key={key}",
                "base": secure,
            })

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                dev_tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                self._device_sessions[dev_tok] = {"session_key": entered}
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Return a persistent device token too, so the phone can move
                # from HTTP bootstrap to the trusted local HTTPS voice origin.
                return JSONResponse({"ok": True, "token": tok, "device_token": dev_tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._phone_audio_clients.add(websocket)
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            chunk_count = 0
            try:
                while True:
                    data = await websocket.receive_bytes()
                    chunk_count += 1
                    try:
                        # Queue raw PCM bytes only. main.py wraps these exactly
                        # once into Gemini's realtime audio payload.
                        self._phone_audio_queue.put_nowait(data)
                        if chunk_count == 1 or chunk_count % 40 == 0:
                            asyncio.create_task(self.broadcast({
                                "type": "voice_diag",
                                "stage": "server_mic",
                                "chunks": chunk_count,
                                "bytes": len(data),
                            }))
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                self._phone_audio_clients.discard(websocket)
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── public tunnel ───────────────────────────────────────────────────────

    @staticmethod
    def _cloudflared_path() -> str | None:
        """Return cloudflared executable, installing the Windows binary once if needed."""
        found = shutil.which("cloudflared")
        if found:
            return found

        # The desktop build is primarily used on Windows. Keep the helper local
        # to the project so no PATH/admin changes are required.
        if sys.platform != "win32":
            return None

        arch = platform.machine().lower()
        asset = "cloudflared-windows-arm64.exe" if "arm" in arch else "cloudflared-windows-amd64.exe"
        target_dir = BASE_DIR / "config" / "bin"
        target = target_dir / "cloudflared.exe"
        if target.exists():
            return str(target)

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
            tmp = target.with_suffix(".download")
            print("[Dashboard] Installing Cloudflare Tunnel helper (one-time)...")
            urllib.request.urlretrieve(url, str(tmp))
            os.replace(tmp, target)
            print(f"[Dashboard] cloudflared installed: {target}")
            return str(target)
        except Exception as e:
            print(f"[Dashboard] cloudflared install failed: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    async def _start_public_tunnel(self) -> None:
        """Start a Cloudflare Quick Tunnel to the local dashboard.

        Quick Tunnels require no account or port-forwarding and provide an HTTPS
        URL suitable for phone microphone permissions and WebSockets.
        """
        loop = asyncio.get_running_loop()
        exe = await loop.run_in_executor(None, self._cloudflared_path)
        if not exe:
            print("[Dashboard] Public tunnel unavailable: cloudflared not found.")
            if sys.platform != "win32":
                print("[Dashboard] Install cloudflared and restart JARVIS for internet Remote Access.")
            return

        local = f"http://127.0.0.1:{PORT}"
        if self._ssl_enabled():
            # cloudflared connects locally; self-signed TLS would otherwise fail
            # certificate validation, so keep the origin HTTP-only expectation
            # explicit rather than silently exposing a broken public URL.
            local = f"https://127.0.0.1:{PORT}"

        cmd = [exe, "tunnel", "--url", local, "--no-autoupdate"]
        if self._ssl_enabled():
            cmd += ["--no-tls-verify"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._tunnel_process = proc
            pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                m = pattern.search(line)
                if m and not self._public_url:
                    self._public_url = m.group(0).rstrip("/")
                    print(f"[Dashboard] Public Remote Access: {self._public_url}")
            code = await proc.wait()
            if self._public_url:
                print(f"[Dashboard] Public tunnel stopped (exit {code}).")
            else:
                print(f"[Dashboard] Public tunnel failed to start (exit {code}).")
        except asyncio.CancelledError:
            if self._tunnel_process and self._tunnel_process.returncode is None:
                self._tunnel_process.terminate()
            raise
        except Exception as e:
            print(f"[Dashboard] Public tunnel error: {e}")

    # ── serve ─────────────────────────────────────────────────────────────

    async def serve(self) -> None:
        self._ready = False
        self._secure_ready = False
        self._serve_error = None
        self._secure_error = None
        self._startup_done = False

        if not _DEPS_OK:
            self._serve_error = "fastapi/uvicorn not installed"
            self._startup_done = True
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        # One secure origin for everything: login, chat, files, WebSockets,
        # and microphone audio all share HTTPS port 8000.
        use_ssl = _ensure_local_tls(self._ip)
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        if not use_ssl:
            self._serve_error = "Could not generate local TLS certificate"
            self._secure_error = self._serve_error
            self._startup_done = True
            return

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            lifespan="off",
            loop="asyncio",
            log_config=None,
            access_log=False,
            ssl_keyfile=str(ssl_key),
            ssl_certfile=str(ssl_cert),
        )
        server = uvicorn.Server(cfg)
        server_task = asyncio.create_task(server.serve())

        # Do not claim Remote Access is available until Uvicorn has really bound
        # the socket. Give startup a finite window so a stuck Uvicorn task becomes
        # a useful diagnostic instead of an endless "not listening yet" state.
        try:
            deadline = asyncio.get_running_loop().time() + 8.0
            while not server.started and not server_task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    self._serve_error = (
                        f"startup timed out after 8s; 127.0.0.1:{PORT} never opened"
                    )
                    self._startup_done = True
                    print(f"[Dashboard] Startup failed: {self._serve_error}")
                    server.should_exit = True
                    try:
                        await asyncio.wait_for(server_task, timeout=2.0)
                    except Exception:
                        server_task.cancel()
                    return
                await asyncio.sleep(0.05)

            if server_task.done() and not server.started:
                exc = server_task.exception()
                self._serve_error = str(exc) if exc else f"could not bind port {PORT}"
                self._startup_done = True
                print(f"[Dashboard] Startup failed: {self._serve_error}")
                return

            # Uvicorn says it started; independently verify that localhost accepts
            # TCP connections before exposing Remote Control to the UI.
            connected = False
            for _ in range(20):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
                    writer.close()
                    await writer.wait_closed()
                    connected = True
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            if not connected:
                self._serve_error = f"Uvicorn started but 127.0.0.1:{PORT} is unreachable"
                self._startup_done = True
                print(f"[Dashboard] Startup failed: {self._serve_error}")
                server.should_exit = True
                return

            self._ready = True
            self._secure_ready = True
            self._secure_error = None
            self._startup_done = True
            print(f"[Dashboard] Local check: https://127.0.0.1:{PORT}")
            print(f"[Dashboard] LAN:         https://{self._ip}:{PORT}")
            print(f"[Dashboard] Chat + Voice: same secure port {PORT}")
            print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")

            # Internet access starts only after the local origin is confirmed live.
            # Public internet tunnel temporarily disabled.
            # Localhost/LAN Remote Access remains enabled.
            # asyncio.create_task(self._start_public_tunnel())
            await server_task
        except asyncio.CancelledError:
            server.should_exit = True
            raise
        except Exception as e:
            self._serve_error = str(e)
            self._startup_done = True
            print(f"[Dashboard] Server error: {e}")
        finally:
            self._ready = False

