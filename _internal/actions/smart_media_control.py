"""Smart media control across system and per-app audio."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any

from core import outcome

_OS = platform.system()


def _media_key(name: str) -> bool:
    """Send a media key without needing the target application focused."""
    if _OS == "Windows":
        try:
            import ctypes
            mapping = {
                "play_pause": 0xB3,
                "stop": 0xB2,
                "next": 0xB0,
                "previous": 0xB1,
                "mute": 0xAD,
                "volume_down": 0xAE,
                "volume_up": 0xAF,
            }
            vk = mapping[name]
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            return True
        except Exception:
            return False
    if _OS == "Darwin":
        # Standard media key simulation is awkward without extra permissions;
        # use AppleScript for the two apps where it is well-defined.
        return False
    if shutil.which("playerctl"):
        cmd = {
            "play_pause": ["playerctl", "play-pause"],
            "stop": ["playerctl", "stop"],
            "next": ["playerctl", "next"],
            "previous": ["playerctl", "previous"],
        }.get(name)
        if cmd:
            try:
                return subprocess.run(cmd, timeout=3, capture_output=True).returncode == 0
            except Exception:
                return False
    return False


def _win_sessions() -> list[dict[str, Any]]:
    if _OS != "Windows":
        return []
    try:
        from pycaw.pycaw import AudioUtilities
        rows = []
        for s in AudioUtilities.GetAllSessions():
            proc = getattr(s, "Process", None)
            name = ""
            pid = 0
            try:
                if proc:
                    name = proc.name()
                    pid = int(proc.pid)
            except Exception:
                pass
            if not name:
                try:
                    name = str(getattr(s, "DisplayName", "") or "")
                except Exception:
                    pass
            vol = getattr(s, "SimpleAudioVolume", None)
            level = muted = None
            if vol is not None:
                try:
                    level = round(float(vol.GetMasterVolume()) * 100)
                    muted = bool(vol.GetMute())
                except Exception:
                    pass
            rows.append({"name": name, "pid": pid, "volume": level, "muted": muted, "session": s})
        return rows
    except Exception:
        return []


def _matches(app: str, name: str) -> bool:
    a = str(app or "").casefold().replace(".exe", "").strip()
    n = str(name or "").casefold().replace(".exe", "").strip()
    return bool(a and (a in n or n in a))


def _set_app_volume(app: str, percent: int | None = None, mute: bool | None = None) -> str:
    sessions = [r for r in _win_sessions() if _matches(app, r.get("name", ""))]
    if not sessions:
        return outcome.failed("smart_media_control", f"no active Windows audio session matched '{app}'", app=app)
    changed = 0
    for row in sessions:
        try:
            vol = row["session"].SimpleAudioVolume
            if percent is not None:
                vol.SetMasterVolume(max(0, min(100, int(percent))) / 100.0, None)
            if mute is not None:
                vol.SetMute(1 if mute else 0, None)
            changed += 1
        except Exception:
            pass
    if not changed:
        return outcome.failed("smart_media_control", f"matched '{app}' but could not change its audio session", app=app)
    # Read back first matching session to verify.
    verify = [r for r in _win_sessions() if _matches(app, r.get("name", ""))]
    facts = verify[0] if verify else {}
    return outcome.done("smart_media_control", app=app, sessions=changed,
                        volume=facts.get("volume"), muted=facts.get("muted"))


def _list_sessions() -> str:
    rows = [r for r in _win_sessions() if r.get("name")]
    if not rows:
        return "No per-app audio sessions are currently visible."
    seen = set(); lines = ["Active audio sessions:"]
    for row in rows:
        key = str(row["name"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {row['name']}: volume={row.get('volume')}% muted={row.get('muted')}")
    return "\n".join(lines[:30])


def run(parameters: dict, **ctx) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    app = str(p.get("app") or "").strip()

    if action in {"play_pause", "pause", "play"}:
        # System media transport sends a toggle. For explicit play/pause on Linux
        # playerctl can be exact; on Windows the OS media key is the reliable
        # cross-player mechanism and is honestly reported as unverified.
        if _OS != "Windows" and shutil.which("playerctl") and action in {"pause", "play"}:
            try:
                r = subprocess.run(["playerctl", action], timeout=3, capture_output=True)
                return outcome.done("smart_media_control", command=action) if r.returncode == 0 else outcome.failed("smart_media_control", r.stderr.decode(errors="ignore") or "playerctl failed")
            except Exception as exc:
                return outcome.failed("smart_media_control", str(exc))
        if _media_key("play_pause"):
            return outcome.unverified("smart_media_control", "system media play/pause key was sent; individual player state is not readable through this transport", command=action)
        return outcome.failed("smart_media_control", "system media transport key could not be sent")

    if action in {"next", "previous", "stop"}:
        if _media_key(action):
            return outcome.unverified("smart_media_control", f"system media {action} key was sent; player state was not read back", command=action)
        return outcome.failed("smart_media_control", f"could not send the {action} media command")

    if action in {"volume_up", "volume_down", "mute"} and not app:
        key = action
        if _media_key(key):
            return outcome.unverified("smart_media_control", f"system {action} key was sent", command=action)
        return outcome.failed("smart_media_control", f"could not send system {action}")

    if action in {"set_volume", "app_volume"}:
        if not app:
            try:
                from actions.computer_settings import volume_set, volume_get
                wanted = max(0, min(100, int(p.get("volume") or 0)))
                volume_set(wanted)
                now = volume_get()
                return outcome.done("smart_media_control", target="system", volume=now) if now is not None else outcome.unverified("smart_media_control", "system volume set was requested but could not be read back", volume=wanted)
            except Exception as exc:
                return outcome.failed("smart_media_control", f"could not set system volume: {exc}")
        return _set_app_volume(app, percent=int(p.get("volume") or 0))

    if action in {"mute_app", "unmute_app"}:
        if not app:
            return outcome.failed("smart_media_control", "name the application to mute or unmute")
        return _set_app_volume(app, mute=(action == "mute_app"))

    if action in {"status", "list", "list_sessions"}:
        return _list_sessions()

    return "Unknown smart_media_control action. Use play_pause, play, pause, next, previous, stop, set_volume, volume_up, volume_down, mute, mute_app, unmute_app, or status."


TOOL = {
    "name": "smart_media_control",
    "description": (
        "Controls media playback and audio intelligently. It can send system play/pause/next/previous/stop controls, change master volume, "
        "and on Windows read/set/mute individual application audio sessions such as Spotify, Discord, browsers and media players. "
        "Use app= when the user wants one program changed without changing everything else."
    ),
    "brief": "Media transport plus system/per-app volume and mute controls.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "play_pause | play | pause | next | previous | stop | set_volume | volume_up | volume_down | mute | mute_app | unmute_app | status"},
            "app": {"type": "STRING", "description": "Optional application name for per-app audio control."},
            "volume": {"type": "INTEGER", "description": "Volume 0-100 for set_volume."},
        },
        "required": ["action"],
    },
    "handler": run,
}
