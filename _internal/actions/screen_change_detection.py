"""Tool wrapper for visual screen-change detection."""
from __future__ import annotations

from core import outcome
from core.screen_change import check, start, stop, wait_for_change


def _region(p):
    vals = [p.get("x"), p.get("y"), p.get("width"), p.get("height")]
    if all(v is not None for v in vals):
        try:
            x, y, w, h = map(int, vals)
            if w > 0 and h > 0:
                return (x, y, w, h)
        except Exception:
            pass
    return None


def run(parameters: dict, **ctx) -> str:
    p = parameters or {}
    action = str(p.get("action") or "check").strip().lower()
    key = str(p.get("watch") or "default").strip() or "default"
    try:
        if action == "start":
            r = start(key, _region(p), float(p.get("threshold") or 0.035))
            return outcome.done("screen_change_detection", watch=key, threshold=f"{r['threshold']:.3f}", region=r.get("region") or "full_screen")
        if action == "check":
            r = check(key, bool(p.get("reset_on_change", False)))
            return outcome.done("screen_change_detection", watch=key, changed=r["changed"], score=f"{r['score']:.4f}", threshold=f"{r['threshold']:.4f}")
        if action == "wait":
            r = wait_for_change(key, float(p.get("timeout") or 30), float(p.get("poll") or 0.45), bool(p.get("reset_on_change", False)))
            if r.get("timed_out"):
                return outcome.unverified("screen_change_detection", "no visual change crossed the threshold before timeout", watch=key, score=f"{r['score']:.4f}", threshold=f"{r['threshold']:.4f}")
            return outcome.done("screen_change_detection", watch=key, changed=True, score=f"{r['score']:.4f}", threshold=f"{r['threshold']:.4f}")
        if action == "stop":
            return outcome.done("screen_change_detection", watch=key, stopped=stop(key))
        return "Unknown action. Use start, check, wait, or stop."
    except KeyError as exc:
        return outcome.failed("screen_change_detection", str(exc))
    except Exception as exc:
        return outcome.failed("screen_change_detection", f"screen capture/change detection failed: {exc}")


TOOL = {
    "name": "screen_change_detection",
    "description": (
        "Watches the whole screen or a rectangular region for meaningful visual change without saving screenshots. "
        "Use start before an operation, then wait/check to know when a page, dialog, installer, render, download UI, or app state visibly changes. "
        "This is useful when DOM/accessibility verification is unavailable."
    ),
    "brief": "Start/check/wait for a visual screen-region change; useful for UI completion verification.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | check | wait | stop"},
            "watch": {"type": "STRING", "description": "Watch name, default 'default'."},
            "x": {"type": "INTEGER"}, "y": {"type": "INTEGER"},
            "width": {"type": "INTEGER"}, "height": {"type": "INTEGER"},
            "threshold": {"type": "NUMBER", "description": "Visual difference threshold 0.005-0.5, default 0.035."},
            "timeout": {"type": "NUMBER", "description": "Wait timeout seconds, max 120."},
            "poll": {"type": "NUMBER", "description": "Wait polling interval seconds."},
            "reset_on_change": {"type": "BOOLEAN", "description": "Replace baseline after a detected change."},
        },
        "required": ["action"],
    },
    "handler": run,
}
