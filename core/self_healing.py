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
import re
import uuid
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

    # ------------------------------------------------------------------
    # Guarded AI self-repair (two-phase: stage -> explicit user apply)
    # ------------------------------------------------------------------
    @property
    def _pending_path(self) -> Path:
        return self.state_dir / "pending_repair.json"

    @property
    def _ai_backup_root(self) -> Path:
        p = self.backup_dir / "ai_repairs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _safe_relative_source(self, raw: str) -> Path | None:
        """Resolve a generated path only if it is inside the repair sandbox."""
        raw = str(raw or "").replace("\\", "/").strip()
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw or raw.startswith("/") or ".." in Path(raw).parts:
            return None

        rel = Path(raw)
        # Keep the guardrail implementation immutable from generated patches.
        denied_exact = {
            Path("core/self_healing.py"),
            Path("actions/self_repair.py"),
        }
        denied_roots = {"config", ".git", "venv", ".venv", "__pycache__"}
        if rel in denied_exact or (rel.parts and rel.parts[0].lower() in denied_roots):
            return None

        allowed_top_files = {"main.py", "ui.py"}
        allowed_roots = {"core", "actions", "dashboard"}
        allowed_suffixes = {".py", ".html", ".js", ".css", ".txt"}

        if len(rel.parts) == 1:
            if rel.name not in allowed_top_files:
                return None
        elif rel.parts[0] not in allowed_roots:
            return None

        if rel.suffix.lower() not in allowed_suffixes:
            return None

        full = (self.base_dir / rel).resolve()
        try:
            full.relative_to(self.base_dir)
        except ValueError:
            return None
        if not full.is_file():
            return None
        return rel

    def _source_manifest(self) -> list[str]:
        items: list[str] = []
        candidates = [self.base_dir / "main.py", self.base_dir / "ui.py"]
        for root in ("core", "actions", "dashboard"):
            base = self.base_dir / root
            if base.exists():
                candidates.extend(p for p in base.rglob("*") if p.is_file())

        for full in sorted(set(candidates)):
            try:
                rel = full.relative_to(self.base_dir)
            except ValueError:
                continue
            safe = self._safe_relative_source(rel.as_posix())
            if safe is None:
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size <= 250_000:
                items.append(f"{safe.as_posix()} ({size} bytes)")
        return items

    def _get_ai_model(self):
        api_path = self.base_dir / "config" / "api_keys.json"
        try:
            cfg = json.loads(api_path.read_text(encoding="utf-8"))
            key = str(cfg.get("gemini_api_key", "")).strip()
        except Exception as e:
            raise RuntimeError(f"Could not read Gemini API key: {e}") from e
        if not key:
            raise RuntimeError("Gemini API key is missing.")

        from google import genai
        client = genai.Client(api_key=key)

        class _Model:
            def generate_content(self, prompt: str):
                return client.models.generate_content(
                    model="gemini-flash-latest",
                    contents=prompt,
                )

        return _Model()

    def _select_repair_files(self, issue: str, manifest: list[str]) -> list[Path]:
        model = self._get_ai_model()
        prompt = f"""You are diagnosing JARVIS, a local Python desktop assistant.
Select the smallest set of existing source files most likely responsible for the user's issue.
You are only selecting files; do not propose code yet.

USER ISSUE:
{issue}

AVAILABLE FILES:
{chr(10).join(manifest)}

Return ONLY JSON:
{{"files":["path/one.py","path/two.html"]}}
Rules:
- choose 1 to 6 files maximum
- only choose exact paths from AVAILABLE FILES
- prefer the direct implementation path, not broad unrelated files
- never select config, credentials, certificates, backups, core/self_healing.py, or actions/self_repair.py
"""
        raw = self._strip_json_fence(getattr(model.generate_content(prompt), "text", ""))
        data = json.loads(raw)
        selected: list[Path] = []
        for item in data.get("files", [])[:6]:
            rel = self._safe_relative_source(str(item))
            if rel is not None and rel not in selected:
                selected.append(rel)
        if not selected:
            raise RuntimeError("The diagnosis did not identify any safe source files.")
        return selected

    def _build_repair_context(self, files: list[Path], max_chars: int = 110_000) -> str:
        blocks: list[str] = []
        used = 0
        for rel in files:
            full = self.base_dir / rel
            text = full.read_text(encoding="utf-8", errors="replace")
            room = max_chars - used
            if room <= 0:
                break
            if len(text) > room:
                text = text[:room]
            block = f"\n===== FILE: {rel.as_posix()} =====\n{text}\n===== END FILE =====\n"
            blocks.append(block)
            used += len(block)
        return "".join(blocks)

    def _validate_generated_edits(self, data: dict, selected: list[Path]) -> list[dict]:
        raw_edits = data.get("edits")
        if not isinstance(raw_edits, list) or not raw_edits:
            raise RuntimeError("AI diagnosis produced no repair edits.")
        if len(raw_edits) > 8:
            raise RuntimeError("AI repair exceeded the 8-edit safety limit.")

        selected_set = {p.as_posix() for p in selected}
        edits: list[dict] = []
        for idx, edit in enumerate(raw_edits, start=1):
            if not isinstance(edit, dict):
                raise RuntimeError(f"Repair edit {idx} is invalid.")
            rel = self._safe_relative_source(str(edit.get("path", "")))
            if rel is None or rel.as_posix() not in selected_set:
                raise RuntimeError(f"Repair edit {idx} targets a file outside the diagnosed sandbox.")
            old = edit.get("old")
            new = edit.get("new")
            reason = str(edit.get("reason", "")).strip()
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise RuntimeError(f"Repair edit {idx} must contain non-empty exact 'old' text and string 'new' text.")
            if old == new:
                raise RuntimeError(f"Repair edit {idx} makes no change.")
            if len(old) > 60_000 or len(new) > 60_000:
                raise RuntimeError(f"Repair edit {idx} exceeds the per-edit size limit.")

            current = (self.base_dir / rel).read_text(encoding="utf-8", errors="replace")
            count = current.count(old)
            if count != 1:
                raise RuntimeError(
                    f"Repair edit {idx} for {rel.as_posix()} is not exact/unique (matches={count})."
                )
            edits.append({
                "path": rel.as_posix(),
                "old": old,
                "new": new,
                "reason": reason,
            })
        return edits

    def stage_ai_repair(self, issue: str) -> str:
        """Diagnose an issue and persist a bounded exact patch. Does not edit source."""
        issue = str(issue or "").strip()
        if not issue:
            return "Self-repair diagnosis needs a problem description."

        try:
            manifest = self._source_manifest()
            selected = self._select_repair_files(issue, manifest)
            context = self._build_repair_context(selected)
            model = self._get_ai_model()
            prompt = f"""You are a senior engineer repairing the JARVIS application itself.
Diagnose the user's concrete problem from the supplied source and propose the smallest safe fix.

USER ISSUE:
{issue}

SOURCE:
{context}

Return ONLY valid JSON with this exact shape:
{{
  "summary": "short user-facing description of the probable root cause and repair",
  "confidence": 0,
  "restart_required": true,
  "edits": [
    {{
      "path": "exact/relative/file.py",
      "old": "exact text copied verbatim from SOURCE",
      "new": "replacement text",
      "reason": "why this edit fixes the issue"
    }}
  ]
}}

STRICT SAFETY RULES:
- Make the minimum change needed for this issue only. Do not refactor unrelated code or UI.
- Maximum 8 edits across the selected files.
- Every 'old' must be copied EXACTLY and must be unique in its file.
- Do not modify security guardrails, credentials, certificates, config secrets, public tunnel policy, or self-repair implementation.
- Do not generate shell commands, package installation, registry changes, privilege escalation, UAC bypass, or deletion of user data.
- Preserve existing behavior unless it directly causes the reported issue.
- If the issue cannot be repaired safely from these files, return {{"summary":"Cannot safely stage this repair from the inspected files.","confidence":0,"restart_required":false,"edits":[]}}.
"""
            raw = self._strip_json_fence(getattr(model.generate_content(prompt), "text", ""))
            data = json.loads(raw)
            edits = self._validate_generated_edits(data, selected)

            txid = uuid.uuid4().hex[:12]
            payload = {
                "version": 1,
                "id": txid,
                "created_at": time.time(),
                "issue": issue,
                "summary": str(data.get("summary", "Repair prepared.")).strip(),
                "confidence": max(0, min(100, int(data.get("confidence", 0) or 0))),
                "restart_required": bool(data.get("restart_required", True)),
                "files": sorted({e["path"] for e in edits}),
                "edits": edits,
            }
            tmp = self._pending_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._pending_path)
            self._record(
                "ai_self_repair_stage", ", ".join(payload["files"]), "staged",
                f"id={txid}; issue={issue[:180]}; edits={len(edits)}"
            )
            files = ", ".join(payload["files"])
            return (
                f"Repair staged, not applied yet. {payload['summary']} "
                f"Files: {files}. Confidence: {payload['confidence']}%. "
                "Explicit confirmation is required before I modify the source."
            )
        except Exception as e:
            self._record("ai_self_repair_stage", "pending", "failed", str(e))
            return f"I could not safely stage that repair: {e}"

    def _load_pending_ai_repair(self) -> dict | None:
        if not self._pending_path.exists():
            return None
        try:
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("edits"):
                return None
            return data
        except Exception:
            return None

    def pending_ai_repair_status(self) -> str:
        data = self._load_pending_ai_repair()
        if not data:
            return "There is no pending self-repair."
        files = ", ".join(data.get("files", []))
        return (
            f"Pending repair {data.get('id', '')}: {data.get('summary', '')} "
            f"Files: {files}. It has not been applied."
        )

    def cancel_pending_ai_repair(self) -> str:
        data = self._load_pending_ai_repair()
        if not self._pending_path.exists():
            return "There is no pending self-repair to cancel."
        try:
            self._pending_path.unlink()
            self._record(
                "ai_self_repair_cancel",
                (data or {}).get("id", "pending"),
                "cancelled",
                "Pending AI repair discarded before source modification."
            )
            return "Pending self-repair cancelled. No source files were changed."
        except Exception as e:
            return f"Could not cancel the pending self-repair: {e}"

    def _validate_repaired_files(self, paths: list[Path]) -> None:
        for rel in paths:
            full = self.base_dir / rel
            if rel.suffix.lower() == ".py":
                py_compile.compile(str(full), doraise=True)
            else:
                text = full.read_text(encoding="utf-8", errors="strict")
                if "\x00" in text:
                    raise ValueError(f"NUL byte found after repair in {rel.as_posix()}")

    def apply_pending_ai_repair(self) -> str:
        """Apply the latest staged patch transactionally, then validate or rollback."""
        data = self._load_pending_ai_repair()
        if not data:
            return "There is no pending self-repair to apply."

        txid = str(data.get("id") or uuid.uuid4().hex[:12])
        backup_dir = self._ai_backup_root / txid
        backup_dir.mkdir(parents=True, exist_ok=False)
        touched: list[Path] = []

        try:
            # Revalidate paths and exact matches against the current on-disk source.
            edits_by_file: dict[Path, list[dict]] = {}
            for edit in data.get("edits", []):
                rel = self._safe_relative_source(str(edit.get("path", "")))
                if rel is None:
                    raise RuntimeError("Pending repair contains a path outside the repair sandbox.")
                old = edit.get("old")
                new = edit.get("new")
                if not isinstance(old, str) or not old or not isinstance(new, str):
                    raise RuntimeError("Pending repair contains an invalid exact replacement.")
                edits_by_file.setdefault(rel, []).append(edit)

            # Build every new file in memory first; no partial writes yet.
            proposed: dict[Path, str] = {}
            for rel, edits in edits_by_file.items():
                full = self.base_dir / rel
                text = full.read_text(encoding="utf-8", errors="strict")
                for edit in edits:
                    old, new = edit["old"], edit["new"]
                    if text.count(old) != 1:
                        raise RuntimeError(
                            f"Source changed since diagnosis; exact patch no longer matches {rel.as_posix()}."
                        )
                    text = text.replace(old, new, 1)
                proposed[rel] = text

            # Backup originals and write transaction.
            for rel in proposed:
                src = self.base_dir / rel
                dst = backup_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

            for rel, text in proposed.items():
                (self.base_dir / rel).write_text(text, encoding="utf-8")
                touched.append(rel)

            self._validate_repaired_files(touched)

        except Exception as e:
            # Roll back every file for which a backup exists.
            for backup in backup_dir.rglob("*"):
                if backup.is_file():
                    rel = backup.relative_to(backup_dir)
                    target = self.base_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
            self._record(
                "ai_self_repair_apply", txid, "rolled_back",
                f"Validation/apply failed and originals restored: {e}"
            )
            return f"Repair failed validation and was rolled back: {e}"

        try:
            self._pending_path.unlink(missing_ok=True)
        except Exception:
            pass
        self._record(
            "ai_self_repair_apply", txid, "repaired",
            f"files={','.join(p.as_posix() for p in touched)}; backup={backup_dir}; validation=PASS"
        )
        restart = bool(data.get("restart_required", True))
        suffix = " Restart JARVIS to load the repaired code." if restart else ""
        return (
            f"Self-repair applied and validation passed. Backup: {backup_dir.name}."
            f"{suffix}"
        )
