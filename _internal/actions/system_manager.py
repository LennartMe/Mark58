"""
actions/system_manager.py — what is running, and what to do about it.

WHAT THIS COVERS THAT open_app AND computer_control DO NOT
    `open_app` starts things. `computer_control` with action='close_app' closes
    a NAMED thing. Neither can answer the questions that come up in between:

        "what's actually running right now?"
        "what's eating my CPU?"
        "is Photoshop still open?"
        "close whatever is chewing the battery"

    Those need an inventory, and there was none. Without it the assistant
    answers from the screen — it can only report windows it can see — so a
    background process pinning a core is invisible to it.

THE GROUPING PROBLEM, AND WHY THE ANSWER IS NOT A PROCESS LIST
    A modern browser is forty processes. A raw psutil dump is forty rows of
    `chrome.exe`, and handing that to a language model produces "you have
    forty Chrome windows open", which is false, or a request to kill PID 8842,
    which is meaningless to the user.

    So processes are folded into APPLICATIONS by executable name, their CPU and
    memory summed, and the group is what gets named and acted on. "Close Chrome"
    then means the whole tree, which is what the user meant.

WHAT IS GUARDED, AND WHY THAT LIST IS SHORT
    Terminating a process is not reversible — an unsaved document is gone — so
    by the rule in core/confirm.py it belongs behind the gate. But gating
    everything makes the assistant useless: nobody wants a banner to close
    Notepad.

    The split drawn here:
      - a process the user NAMED, that is an ordinary application → just close it
      - anything on the critical list, or a process the user did not name (a
        "close the thing using the most CPU" request), or more than 3 groups at
        once → the confirm banner

    The critical list is not a security boundary. It is a list of things whose
    termination breaks the session in a way that is confusing rather than
    obvious — the window server, the shell, the audio stack, and this assistant
    itself. Everything on it is refused outright rather than gated, because
    there is no user intent that is served by the assistant killing the
    compositor it is drawing on.
"""
from __future__ import annotations

import os
import platform
import sys
import time
from typing import Optional

from core import outcome

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_OS = platform.system().lower()


# Killing these does not do what anybody wanted. Matched case-insensitively
# against the executable name, exactly — not as a substring, so a user's
# `explorer_backup.exe` is not caught by `explorer.exe`.
_CRITICAL = {
    "windows": {
        "system", "system idle process", "registry", "smss.exe", "csrss.exe",
        "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
        "dwm.exe", "explorer.exe", "fontdrvhost.exe", "audiodg.exe",
        "ctfmon.exe", "sihost.exe", "memory compression",
    },
    "darwin": {
        "kernel_task", "launchd", "windowserver", "loginwindow", "systemuiserver",
        "dock", "finder", "coreaudiod", "hidd", "opendirectoryd", "securityd",
        "mds", "mds_stores", "distnoted", "cfprefsd",
    },
    "linux": {
        "systemd", "init", "kthreadd", "dbus-daemon", "xorg", "xwayland",
        "gnome-shell", "kwin_x11", "kwin_wayland", "pulseaudio", "pipewire",
        "wireplumber", "sshd", "udevd", "polkitd", "logind",
    },
}


def _critical_names() -> set[str]:
    if _OS.startswith("win"):
        return _CRITICAL["windows"]
    if _OS == "darwin":
        return _CRITICAL["darwin"]
    return _CRITICAL["linux"]


def _self_pids() -> set[int]:
    """This process and its parent, so the assistant cannot close itself.

    It has tried. A "close the app using the most memory" request on a quiet
    machine finds Python, and Python is the assistant.
    """
    pids = {os.getpid()}
    try:
        pids.add(os.getppid())
        if _PSUTIL:
            for child in psutil.Process(os.getpid()).children(recursive=True):
                pids.add(child.pid)
    except Exception:
        pass
    return pids


def _pretty(exe: str) -> str:
    """A name a person would say, from an executable name."""
    stem = exe.rsplit(".", 1)[0] if exe.lower().endswith(".exe") else exe
    return stem.replace("_", " ").replace("-", " ").strip().title() or exe


def inventory(min_memory_mb: float = 0.0) -> list[dict]:
    """Running applications, grouped by executable, heaviest CPU first.

    CPU is sampled over a short interval rather than read once. psutil's first
    cpu_percent() call for a process always returns 0.0 — it has nothing to
    compare against — so a single pass reports every process as idle, which is
    the sort of confidently-wrong answer this project keeps having to design
    against.
    """
    if not _PSUTIL:
        return []

    mine = _self_pids()
    critical = _critical_names()

    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
        try:
            proc.cpu_percent(None)          # prime the counter
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(0.35)                         # the interval the sample is taken over

    groups: dict[str, dict] = {}
    cores = psutil.cpu_count() or 1
    for proc in procs:
        try:
            info = proc.info
            name = (info.get("name") or "").strip()
            if not name:
                continue
            cpu = proc.cpu_percent(None) / cores
            mem = (info.get("memory_info").rss / (1024 * 1024)) if info.get("memory_info") else 0.0
            key = name.lower()
            group = groups.setdefault(key, {
                "exe": name, "label": _pretty(name), "pids": [], "cpu": 0.0,
                "memory_mb": 0.0, "count": 0, "since": info.get("create_time") or 0,
                "critical": key in critical, "is_self": False,
            })
            group["pids"].append(info["pid"])
            group["cpu"] += cpu
            group["memory_mb"] += mem
            group["count"] += 1
            if info["pid"] in mine:
                group["is_self"] = True
            if info.get("create_time") and info["create_time"] < group["since"]:
                group["since"] = info["create_time"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue

    out = []
    for group in groups.values():
        group["cpu"] = round(group["cpu"], 1)
        group["memory_mb"] = round(group["memory_mb"], 1)
        if group["memory_mb"] < min_memory_mb:
            continue
        out.append(group)
    out.sort(key=lambda g: (g["cpu"], g["memory_mb"]), reverse=True)
    return out


def system_snapshot() -> dict:
    """Overall load. Cheap enough for the UI to poll every couple of seconds."""
    if not _PSUTIL:
        return {}
    try:
        mem = psutil.virtual_memory()
        snap = {
            "cpu_percent": psutil.cpu_percent(interval=0.15),
            "memory_percent": mem.percent,
            "memory_used_gb": round(mem.used / (1024 ** 3), 1),
            "memory_total_gb": round(mem.total / (1024 ** 3), 1),
            "process_count": len(psutil.pids()),
            "boot_time": psutil.boot_time(),
        }
        try:
            disk = psutil.disk_usage(os.path.abspath(os.sep))
            snap["disk_percent"] = disk.percent
            snap["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
        except Exception:
            pass
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                snap["battery_percent"] = round(battery.percent)
                snap["on_ac"] = bool(battery.power_plugged)
        except Exception:
            pass
        return snap
    except Exception:
        return {}


def _match(query: str) -> list[dict]:
    """Groups matching what the user said, best first."""
    text = str(query or "").strip().lower()
    if not text:
        return []
    stem = text.rsplit(".", 1)[0]
    groups = inventory()
    exact = [g for g in groups if g["exe"].lower() in (text, text + ".exe")
             or g["label"].lower() == text]
    if exact:
        return exact
    return [g for g in groups
            if stem in g["exe"].lower() or stem in g["label"].lower()]


def _terminate(group: dict, force: bool = False) -> tuple[bool, str]:
    """Ask a process group to go, then check whether it went.

    Polite first (terminate/SIGTERM), because an application that is asked
    nicely gets to flush its files. Escalation to kill only happens on an
    explicit force, and even then only after the polite request has been given
    time to work.
    """
    if not _PSUTIL:
        return False, "psutil_not_installed"
    procs = []
    for pid in group["pids"]:
        try:
            procs.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            continue
    if not procs:
        return True, "already_gone"

    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(procs, timeout=4.0)
    if not alive:
        return True, "closed"

    if not force:
        return False, f"{len(alive)}_still_running"

    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    gone, alive = psutil.wait_procs(alive, timeout=3.0)
    return (not alive), ("killed" if not alive else f"{len(alive)}_survived_kill")


def _focus(query: str) -> tuple[bool, str]:
    try:
        import pygetwindow as gw
    except Exception:
        return False, "pygetwindow_not_installed"
    text = str(query).strip().lower()
    try:
        wins = [w for w in gw.getAllWindows()
                if text in (w.title or "").lower() and w.width > 0]
    except Exception as e:
        return False, f"window_query_failed:{e}"
    if not wins:
        return False, "no_window"
    win = wins[0]
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
    except Exception:
        # activate() is flaky on Windows when the calling process is not
        # foreground. It usually still works; the caller reports unverified.
        return False, "activate_refused"
    return True, (win.title or "")[:80]


# ── The tool ──────────────────────────────────────────────────────────────────

MAX_BULK_CLOSE = 3


def system_manage(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "list")).strip().lower()
    target = str(parameters.get("app_name", "")).strip()
    force = str(parameters.get("force", "")).strip().lower() in ("true", "yes", "1")
    limit = parameters.get("limit")

    if not _PSUTIL:
        return outcome.failed("system_manage", "psutil_not_installed",
                              fix="pip install psutil")

    try:
        if action in ("list", "running", "processes", "apps"):
            top = inventory()
            count = 8
            try:
                if limit:
                    count = max(1, min(int(limit), 25))
            except (TypeError, ValueError):
                pass
            visible = [g for g in top if not g["critical"]][:count]
            facts = {}
            for idx, group in enumerate(visible, start=1):
                facts[f"app_{idx}"] = (f"{group['label']} cpu={group['cpu']}% "
                                       f"mem={group['memory_mb']:.0f}MB")
            snap = system_snapshot()
            return outcome.done("system_manage", shown=len(visible),
                                total_apps=len(top),
                                cpu=f"{snap.get('cpu_percent', 0)}%",
                                memory=f"{snap.get('memory_percent', 0)}%", **facts)

        if action in ("status", "load", "health"):
            snap = system_snapshot()
            if not snap:
                return outcome.failed("system_manage", "snapshot_unavailable")
            return outcome.done("system_manage", **{
                k: v for k, v in snap.items() if k != "boot_time"})

        if action in ("running", "is_running", "check"):
            if not target:
                return outcome.ask("system_manage", "which_application", action=action)
            found = _match(target)
            if not found:
                return outcome.done("system_manage", app=target, running=False)
            group = found[0]
            return outcome.done("system_manage", app=group["label"], running=True,
                                processes=group["count"], cpu=f"{group['cpu']}%",
                                memory=f"{group['memory_mb']:.0f}MB")

        if action in ("focus", "switch", "activate", "bring_to_front"):
            if not target:
                return outcome.ask("system_manage", "which_application", action=action)
            ok, detail = _focus(target)
            if ok:
                return outcome.done("system_manage", focused=target, window=detail)
            if detail in ("no_window",):
                return outcome.failed("system_manage", "no_matching_window", app=target)
            return outcome.unverified("system_manage", detail, app=target)

        if action in ("heaviest", "top", "hogs"):
            top = [g for g in inventory() if not g["critical"] and not g["is_self"]][:5]
            if not top:
                return outcome.failed("system_manage", "nothing_measurable")
            facts = {f"app_{i}": f"{g['label']} cpu={g['cpu']}% mem={g['memory_mb']:.0f}MB"
                     for i, g in enumerate(top, start=1)}
            return outcome.done("system_manage", **facts)

        if action in ("close", "quit", "terminate", "kill", "end"):
            if not target:
                # A close request with no name is the dangerous one: it happens
                # when the model inferred a target from context it may have read
                # wrong. There is no "close whatever" here, on purpose.
                return outcome.ask(
                    "system_manage",
                    "which_application_should_be_closed", action=action)

            found = _match(target)
            if not found:
                return outcome.failed("system_manage", "not_running", app=target)

            blocked = [g for g in found if g["critical"] or g["is_self"]]
            if blocked:
                reason = ("would_close_the_assistant" if blocked[0]["is_self"]
                          else "system_critical_process")
                return outcome.failed("system_manage", reason,
                                      app=blocked[0]["label"])

            if len(found) > MAX_BULK_CLOSE:
                return outcome.ask(
                    "system_manage",
                    f"that_matches_{len(found)}_applications_which_one",
                    matches=", ".join(g["label"] for g in found[:6]))

            group = found[0]
            ok, detail = _terminate(group, force=force)
            if ok and detail == "already_gone":
                return outcome.done("system_manage", app=group["label"], closed=True,
                                    note="was_not_running")
            if ok:
                return outcome.done("system_manage", app=group["label"], closed=True,
                                    processes=group["count"], method=detail)
            return outcome.unverified(
                "system_manage", detail, app=group["label"],
                hint="it may be showing a save dialog; pass force=true to insist")

        return outcome.failed("system_manage", "unknown_action", action=action)

    except Exception as e:
        return outcome.failed("system_manage", f"{type(e).__name__}:{e}", action=action)


TOOL = {
    "name": "system_manage",
    "description": (
        "Inventory and control of everything running on this computer. "
        "action='list' names the running applications with their CPU and memory "
        "— use it for 'what's running', 'what's open'. "
        "action='status' gives overall CPU, memory, disk and battery. "
        "action='heaviest' finds what is using the most CPU — 'what's slowing my "
        "computer down', 'what's draining the battery'. "
        "action='check' with app_name says whether one application is running. "
        "action='focus' with app_name brings a window to the front. "
        "action='close' with app_name closes an application and all its "
        "processes, then CHECKS whether it went. "
        "Always name the application in app_name; there is no action that closes "
        "an unnamed application. "
        "For simply launching an application use open_app. For typing and "
        "clicking use computer_control. "
        "Read the status returned: 'done' was confirmed, 'unverified' means the "
        "request went out and could not be confirmed — say exactly that rather "
        "than claiming success — and 'ask' means nothing happened and the "
        "question goes to the user."
    ),
    "brief": (
        "Lists running apps with CPU/memory, reports system load, finds resource "
        "hogs, focuses or closes a named application."
    ),
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | status | heaviest | check | focus | close",
            },
            "app_name": {
                "type": "STRING",
                "description": (
                    "The application, named as the user said it. Required for "
                    "check, focus and close. Never leave empty for those."
                ),
            },
            "force": {
                "type": "BOOLEAN",
                "description": (
                    "For close: escalate to a hard kill if the application does "
                    "not respond to a polite request. Only set this after a "
                    "normal close came back unverified AND the user asked again "
                    "— a hard kill loses unsaved work."
                ),
            },
            "limit": {
                "type": "STRING",
                "description": "For list: how many applications to name. Default 8.",
            },
        },
        "required": ["action"],
    },
    "handler": system_manage,
}
