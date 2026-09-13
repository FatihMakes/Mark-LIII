"""Guarded voice/chat self-repair action for JARVIS.

The action is intentionally two-phase:
1. diagnose: AI may inspect allowed project source and stage an exact patch.
2. apply: the staged patch is applied only after the user explicitly confirms.

No generated shell commands are executed. Sensitive/runtime paths and the guardrail
implementation itself are outside the writable repair sandbox.
"""
from __future__ import annotations

import sys
from pathlib import Path

from core.self_healing import SelfHealingEngine


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def self_repair(parameters: dict, player=None, speak=None, **kwargs) -> str:
    p = parameters or {}
    action = str(p.get("action", "diagnose")).strip().lower()
    issue = str(p.get("issue", "")).strip()
    engine = SelfHealingEngine(_base_dir())

    if action in {"diagnose", "plan", "repair"}:
        if not issue:
            return "Tell me what is wrong so I can diagnose it before changing anything."
        return engine.stage_ai_repair(issue)

    if action in {"apply", "confirm"}:
        return engine.apply_pending_ai_repair()

    if action in {"cancel", "discard"}:
        return engine.cancel_pending_ai_repair()

    if action in {"status", "pending"}:
        return engine.pending_ai_repair_status()

    return "Unknown self-repair action. Use diagnose, apply, cancel, or status."


TOOL = {
    "name": "self_repair",
    "description": (
        "Diagnoses and safely repairs JARVIS's own source code from a user's chat or voice request. "
        "ALWAYS call action='diagnose' first when the user asks JARVIS to fix/repair itself or one of its own features. "
        "Diagnosis only stages a bounded patch and does NOT modify source. "
        "Call action='apply' ONLY after the user explicitly confirms the staged repair in a later message (for example: yes, apply it, lanjutkan, terapkan). "
        "Use cancel to discard the staged repair and status to inspect it. Never skip confirmation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "diagnose | apply | cancel | status"
            },
            "issue": {
                "type": "STRING",
                "description": "User's description of the JARVIS problem. Required for diagnose."
            }
        },
        "required": ["action"]
    },
    "handler": self_repair,
}
