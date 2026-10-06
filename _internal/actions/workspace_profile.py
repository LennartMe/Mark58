"""User-facing control for Smart Workspace Profiles."""
from __future__ import annotations

from core.workspace_profiles import PROFILES, WorkspaceProfileManager


def run(parameters: dict, **ctx) -> str:
    p = parameters or {}; action = str(p.get("action") or "status").strip().lower()
    mgr = WorkspaceProfileManager()
    if action == "status":
        st = mgr.status()
        return f"Workspace profile: {st['label']} ({st['active']}); mode={st['mode']}; reason={st['reason']}; proactive={'allowed' if st['allow_proactive'] else 'suppressed'}."
    if action == "auto":
        active = mgr.set("general", "auto")
        st = mgr.status()
        return f"Smart Workspace Profiles set to automatic. Current profile: {st['label']} — {st['reason']}."
    if action in {"set", "manual"}:
        profile = str(p.get("profile") or "general").strip().lower()
        if profile not in PROFILES:
            return "Unknown profile. Use general, coding, gaming, study, media, work, or focus."
        mgr.set(profile, "manual")
        return f"Workspace profile locked to {PROFILES[profile]['label']}."
    if action == "detect":
        active = mgr.refresh(); st = mgr.status()
        return f"Detected profile: {st['label']} — {st['reason']}."
    return "Use status, auto, detect, or set."


TOOL = {
    "name": "workspace_profile",
    "description": (
        "Inspect or control Smart Workspace Profiles. In auto mode JARVIS detects coding, gaming, study, media and work contexts from the active application and silently adjusts response/proactive behaviour while keeping the same HUD."
    ),
    "brief": "View, auto-detect or set coding/gaming/study/media/work/focus behaviour profile.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | auto | detect | set"},
            "profile": {"type": "STRING", "description": "general | coding | gaming | study | media | work | focus"},
        },
        "required": ["action"],
    },
    "handler": run,
}
