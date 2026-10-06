"""Native desktop application control through the accessibility tree.

This is the bridge between "I can press keys" and "I can use the application".
On Windows it uses UI Automation via pywinauto, so JARVIS can inspect named
controls, click buttons, fill edit fields, select items and read a native app
without relying on screen coordinates.  Blind keyboard/mouse control remains in
computer_control as the fallback when an app exposes no accessibility tree.
"""
from __future__ import annotations

import json
import platform
import time
from difflib import SequenceMatcher
from typing import Any

from core import appidentity, outcome

_OS = platform.system()


def _norm(s: Any) -> str:
    return " ".join(str(s or "").split()).casefold()


def _active_hwnd() -> int | None:
    if _OS != "Windows":
        return None
    try:
        import ctypes
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        return hwnd or None
    except Exception:
        return None


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _window_for(target: str = ""):
    """Return a UIA window wrapper and a short target description."""
    if _OS != "Windows":
        raise RuntimeError("native accessibility control is currently available on Windows")

    if not str(target or "").strip():
        hwnd = _active_hwnd()
        if not hwnd:
            raise RuntimeError("no active window is available")
        return _desktop().window(handle=hwnd), "active window"

    matches = appidentity.running(str(target))
    usable = [m for m in matches if not getattr(m, "is_self", False) and getattr(m, "titles", None)]
    if not usable:
        raise RuntimeError(f"no visible running application matched '{target}'")
    best = usable[0]
    try:
        appidentity.focus(best)
    except Exception:
        pass
    time.sleep(0.10)
    # A process may own several windows. Prefer the foreground window after
    # focus(), otherwise the first titled handle exposed by the process scan.
    hwnd = _active_hwnd()
    if hwnd:
        return _desktop().window(handle=hwnd), getattr(best, "label", target)
    try:
        handles = list(appidentity._hwnds_for(int(best.pid)))
    except Exception:
        handles = []
    if handles:
        return _desktop().window(handle=int(handles[0])), getattr(best, "label", target)
    raise RuntimeError(f"'{target}' is running but no accessible window handle was found")


def _element_name(el) -> str:
    for fn in ("window_text",):
        try:
            val = getattr(el, fn)()
            if val:
                return str(val).strip()
        except Exception:
            pass
    try:
        return str(el.element_info.name or "").strip()
    except Exception:
        return ""


def _element_type(el) -> str:
    try:
        return str(el.element_info.control_type or "")
    except Exception:
        return ""


def _automation_id(el) -> str:
    try:
        return str(el.element_info.automation_id or "")
    except Exception:
        return ""


def _rect(el) -> list[int]:
    try:
        r = el.rectangle()
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        return []


def _is_usable(el) -> bool:
    try:
        if hasattr(el, "is_visible") and not el.is_visible():
            return False
    except Exception:
        pass
    try:
        if hasattr(el, "is_enabled") and not el.is_enabled():
            return False
    except Exception:
        pass
    return True


def _score(el, wanted: str, control_type: str = "", automation_id: str = "") -> float:
    name = _norm(_element_name(el))
    wid = _norm(wanted)
    typ = _norm(_element_type(el))
    aid = _norm(_automation_id(el))
    ctyp = _norm(control_type)
    waid = _norm(automation_id)
    score = 0.0
    if waid:
        if aid == waid:
            score += 1000
        elif waid in aid:
            score += 400
        else:
            return -1
    if ctyp:
        if typ == ctyp:
            score += 180
        elif ctyp not in typ:
            return -1
    if wid:
        if name == wid:
            score += 800
        elif wid in name:
            score += 430
        elif name and name in wid:
            score += 260
        elif name:
            ratio = SequenceMatcher(None, wid, name).ratio()
            if ratio < 0.48:
                return -1
            score += ratio * 220
        else:
            return -1
    elif not (waid or ctyp):
        return -1
    if _is_usable(el):
        score += 40
    return score


def _find(win, name: str = "", control_type: str = "", automation_id: str = ""):
    try:
        descendants = win.descendants()
    except Exception as exc:
        raise RuntimeError(f"the application accessibility tree could not be read: {exc}")
    rows = []
    for el in descendants:
        try:
            s = _score(el, name, control_type, automation_id)
            if s >= 0:
                rows.append((s, el))
        except Exception:
            continue
    if not rows:
        bits = [x for x in (name, control_type, automation_id) if x]
        raise RuntimeError("no accessible control matched " + ", ".join(bits or ["the request"]))
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows[0][1]


def _list_windows() -> str:
    if _OS != "Windows":
        return "Native accessibility window listing is currently Windows-only."
    rows = []
    seen = set()
    try:
        for w in _desktop().windows():
            try:
                title = str(w.window_text() or "").strip()
                if not title or title in seen or not w.is_visible():
                    continue
                seen.add(title)
                rows.append(title)
            except Exception:
                continue
    except Exception as exc:
        return f"Could not enumerate application windows: {exc}"
    return "Visible application windows:\n" + "\n".join(f"- {x}" for x in rows[:40])


def _inspect(target: str, limit: int = 90) -> str:
    win, label = _window_for(target)
    out = []
    try:
        descendants = win.descendants()
    except Exception as exc:
        return f"Could not inspect {label}: {exc}"
    interesting = {
        "Button", "Edit", "ComboBox", "ListItem", "TabItem", "MenuItem",
        "CheckBox", "RadioButton", "Hyperlink", "TreeItem", "DataItem",
        "Slider", "Text",
    }
    for el in descendants:
        try:
            typ = _element_type(el)
            name = _element_name(el)
            aid = _automation_id(el)
            if typ not in interesting or (not name and not aid):
                continue
            row = {"type": typ}
            if name:
                row["name"] = name[:140]
            if aid:
                row["id"] = aid[:100]
            rect = _rect(el)
            if rect:
                row["rect"] = rect
            out.append(row)
            if len(out) >= max(10, min(160, int(limit))):
                break
        except Exception:
            continue
    if not out:
        return f"{label} exposed no useful accessible controls. Use computer_control or vision as fallback."
    return f"Accessible controls in {label}:\n" + json.dumps(out, ensure_ascii=False, indent=2)


def _read(target: str, limit: int = 5000) -> str:
    win, label = _window_for(target)
    pieces = []
    seen = set()
    for el in win.descendants():
        try:
            typ = _element_type(el)
            if typ not in {"Text", "Edit", "ListItem", "DataItem", "TreeItem", "Button", "TabItem"}:
                continue
            text = _element_name(el)
            if not text or text in seen:
                continue
            seen.add(text)
            pieces.append(text)
            if sum(len(x) + 1 for x in pieces) >= limit:
                break
        except Exception:
            continue
    if not pieces:
        return f"No readable accessibility text was exposed by {label}."
    return (f"Readable content from {label}:\n" + "\n".join(pieces))[:limit + 80]


def _click(target: str, name: str, control_type: str = "", automation_id: str = "") -> str:
    win, label = _window_for(target)
    el = _find(win, name, control_type, automation_id)
    picked = _element_name(el) or _automation_id(el) or _element_type(el)
    try:
        el.invoke()
    except Exception:
        try:
            el.click_input()
        except Exception:
            el.set_focus()
            el.click_input()
    return f"Clicked accessible control '{picked}' in {label}."


def _set_text(target: str, name: str, text: str, automation_id: str = "") -> str:
    win, label = _window_for(target)
    el = _find(win, name, "Edit", automation_id)
    picked = _element_name(el) or _automation_id(el) or "edit field"
    try:
        el.set_edit_text(str(text))
    except Exception:
        try:
            import pyautogui, pyperclip
            el.set_focus()
            pyperclip.copy(str(text))
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "v")
        except Exception as exc:
            raise RuntimeError(f"field was found but text could not be entered: {exc}")
    return f"Entered text in '{picked}' in {label}."


def _select(target: str, name: str, value: str, control_type: str = "") -> str:
    win, label = _window_for(target)
    el = _find(win, name, control_type or "ComboBox")
    picked = _element_name(el) or _automation_id(el) or "selection control"
    # Native ComboBox/ListBox wrappers often implement select(value).
    try:
        el.select(str(value))
        return f"Selected '{value}' in '{picked}' in {label}."
    except Exception:
        pass
    try:
        el.expand()
    except Exception:
        try:
            el.click_input()
        except Exception:
            pass
    time.sleep(0.12)
    # Re-read the whole window because expanding a combo creates item controls.
    item = _find(win, str(value), "ListItem")
    try:
        item.select()
    except Exception:
        try:
            item.click_input()
        except Exception:
            item.invoke()
    return f"Selected '{value}' in '{picked}' in {label}."


def _focus(target: str) -> str:
    win, label = _window_for(target)
    try:
        win.set_focus()
    except Exception:
        pass
    return f"Focused {label}."


def run(parameters: dict, **ctx):
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    target = str(p.get("app") or p.get("target") or "").strip()
    try:
        if action == "list_windows":
            return _list_windows()
        if action == "focus":
            return _focus(target)
        if action == "inspect":
            return _inspect(target, int(p.get("limit") or 90))
        if action == "read":
            return _read(target, int(p.get("limit") or 5000))
        if action in {"click", "invoke"}:
            return _click(target, str(p.get("name") or ""), str(p.get("control_type") or ""), str(p.get("automation_id") or ""))
        if action in {"set_text", "type"}:
            return _set_text(target, str(p.get("name") or ""), str(p.get("text") or ""), str(p.get("automation_id") or ""))
        if action == "select":
            return _select(target, str(p.get("name") or ""), str(p.get("value") or ""), str(p.get("control_type") or ""))
        return "Unknown app_control action. Use list_windows, focus, inspect, read, click, set_text, or select."
    except Exception as exc:
        return outcome.failed(
            "app_control",
            f"Native app control failed: {exc}. If this application does not expose accessibility controls, use computer_control or screen vision as fallback.",
            requested_action=action,
            app=target,
        )


TOOL = {
    "name": "app_control",
    "description": (
        "Controls normal Windows desktop applications through the accessibility/UI Automation tree. "
        "Prefer this over blind mouse coordinates when the user wants to operate a native app such as Settings, "
        "File Explorer, launchers, editors, Spotify, Discord desktop, Office, installers or dialogs. "
        "Use inspect to discover real controls, read to obtain visible accessible text, click/invoke for named buttons, "
        "set_text for named edit fields, select for lists/combos, focus to activate an app, and list_windows to see what is open. "
        "If it reports that the app exposes no accessible control, fall back to computer_control or screen_process."
    ),
    "brief": "Native Windows app control through accessibility: inspect/read/click/set_text/select/focus/list_windows. Prefer over coordinate clicks for desktop apps.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list_windows | focus | inspect | read | click | invoke | set_text | select"},
            "app": {"type": "STRING", "description": "Application/window name. Leave blank to target the currently active window."},
            "name": {"type": "STRING", "description": "Accessible control name/label to target."},
            "control_type": {"type": "STRING", "description": "Optional UIA control type such as Button, Edit, ComboBox, ListItem, TabItem."},
            "automation_id": {"type": "STRING", "description": "Optional exact/partial automation id from inspect output."},
            "text": {"type": "STRING", "description": "Text for set_text."},
            "value": {"type": "STRING", "description": "Value/item for select."},
            "limit": {"type": "INTEGER", "description": "Maximum controls/text size for inspect/read."},
        },
        "required": ["action"],
    },
    "handler": run,
}
