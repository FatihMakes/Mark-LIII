"""Guarded self-healing for JARVIS.

Conservative by design:
- no LLM-generated source edits
- exact whitelisted repairs only
- backup before source edits
- syntax validation after edits
- rollback on failure
- append-only repair history
"""
from __future__ import annotations

import json
import py_compile
import shutil
import socket
import time
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class RepairRecord:
    ts: float
    rule: str
    target: str
    status: str
    detail: str


class SelfHealingEngine:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()
        self.state_dir = self.base_dir / "config" / "self_healing"
        self.backup_dir = self.state_dir / "backups"
        self.history_path = self.state_dir / "repair_history.jsonl"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _record(self, rule: str, target, status: str, detail: str) -> None:
        rec = RepairRecord(time.time(), rule, str(target), status, detail)
        try:
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def heal_dashboard_runtime(self, dashboard) -> list[str]:
        """Repair only safe in-memory dashboard state inconsistencies."""
        repaired: list[str] = []
        try:
            listening = self._tcp_open("127.0.0.1", 8000)
            if listening and not getattr(dashboard, "_ready", False):
                dashboard._ready = True
                dashboard._serve_error = None
                repaired.append("dashboard readiness state")
                self._record(
                    "dashboard_ready_state", "runtime", "repaired",
                    "Port 8000 was listening while _ready was false; state synchronized."
                )

            public_enabled = bool(
                dashboard.public_enabled()
                if hasattr(dashboard, "public_enabled")
                else False
            )
            if not public_enabled:
                changed = False
                if getattr(dashboard, "_public_url", None):
                    dashboard._public_url = None
                    repaired.append("stale public URL")
                    changed = True

                proc = getattr(dashboard, "_tunnel_process", None)
                if proc is not None and getattr(proc, "returncode", None) is None:
                    try:
                        proc.terminate()
                        repaired.append("stale public tunnel process")
                        changed = True
                    except Exception:
                        pass

                if changed:
                    self._record(
                        "public_tunnel_disabled_state", "runtime", "repaired",
                        "Public tunnel is disabled; stale public tunnel state was cleared."
                    )
        except Exception as e:
            self._record("runtime_dashboard_heal", "runtime", "failed", str(e))
        return repaired

    def repair_known_source_regressions(self) -> list[str]:
        """Repair exact, known-safe source regressions with backup and rollback."""
        repaired: list[str] = []
        main_py = self.base_dir / "main.py"
        if not main_py.exists():
            return repaired

        try:
            text = main_py.read_text(encoding="utf-8")
        except Exception as e:
            self._record("remote_control_public_gate", main_py, "failed", str(e))
            return repaired

        old = (
            "        if not self._dashboard.public_ready():\n"
            "            self.ui.write_log(\n"
            "                \"SYS: Local dashboard is ready, but the public tunnel is still connecting. \"\n"
            "                \"Try REMOTE CONTROL again shortly.\"\n"
            "            )\n"
            "            return None\n"
        )
        new = (
            "        if self._dashboard.public_enabled() and not self._dashboard.public_ready():\n"
            "            self.ui.write_log(\n"
            "                \"SYS: Local dashboard is ready, but the public tunnel is still connecting. \"\n"
            "                \"Try REMOTE CONTROL again shortly.\"\n"
            "            )\n"
            "            return None\n"
        )

        if old not in text:
            return repaired

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.backup_dir / f"main.py.{stamp}.bak"
        try:
            shutil.copy2(main_py, backup)
            main_py.write_text(text.replace(old, new, 1), encoding="utf-8")
            py_compile.compile(str(main_py), doraise=True)
        except Exception as e:
            try:
                if backup.exists():
                    shutil.copy2(backup, main_py)
            finally:
                self._record(
                    "remote_control_public_gate", main_py, "rolled_back",
                    f"Validation failed: {e}"
                )
            return repaired

        repaired.append("Remote Control public-tunnel gate")
        self._record(
            "remote_control_public_gate", main_py, "repaired",
            f"Exact safe patch applied; backup={backup.name}; py_compile=PASS"
        )
        return repaired
