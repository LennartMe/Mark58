"""User-facing controls for the persistent short-term working context."""
from core.context_engine import ContextEngine

def run(parameters: dict, **ctx):
    context = ContextEngine()
    p = parameters or {}
    action = str(p.get("action") or "status").lower().strip()
    if action == "status":
        block = context.prompt_block(max_chars=5000)
        return block or "No current working context has been recorded yet."
    if action == "clear":
        context.clear_recent()
        return "Recent working context cleared. Long-term personal memory was not changed."
    if action == "set_project":
        project = str(p.get("project") or "").strip()
        if not project:
            return "Provide a project name or path."
        context.set_project(project)
        return f"Active project context set to: {project}"
    return "Unknown work_context action. Use status, clear, or set_project."


TOOL = {
    "name": "work_context",
    "description": (
        "Inspect or manage JARVIS's short-term working context: active project, recent files, recent apps, recent tasks and recent tool actions. "
        "This is separate from personal long-term memory. Use status if the user asks what they were working on, set_project when they explicitly identify the current project, or clear when they ask to reset the current work context."
    ),
    "brief": "Inspect/reset current project/recent-file/app/task working context.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "status | clear | set_project"},
            "project": {"type": "STRING", "description": "Project name or path for set_project."},
        },
        "required": ["action"],
    },
    "handler": run,
}
