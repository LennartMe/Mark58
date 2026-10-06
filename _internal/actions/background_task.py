"""
background_task — start, check, steer, finish, pause, resume and cancel work
that runs in the background (core/tasks.py), by voice.

A task keeps running while the conversation moves on, survives reconnects, and
resumes after a restart or crash. When it finishes, Jarvis says so on its own —
there is no need to poll. The TASKS panel on the HUD shows live progress.
"""
from __future__ import annotations

from core import outcome


def _pick(mgr, ref: str):
    matches = mgr.resolve(ref)
    if not matches:
        return None, outcome.failed("background_task", "no task matches that",
                                    known=", ".join(t["title"] for t in mgr.list(limit=6)) or "none")
    if len(matches) > 1 and ref:
        titles = "; ".join(f"{t['title']} ({t['status']})" for t in matches[:4])
        return None, outcome.ask("background_task", f"which task did you mean: {titles}?")
    return matches[0], ""


def background_task(parameters: dict, player=None, **_ctx) -> str:
    from core.tasks import manager, FINAL
    from core.agents import list_agents
    mgr = manager()
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    ref = str(p.get("task") or "").strip()

    try:
        if action in ("start", "create", "new", "research"):
            goal = str(p.get("goal") or "").strip()
            if not goal:
                return outcome.failed("background_task", "say what the task should do in 'goal'")
            agent = str(p.get("agent") or ("researcher" if action == "research" else "general"))
            opts = {}
            depth = str(p.get("depth") or "").strip().lower()
            if depth in ("quick", "standard", "deep"):
                opts["depth"] = depth
            t = mgr.start(goal, agent=agent, title=str(p.get("title") or ""), origin="voice",
                          options=opts)
            if player is not None and hasattr(player, "show_ops_panel"):
                try:
                    from memory.config_manager import get_background_task_settings
                    if get_background_task_settings().get("auto_show_panel", True):
                        player.show_ops_panel("tasks", True)
                except Exception:
                    pass
            return outcome.started("background_task", id=t["id"], title=t["title"],
                                   agent=t["agent"], depth=opts.get("depth") or "",
                                   note="Jarvis announces the result automatically when it finishes")

        if action in ("list", "status_all", "all"):
            rows = mgr.list(include_finished=True, limit=8)
            if not rows:
                return "No background tasks yet."
            return "Background tasks (newest active first):\n" + "\n".join(
                "- " + mgr.status_line(t) for t in rows)

        if action == "agents":
            return "Installed agents: " + "; ".join(
                f"{a.name} ({a.kind}) — {a.description}" for a in list_agents())

        if action in ("show_panel", "hide_panel"):
            if player is not None and hasattr(player, "show_ops_panel"):
                player.show_ops_panel("tasks", action == "show_panel")
                return outcome.done("background_task", panel="tasks",
                                    visible=action == "show_panel")
            return outcome.failed("background_task", "the HUD is not available")

        t, err = _pick(mgr, ref)
        if t is None:
            return err

        if action in ("status", "check", "progress"):
            tail = mgr.log_tail(t["id"], 4)
            recent = " | ".join(e.get("msg", "")[:120] for e in tail)
            return mgr.status_line(t) + (f" recent={recent!r}" if recent else "")

        if action in ("steer", "direct", "update", "redirect"):
            msg = str(p.get("message") or "").strip()
            t = mgr.steer(t["id"], msg)
            return outcome.done("background_task", steered=t["title"], direction=msg[:200],
                                note="the agent picks this up at its next step")

        if action in ("finish", "wrap_up", "stop_early", "finish_now"):
            t = mgr.finish(t["id"])
            return outcome.started("background_task", finishing=t["title"],
                                   note="it will write up what it has and report back")

        if action in ("cancel", "stop", "abort", "kill"):
            t = mgr.cancel(t["id"])
            if t.get("status") == "cancelled":
                return outcome.done("background_task", cancelled=t["title"])
            return outcome.started("background_task", cancelling=t["title"],
                                   note="it stops at its next safe point, within seconds")

        if action == "pause":
            t = mgr.pause(t["id"])
            if t.get("status") == "paused":
                return outcome.done("background_task", paused=t["title"])
            return outcome.started("background_task", pausing=t["title"],
                                   note="it pauses at its next safe point")

        if action in ("resume", "continue", "retry", "restart"):
            t = mgr.resume(t["id"])
            return outcome.done("background_task", resumed=t["title"], status=t["status"])

        if action in ("show", "open", "report", "read"):
            md = mgr.detail_markdown(t["id"])
            if player is not None and hasattr(player, "show_document"):
                player.show_document(t["title"], md)
            text = mgr.report_text(t["id"]) if t.get("status") in FINAL else ""
            extra = {"exported": t.get("exported") or ""}
            if text:
                return outcome.done("background_task", shown=t["title"], **extra) + "\n" + \
                    outcome.untrusted("report", text[:6000])
            return outcome.done("background_task", shown=t["title"], status=t["status"],
                                summary=t.get("summary", "")[:300], **extra)

        return ("Unknown action. Use start, list, status, steer, finish, cancel, pause, "
                "resume, show, agents, show_panel or hide_panel.")
    except ValueError as e:
        return outcome.failed("background_task", str(e))


TOOL = {
    "name": "background_task",
    "description": (
        "Runs work in the BACKGROUND that takes minutes rather than seconds, and manages it. "
        "Use action='start' for deep research ('research X and write it up', agent='researcher') "
        "or a multi-step job with web/files/knowledge tools (agent='general'). The task keeps "
        "running while you keep talking, survives reconnects and restarts, and you will be told "
        "automatically when it finishes — do NOT poll it. "
        "Other actions, all taking 'task' as the task's id or words from its title (empty = the "
        "current one): status (how is it going), steer (add direction mid-flight in 'message', "
        "e.g. 'focus on European prices'), finish (wrap up now with what it has), pause, resume, "
        "cancel, show (open the report / progress on the HUD), list (all tasks), agents (list "
        "installed agents), show_panel / hide_panel (the TASKS panel). "
        "For a quick answer use web_search directly instead of starting a task."
    ),
    "brief": "Start/check/steer/finish/cancel long-running background tasks (deep research, multi-step agents).",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | list | status | steer | finish | pause | resume | cancel | show | agents | show_panel | hide_panel"},
            "goal": {"type": "STRING", "description": "start: what the task must achieve, in full, in English"},
            "agent": {"type": "STRING", "description": "start: 'researcher' for research reports, 'general' otherwise, or another installed agent"},
            "depth": {"type": "STRING", "description": "start with researcher: quick | standard | deep"},
            "title": {"type": "STRING", "description": "start: optional short title"},
            "task": {"type": "STRING", "description": "which task: id, 'latest', or words from its title"},
            "message": {"type": "STRING", "description": "steer: the new direction"},
        },
        "required": ["action"],
    },
    "handler": background_task,
}
