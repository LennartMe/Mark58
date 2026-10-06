"""
routines — create and manage scheduled routines and quiet hours by voice.

One scheduler (core/routines.py) runs everything time-based: routines,
reminders and startup/idle behaviours. This is its voice surface; the
ROUTINES panel on the HUD is its visual one.
"""
from __future__ import annotations

from core import outcome


def _pick(eng, ref: str):
    rows = eng.find(ref)
    if not rows:
        names = ", ".join(r["name"] for r in eng.list(include_reminders=False)[:8]) or "none"
        return None, outcome.failed("routines", "no routine matches that", known=names)
    if len(rows) > 1:
        return None, outcome.ask("routines", "which one did you mean: "
                                 + "; ".join(r["name"] for r in rows[:4]) + "?")
    return rows[0], ""


def routines(parameters: dict, player=None, **_ctx) -> str:
    from core.routines import (engine, RoutineError, describe_trigger, describe_action,
                               quiet_hours_describe)
    eng = engine()
    p = dict(parameters or {})
    action = str(p.get("action") or "list").strip().lower()
    ref = str(p.get("routine") or "").strip()

    try:
        if action in ("create", "add", "new", "schedule"):
            r = eng.create(p)
            if player is not None and hasattr(player, "show_ops_panel"):
                try:
                    player.show_ops_panel("routines", True)
                except Exception:
                    pass
            return outcome.done("routines", created=r["name"], id=r["id"],
                                schedule=describe_trigger(r), does=describe_action(r),
                                next_run=r.get("next_run") or "on its trigger")

        if action in ("list", "status"):
            rows = eng.list(include_reminders=True)
            live = [r for r in rows if not r.get("fired")]
            if not live:
                return "No routines or pending reminders. " + quiet_hours_describe() + "."
            lines = [f"- {r['name']} [{r['id']}] — {describe_trigger(r)}; {describe_action(r)}; "
                     f"{'enabled' if r.get('enabled') else 'disabled'}"
                     + (f"; next {r['next_run']}" if r.get("next_run") and r.get("enabled") else "")
                     + (f"; last {r['last_status']}" if r.get("last_status") else "")
                     for r in live[:20]]
            return "Routines:\n" + "\n".join(lines) + "\n" + quiet_hours_describe() + "."

        if action in ("quiet_hours", "quiet"):
            from memory.config_manager import save_quiet_hours
            enabled = p.get("enabled")
            if enabled is None and (p.get("quiet_start") or p.get("quiet_end")):
                enabled = True
            cfg = save_quiet_hours(enabled=enabled, start=p.get("quiet_start"),
                                   end=p.get("quiet_end"),
                                   allow_reminders=p.get("allow_reminders"))
            return outcome.done("routines", quiet_hours="on" if cfg["enabled"] else "off",
                                start=cfg["start"], end=cfg["end"],
                                reminders_still_spoken=cfg["allow_reminders"],
                                now=quiet_hours_describe(cfg))

        if action in ("show_panel", "hide_panel"):
            if player is not None and hasattr(player, "show_ops_panel"):
                player.show_ops_panel("routines", action == "show_panel")
                return outcome.done("routines", panel="routines", visible=action == "show_panel")
            return outcome.failed("routines", "the HUD is not available")

        r, err = _pick(eng, ref or str(p.get("name") or ""))
        if r is None:
            return err

        if action in ("enable", "on", "resume"):
            r = eng.set_enabled(r["id"], True)
            return outcome.done("routines", enabled=r["name"], next_run=r.get("next_run") or "")
        if action in ("disable", "off", "pause"):
            r = eng.set_enabled(r["id"], False)
            return outcome.done("routines", disabled=r["name"])
        if action in ("delete", "remove"):
            gone = eng.delete(r["id"])
            if gone and gone.get("os_task"):
                try:
                    from actions.reminder import cancel_os_task
                    cancel_os_task(gone["os_task"])
                except Exception:
                    pass
            return outcome.done("routines", deleted=r["name"])
        if action in ("run", "run_now", "test", "trigger"):
            ok = eng.run_now(r["id"])
            return outcome.started("routines", running=r["name"]) if ok else \
                outcome.failed("routines", "it is already running")
        if action in ("update", "edit", "change"):
            changes = {k: v for k, v in p.items() if k not in ("action", "routine")}
            if "every_minutes" in changes:
                changes["minutes"] = changes.pop("every_minutes")
            if "day_of_month" in changes:
                changes["day"] = changes.pop("day_of_month")
            r2 = eng.update(r["id"], changes)
            return outcome.done("routines", updated=r2["name"], schedule=describe_trigger(r2),
                                does=describe_action(r2), next_run=r2.get("next_run") or "")
        if action in ("show", "detail", "details"):
            if player is not None and hasattr(player, "show_document"):
                player.show_document(r["name"], eng.detail_markdown(r["id"]))
            return outcome.done("routines", shown=r["name"], schedule=describe_trigger(r),
                                does=describe_action(r))
        return ("Unknown action. Use create, list, enable, disable, delete, run_now, update, "
                "show, quiet_hours, show_panel or hide_panel.")
    except RoutineError as e:
        return outcome.failed("routines", str(e))
    except ValueError as e:
        return outcome.failed("routines", str(e))


TOOL = {
    "name": "routines",
    "description": (
        "Creates and manages ROUTINES — things Jarvis does on a schedule or trigger — and QUIET "
        "HOURS. Schedules: daily (time, optional days e.g. 'weekdays'), weekly (days + time), "
        "monthly (day_of_month 1-31 or 'last' + time), interval (every_minutes, optional "
        "between '09:00-17:00'), startup (when Jarvis starts), idle (idle_minutes away from the "
        "keyboard), once (at 'YYYY-MM-DD HH:MM'). What it does ('do'): speak (say 'text'), "
        "prompt (carry out 'text' as if the user just asked — may use tools), agent (start a "
        "background task: 'goal', 'agent' researcher/general), teach (replay a Teach Mode "
        "recording named 'teach_name'). Examples: 'every weekday at 8 give me the weather' → "
        "create schedule=daily days=weekdays time=08:00 do=prompt text='Tell me today's weather'; "
        "'every Sunday night research AI news' → schedule=weekly days=sun time=21:00 do=agent "
        "agent=researcher goal=...; 'be quiet between 11pm and 7am' → action=quiet_hours "
        "quiet_start=23:00 quiet_end=07:00. For a single one-off reminder use the reminder tool "
        "instead. Other actions take 'routine' (id or name words): list, enable, disable, delete, "
        "run_now, update, show; show_panel/hide_panel toggle the ROUTINES panel."
    ),
    "brief": "Scheduled routines (daily/weekly/monthly/interval/startup/idle) and quiet hours.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create | list | enable | disable | delete | run_now | update | show | quiet_hours | show_panel | hide_panel"},
            "routine": {"type": "STRING", "description": "existing routine: id or words from its name"},
            "name": {"type": "STRING", "description": "create: short name for the routine"},
            "schedule": {"type": "STRING", "description": "daily | weekly | monthly | interval | startup | idle | once"},
            "time": {"type": "STRING", "description": "HH:MM 24h for daily/weekly/monthly"},
            "days": {"type": "STRING", "description": "e.g. 'weekdays', 'weekends', 'mon,wed,fri'"},
            "day_of_month": {"type": "STRING", "description": "monthly: 1-31 or 'last'"},
            "every_minutes": {"type": "INTEGER", "description": "interval: minutes between runs"},
            "between": {"type": "STRING", "description": "interval: optional active window 'HH:MM-HH:MM'"},
            "idle_minutes": {"type": "INTEGER", "description": "idle: minutes of inactivity"},
            "at": {"type": "STRING", "description": "once: 'YYYY-MM-DD HH:MM'"},
            "do": {"type": "STRING", "description": "speak | prompt | agent | teach"},
            "text": {"type": "STRING", "description": "speak/prompt: what to say or do"},
            "goal": {"type": "STRING", "description": "agent: the task goal"},
            "agent": {"type": "STRING", "description": "agent: researcher | general"},
            "teach_name": {"type": "STRING", "description": "teach: the Teach Mode recording to replay"},
            "respect_quiet_hours": {"type": "BOOLEAN", "description": "default true; false lets it speak during quiet hours"},
            "catch_up": {"type": "BOOLEAN", "description": "run once at the next launch if its time passed while Jarvis was closed (within 12 h); default false"},
            "enabled": {"type": "BOOLEAN", "description": "create/update/quiet_hours: on or off"},
            "quiet_start": {"type": "STRING", "description": "quiet_hours: start HH:MM"},
            "quiet_end": {"type": "STRING", "description": "quiet_hours: end HH:MM"},
            "allow_reminders": {"type": "BOOLEAN", "description": "quiet_hours: still speak reminders (default true)"},
        },
        "required": ["action"],
    },
    "handler": routines,
}
