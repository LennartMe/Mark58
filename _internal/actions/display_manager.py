"""
actions/display_manager.py — every screen, not just the first one.

THE BUG THIS STARTED AS
    actions/screen_processor.py::_capture_screen() reads:

        monitors = sct.monitors
        target   = monitors[1] if len(monitors) > 1 else monitors[0]

    `monitors[1]` is the PRIMARY display. On a one-screen machine that is the
    right answer by luck. On a two-screen machine the assistant has been
    answering questions about the second monitor by looking at the first — and,
    worse, answering them confidently, because nothing in the path knew there
    was anything it had not seen. "What's this error on my right-hand screen?"
    got a reading of the left-hand screen, delivered without a caveat.

    That is the same failure `core/outcome.py` was written about: not a crash,
    a confident wrong answer.

WHAT THIS ADDS
    list      what displays exist, their geometry, which is primary, and a
              stable index the user can refer to out loud ("screen 2")
    capture   a frame from ONE named display, or from all of them stitched
    analyse   capture, then ask the vision model a question about it
    find      which display a given window or application is currently on

    `screen_processor._capture_screen()` is left in place and now takes an
    optional monitor argument, so every existing caller keeps working
    unchanged and the default behaviour is identical.

ON RESOLUTION AND COST
    A 4K screen is a 3840x2160 PNG, and three of them stitched is a picture no
    vision model needs and every one of them charges for. Frames are downscaled
    to a long edge of 1600px before they leave this module — enough to read a
    dialog box, roughly a tenth of the bytes. The stitch downscales further,
    because a strip of three screens is being asked "where is the thing", not
    "what does the small print say".
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Optional

from core import outcome

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


# Long edge, in pixels, for a single-display frame. 1600 keeps a 1080p screen
# essentially untouched and cuts a 4K one to a quarter of its area.
MAX_EDGE_SINGLE = 1600
MAX_EDGE_STITCH = 2000
JPEG_QUALITY = 78


def _monitors() -> list[dict]:
    """Real displays only — mss.monitors[0] is the union rectangle, not a screen."""
    if not _MSS:
        return []
    with mss.mss() as sct:
        raw = sct.monitors
    if len(raw) <= 1:
        return [dict(raw[0], index=1, primary=True)] if raw else []
    out = []
    for idx, mon in enumerate(raw[1:], start=1):
        item = dict(mon)
        item["index"] = idx
        # mss orders these with the primary first on every platform it supports.
        item["primary"] = (idx == 1)
        out.append(item)
    return out


def describe() -> list[dict]:
    """Displays as plain dicts, safe to hand to a UI or a model."""
    out = []
    for mon in _monitors():
        width, height = int(mon.get("width", 0)), int(mon.get("height", 0))
        out.append({
            "index": mon["index"],
            "primary": bool(mon.get("primary")),
            "width": width,
            "height": height,
            "left": int(mon.get("left", 0)),
            "top": int(mon.get("top", 0)),
            "label": f"Display {mon['index']}"
                     + (" (primary)" if mon.get("primary") else "")
                     + f" — {width}x{height}",
        })
    return out


def _resolve(monitor) -> Optional[dict]:
    """Accept 1-based index, 'primary', 'all', or a name like 'screen 2'."""
    mons = _monitors()
    if not mons:
        return None
    if monitor in (None, "", "primary", "main", 0):
        return next((m for m in mons if m.get("primary")), mons[0])
    text = str(monitor).strip().lower()
    digits = "".join(c for c in text if c.isdigit())
    if digits:
        idx = int(digits)
        for mon in mons:
            if mon["index"] == idx:
                return mon
        return None
    if text in ("primary", "main", "first"):
        return mons[0]
    if text in ("second", "secondary") and len(mons) > 1:
        return mons[1]
    return None


def _encode(img, max_edge: int) -> tuple[bytes, str]:
    """Downscale to a long edge and JPEG it. Falls back to raw PNG without PIL."""
    if not _PIL:
        return img, "image/png"
    try:
        image = PIL.Image.open(io.BytesIO(img))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        longest = max(image.size)
        if longest > max_edge:
            scale = max_edge / longest
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                PIL.Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return img, "image/png"


def capture(monitor=None) -> tuple[bytes, str, dict]:
    """One display's pixels. Returns (bytes, mime, the display's description).

    Raises RuntimeError rather than returning an outcome string, because the
    callers inside this file want the exception and the TOOL handler at the
    bottom is the only place that speaks the outcome vocabulary.
    """
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    target = _resolve(monitor)
    if target is None:
        raise RuntimeError(f"no display matches {monitor!r}")
    region = {k: target[k] for k in ("left", "top", "width", "height")}
    with mss.mss() as sct:
        shot = sct.grab(region)
        png = mss.tools.to_png(shot.rgb, shot.size)
    data, mime = _encode(png, MAX_EDGE_SINGLE)
    return data, mime, {"index": target["index"], "width": target["width"],
                        "height": target["height"]}


def capture_all() -> tuple[bytes, str, dict]:
    """Every display side by side in one frame, in index order.

    Stitched rather than sent as N images because the question being asked of a
    multi-screen shot is almost always comparative — where is the window, which
    screen is the video on — and a model answers that better from one picture
    than from three it has to hold in mind at once.
    """
    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")
    mons = _monitors()
    if not mons:
        raise RuntimeError("no displays detected")
    if len(mons) == 1:
        return capture(mons[0]["index"])
    if not _PIL:
        # Without PIL there is nothing to stitch with; the primary is a better
        # answer than a crash, and the caller is told which one it got.
        data, mime, meta = capture(mons[0]["index"])
        meta["note"] = "pillow is not installed, so only the primary display was captured"
        return data, mime, meta

    frames = []
    with mss.mss() as sct:
        for mon in mons:
            region = {k: mon[k] for k in ("left", "top", "width", "height")}
            shot = sct.grab(region)
            frames.append(PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))

    gap = 12
    height = max(f.height for f in frames)
    width = sum(f.width for f in frames) + gap * (len(frames) - 1)
    canvas = PIL.Image.new("RGB", (width, height), (16, 18, 22))
    x = 0
    for frame in frames:
        canvas.paste(frame, (x, 0))
        x += frame.width + gap

    longest = max(canvas.size)
    if longest > MAX_EDGE_STITCH:
        scale = MAX_EDGE_STITCH / longest
        canvas = canvas.resize((int(canvas.width * scale), int(canvas.height * scale)),
                               PIL.Image.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue(), "image/jpeg", {
        "index": 0, "count": len(mons),
        "order": [m["index"] for m in mons],
        "note": "displays are left to right in index order, separated by a dark gap"}


def window_display(title_fragment: str) -> Optional[int]:
    """Which display a window is on, by matching a fragment of its title."""
    try:
        import pygetwindow as gw
    except Exception:
        return None
    fragment = str(title_fragment).strip().lower()
    if not fragment:
        return None
    try:
        matches = [w for w in gw.getAllWindows()
                   if fragment in (w.title or "").lower() and w.width > 0]
    except Exception:
        return None
    if not matches:
        return None
    win = matches[0]
    centre_x, centre_y = win.left + win.width // 2, win.top + win.height // 2
    for mon in _monitors():
        if (mon["left"] <= centre_x < mon["left"] + mon["width"]
                and mon["top"] <= centre_y < mon["top"] + mon["height"]):
            return mon["index"]
    return None


# ── The tool ──────────────────────────────────────────────────────────────────

def display_control(parameters: dict, player=None, response=None) -> str:
    action = str(parameters.get("action", "list")).strip().lower()
    monitor = parameters.get("monitor")
    question = str(parameters.get("question", "")).strip()

    if not _MSS:
        return outcome.failed("display_control", "mss_not_installed",
                              fix="pip install mss")

    try:
        if action in ("list", "displays", "enumerate"):
            screens = describe()
            if not screens:
                return outcome.failed("display_control", "no_displays_detected")
            facts = {f"display_{s['index']}":
                     f"{s['width']}x{s['height']}{' primary' if s['primary'] else ''}"
                     for s in screens}
            return outcome.done("display_control", count=len(screens), **facts)

        if action in ("find", "locate", "which_display"):
            fragment = str(parameters.get("app_name")
                           or parameters.get("window") or question).strip()
            if not fragment:
                return outcome.ask("display_control",
                                   "which_window_or_application", action="find")
            found = window_display(fragment)
            if found is None:
                return outcome.unverified("display_control", "window_not_found",
                                          searched=fragment)
            return outcome.done("display_control", window=fragment, display=found)

        if action in ("capture", "screenshot", "analyse", "analyze", "read", "all"):
            want_all = action == "all" or str(monitor).strip().lower() in ("all", "every")
            if want_all:
                data, mime, meta = capture_all()
            else:
                data, mime, meta = capture(monitor)

            # Hand the frame to whatever the app uses for vision. `response` is
            # the live session injector main.py passes in; when it is absent
            # (a direct call, a test) the frame is saved instead of dropped.
            if response is not None and hasattr(response, "send_image"):
                try:
                    response.send_image(data, mime, question or "")
                    return outcome.done("display_control", captured=meta.get("index", "?"),
                                        bytes=len(data), sent="vision")
                except Exception as e:
                    return outcome.unverified("display_control", f"vision_send_failed:{e}",
                                              captured=meta.get("index", "?"))

            from core.user_data import MEMORY_DIR
            path = MEMORY_DIR / f"display_{int(time.time())}.jpg"
            try:
                path.write_bytes(data)
            except Exception as e:
                return outcome.failed("display_control", f"write_failed:{e}")
            if player is not None:
                try:
                    player.write_log(f"JARVIS: captured display {meta.get('index', '?')}")
                except Exception:
                    pass
            return outcome.done("display_control", captured=meta.get("index", "?"),
                                saved=path.name, bytes=len(data))

        return outcome.failed("display_control", "unknown_action", action=action)

    except RuntimeError as e:
        return outcome.failed("display_control", str(e), action=action)
    except Exception as e:
        return outcome.failed("display_control", f"{type(e).__name__}:{e}", action=action)


TOOL = {
    "name": "display_control",
    "description": (
        "Sees and reasons about ALL connected monitors, not just the main one. "
        "Use action='list' to find out how many screens there are and how big "
        "they are. Use action='capture' with 'monitor' set to a screen number "
        "to look at ONE specific screen — 'what's on my second monitor', "
        "'read the error on screen 2'. Use monitor='all' to see every screen at "
        "once, which is the right choice for 'where did that window go' or "
        "'what's on my screens'. Use action='find' with 'app_name' to work out "
        "which screen a particular window is currently on. "
        "For an ordinary screenshot of whatever the user is looking at right "
        "now, the existing screen capture is still fine — this tool is for when "
        "the screen MATTERS, i.e. the user said which one or has more than one."
    ),
    "brief": "Lists, captures and analyses any connected display; monitor='all' sees every screen at once.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | capture | find",
            },
            "monitor": {
                "type": "STRING",
                "description": (
                    "Which display: a number as the user said it ('2', 'screen 2'), "
                    "'primary', or 'all' for every screen in one frame. "
                    "Defaults to the primary display."
                ),
            },
            "app_name": {
                "type": "STRING",
                "description": "For action='find': part of the window or application title.",
            },
            "question": {
                "type": "STRING",
                "description": "What to look for in the captured frame, if anything specific.",
            },
        },
        "required": ["action"],
    },
    "handler": display_control,
}
