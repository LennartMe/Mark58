"""On-demand system awareness and slowdown diagnosis."""
from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

import psutil

from actions.system_monitor import get_system_status
from core.context_engine import foreground_window


def _top_processes() -> list[dict]:
    procs=[]
    for p in psutil.process_iter(["pid","name","memory_percent"]):
        try:
            p.cpu_percent(None); procs.append(p)
        except Exception: pass
    time.sleep(.12)
    rows=[]
    for p in procs:
        try:
            rows.append({"pid":p.pid,"name":p.name(),"cpu":round(p.cpu_percent(None),1),"memory":round(p.memory_percent(),1)})
        except Exception: pass
    rows.sort(key=lambda x:(x["cpu"]*1.5+x["memory"]), reverse=True)
    return rows[:12]


def _disks():
    rows=[]
    for part in psutil.disk_partitions(all=False):
        try:
            u=psutil.disk_usage(part.mountpoint)
            rows.append({"mount":part.mountpoint,"used_percent":round(u.percent,1),"free_gb":round(u.free/1024**3,1)})
        except Exception: pass
    return rows


def _network():
    out=[]
    stats=psutil.net_if_stats()
    for name,st in stats.items():
        if st.isup and not name.casefold().startswith(("loopback","lo")):
            out.append(name)
    return out[:12]


def _audio():
    try:
        from memory.config_manager import get_input_device, get_output_device
        from core.audio_devices import list_devices
        saved_in=get_input_device(); saved_out=get_output_device()
        ins=list_devices("input"); outs=list_devices("output")
        return {
            "input": saved_in or "system default", "output": saved_out or "system default",
            "input_available": (not saved_in) or saved_in in ins,
            "output_available": (not saved_out) or saved_out in outs,
            "input_count": len(ins), "output_count": len(outs),
        }
    except Exception as e:
        return {"error":str(e)}


def _windows():
    try:
        from actions.app_control import _list_windows
        return _list_windows()
    except Exception:
        return "Window list unavailable."


def _snapshot() -> dict:
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    return {
        "system": get_system_status(),
        "foreground": foreground_window(),
        "battery": None if b is None else {"percent":round(b.percent,1),"plugged":bool(b.power_plugged)},
        "disks": _disks(),
        "network_interfaces_up": _network(),
        "audio": _audio(),
        "top_processes": _top_processes(),
    }


def _diagnose(s: dict) -> str:
    sys=s["system"]; findings=[]
    if float(sys.get("cpu_percent") or 0)>=80: findings.append(f"CPU is heavily loaded at {sys['cpu_percent']}%.")
    if float(sys.get("ram_percent") or 0)>=85: findings.append(f"Memory pressure is high at {sys['ram_percent']}%.")
    hot=sys.get("cpu_temp_c")
    if hot is not None and hot>=85: findings.append(f"CPU temperature is high at {hot}°C.")
    for d in s.get("disks") or []:
        if d["used_percent"]>=92: findings.append(f"Disk {d['mount']} is almost full ({d['free_gb']} GB free).")
    audio=s.get("audio") or {}
    if audio.get("input_available") is False: findings.append(f"Configured microphone is disconnected: {audio.get('input')}.")
    if audio.get("output_available") is False: findings.append(f"Configured speaker is disconnected: {audio.get('output')}.")
    if not s.get("network_interfaces_up"): findings.append("No active non-loopback network interface was detected.")
    top=s.get("top_processes") or []
    if top:
        p=top[0]
        if p["cpu"]>=35 or p["memory"]>=20: findings.append(f"Top resource user is {p['name']} (CPU {p['cpu']}%, RAM {p['memory']}%).")
    if not findings: findings.append("No obvious CPU, memory, disk, temperature, audio-device or network bottleneck is visible right now.")
    return "System awareness diagnosis:\n- " + "\n- ".join(findings) + "\nTop processes:\n" + "\n".join(f"- {p['name']} — CPU {p['cpu']}% · RAM {p['memory']}%" for p in top[:8])


def run(parameters: dict, **ctx) -> str:
    action=str((parameters or {}).get("action") or "status").strip().lower()
    if action in {"apps","windows"}: return _windows()
    snap=_snapshot()
    if action in {"diagnose","slow","performance"}: return _diagnose(snap)
    if action in {"devices","hardware"}:
        return json.dumps({"audio":snap["audio"],"battery":snap["battery"],"network_interfaces_up":snap["network_interfaces_up"],"disks":snap["disks"]},indent=2)
    return "System awareness snapshot:\n" + json.dumps(snap,indent=2)


TOOL={
    "name":"system_awareness",
    "description":(
        "Inspect what is happening on the PC right now: foreground/open apps, CPU/RAM/GPU/temperature, top resource-using processes, battery, disks, network and whether configured audio devices are still present. "
        "Use diagnose when the user asks why the PC is slow, laggy, hot, disconnected, or behaving oddly. This tool only observes; it does not change the system."
    ),
    "tier":"core",
    "brief":"Inspect apps/devices/resources and diagnose PC slowdowns or disconnected hardware.",
    "parameters":{"type":"OBJECT","properties":{"action":{"type":"STRING","description":"status | diagnose | apps | devices"}},"required":["action"]},
    "handler":run,
}
