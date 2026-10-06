"""Smart clipboard access without persistent secret collection."""
from __future__ import annotations

from core.clipboard_intelligence import classify, get_engine, looks_sensitive
from core import outcome


def _preview(text: str, limit: int = 1500) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def run(parameters: dict, **ctx) -> str:
    p = parameters or {}
    action = str(p.get("action") or "read").strip().lower()
    eng = get_engine()
    if action in {"read", "inspect", "current"}:
        raw = eng.current()
        if not raw:
            return outcome.failed("smart_clipboard", "clipboard is empty")
        if looks_sensitive(raw):
            return outcome.failed("smart_clipboard", "clipboard content looks sensitive, so JARVIS will not expose or retain it")
        eng.observe(raw)
        return outcome.done("smart_clipboard", kind=classify(raw), text=_preview(raw), characters=len(raw))
    if action == "history":
        rows = eng.history(int(p.get("limit") or 8))
        if not rows:
            return "Clipboard history is empty. History is session-only and secret-looking values are excluded."
        lines = ["Session clipboard history (newest first; sensitive values excluded):"]
        for i, row in enumerate(rows, 1):
            one = " ".join(str(row["text"]).replace("\n", " ").split())
            lines.append(f"{i}. [{row['kind']}] {one[:240]}{'…' if len(one)>240 else ''}")
        return "\n".join(lines)
    if action in {"copy", "set"}:
        text = str(p.get("text") or "")
        if not text:
            return outcome.failed("smart_clipboard", "no text was supplied")
        if not eng.set(text):
            return outcome.failed("smart_clipboard", "the operating-system clipboard could not be written")
        return outcome.unverified("smart_clipboard", "clipboard write was requested; target application paste has not been performed", characters=len(text))
    if action in {"summarize", "explain", "rewrite", "translate", "fix"}:
        raw = eng.current()
        if not raw:
            return outcome.failed("smart_clipboard", "clipboard is empty")
        if looks_sensitive(raw):
            return outcome.failed("smart_clipboard", "clipboard content looks sensitive, so JARVIS will not send it to a model")
        eng.observe(raw)
        instruction = {
            "summarize": "Summarize the clipboard content concisely.",
            "explain": "Explain the clipboard content clearly.",
            "rewrite": "Rewrite the clipboard content more clearly while preserving meaning.",
            "translate": f"Translate the clipboard content into {str(p.get('language') or 'English')}.",
            "fix": "Fix grammar, spelling and obvious formatting in the clipboard content.",
        }[action]
        return f"{instruction}\n\n[CLIPBOARD_CONTENT]\n{_preview(raw, 5000)}"
    return "Unknown smart_clipboard action. Use read, history, copy, summarize, explain, rewrite, translate, or fix."


TOOL = {
    "name": "smart_clipboard",
    "description": (
        "Reads and manages the user's clipboard intelligently. It keeps only a session-local history, excludes secret-looking "
        "values, classifies copied text/URLs/paths/code, and can provide clipboard content for summarising, explaining, rewriting, "
        "translation or grammar correction. Use this instead of asking the user to paste something they just copied."
    ),
    "brief": "Inspect current/session clipboard safely; summarize/explain/rewrite/translate copied text.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | history | copy | summarize | explain | rewrite | translate | fix"},
            "text": {"type": "STRING", "description": "Text for copy/set."},
            "language": {"type": "STRING", "description": "Target language for translate."},
            "limit": {"type": "INTEGER", "description": "History entries, default 8."},
        },
        "required": ["action"],
    },
    "handler": run,
}
