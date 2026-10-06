"""Voice-accessible Teach Jarvis controls."""
from core.teach_mode import manager


def teach_jarvis(parameters: dict, **_ctx) -> str:
    p = parameters or {}
    mode = str(p.get("mode") or "").strip().lower()
    name = str(p.get("name") or "").strip()
    if mode == "start":
        return manager().start(name)
    if mode in ("remember", "save", "stop"):
        return manager().stop(name)
    if mode == "cancel":
        return manager().cancel()
    if mode == "run":
        return manager().replay(name, float(p.get("speed") or 1.0))
    if mode == "delete":
        return manager().delete(name)
    if mode == "list":
        rows = manager().list()
        if not rows:
            return "No learned tasks have been saved for this profile yet."
        return "Learned tasks: " + ", ".join(r["name"] for r in rows[:20])
    return "Choose start, remember, run, list, delete, or cancel for Teach Mode."


TOOL = {
    "name": "teach_jarvis",
    "description": (
        "Teach Jarvis a reusable desktop routine by watching the user's keyboard and mouse demonstration. "
        "Use mode=start when the user says things like 'watch how I do this' or 'learn this task'. "
        "Use mode=remember when the user says 'remember how I did that' or finishes teaching. "
        "Use run to replay a saved routine by name, list to list them, delete to remove one, and cancel to discard recording."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING", "enum": ["start", "remember", "run", "list", "delete", "cancel"]},
            "name": {"type": "STRING", "description": "Friendly name for the learned routine."},
            "speed": {"type": "NUMBER", "description": "Replay speed multiplier from 0.25 to 4."},
        },
        "required": ["mode"],
    },
    "handler": teach_jarvis,
    "tier": "core",
    "brief": "Record, remember and replay a user-demonstrated desktop routine.",
}
