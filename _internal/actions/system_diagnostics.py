"""Self-repair and deep diagnostics tool."""
from __future__ import annotations

import json
from pathlib import Path

from core.diagnostics import REPORT_PATH, format_report, run_diagnostics, safe_repair


def run(parameters: dict, **ctx) -> str:
    action = str((parameters or {}).get("action") or "scan").strip().lower()
    if action in {"scan", "diagnose", "test"}:
        return format_report(run_diagnostics(compile_python=True))
    if action in {"repair", "safe_repair"}:
        result = safe_repair()
        lines = ["Self-repair: " + ("SUCCESS" if result.get("ok") else "NEEDS ATTENTION")]
        if result.get("changes"):
            lines.append("Repairs: " + "; ".join(result["changes"]))
        if result.get("errors"):
            lines.append("Errors: " + "; ".join(result["errors"]))
        lines.append(format_report(result.get("after") or {}))
        return "\n".join(lines)
    if action == "last_report":
        if not REPORT_PATH.exists():
            return "No diagnostic report has been created yet."
        try:
            return format_report(json.loads(REPORT_PATH.read_text(encoding="utf-8")))
        except Exception as exc:
            return f"Could not read the last diagnostic report: {exc}"
    return "Unknown action. Use scan, repair, or last_report."


TOOL = {
    "name": "system_diagnostics",
    "description": (
        "Runs deep Mark 58 diagnostics and conservative self-repair. Use scan to check runtime files, configuration, "
        "memory database integrity, audio routes, browser bridge and Python source integrity. Use repair only for safe "
        "repairs such as stale cache cleanup, database checkpointing, required-directory recreation and browser-session reset; "
        "it does not delete user data or rewrite source code."
    ),
    "brief": "Diagnose Mark 58 subsystems or run safe non-destructive self-repair.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "scan | repair | last_report"}
        },
        "required": ["action"],
    },
    "handler": run,
}
