"""
screen_agent — read the screen as text, find things on it, wait for them, and
carry out a short multi-step job by looking, acting and checking.

WHERE IT SITS
    screen_process    lets the MODEL look at the screen (the image joins the
                      conversation). Use it to answer "what's on my screen?".
    screen_agent      does something WITH the screen: returns its text for
                      further use, locates an element, waits for one to
                      appear, or performs a sequence of clicks and typing
                      toward a goal and checks each step visibly changed
                      something.
    computer_control  single blind input: one click, one hotkey, one string.

    desktop.py and requirements.txt already described `screen_agent` as the
    home of multi-step screen jobs; this is that tool.

SAFETY
    - HALT stops it between steps (core/autonomy.py).
    - Moving the mouse into a screen corner aborts immediately (PyAutoGUI
      fail-safe).
    - Step cap: 8 by default, 20 at most.
    - It stops and asks rather than deleting, sending, buying, submitting or
      confirming anything the goal did not name explicitly.
    - Screen text is treated as data, never as instructions.
"""
from __future__ import annotations

import time

from core import outcome


def _halted() -> bool:
    try:
        from core import autonomy
        return autonomy.halted()
    except Exception:
        return False


def _do(goal: str, monitor, max_steps: int, player) -> str:
    from core import screen_vision as sv
    mon = sv.resolve_monitor(monitor)
    if mon is None:
        return outcome.failed("screen_agent", "no display could be captured")
    history: list[str] = []
    unchanged = 0
    for step in range(1, max_steps + 1):
        if _halted():
            return outcome.failed("screen_agent", "HALT is active — stopped", steps=step - 1)
        before = sv.grab(mon)
        plan = sv.decide(goal, history, before, mon)
        status = str(plan.get("status") or "").lower()
        reason = str(plan.get("reason") or "")[:200]
        if not plan:
            return outcome.failed("screen_agent", "the vision model did not answer",
                                  steps=step - 1, history=history[-4:])
        if status == "done":
            return outcome.done("screen_agent", goal=goal, steps=step - 1,
                                verified_by="final screenshot", observation=reason)
        if status == "impossible":
            return outcome.failed("screen_agent", reason or "the goal is not reachable from here",
                                  steps=step - 1, history=history[-4:])
        if status == "needs_user":
            return outcome.ask("screen_agent", reason or "the next step needs your go-ahead",
                               steps=step - 1, history=history[-4:])
        action = plan.get("action") if isinstance(plan.get("action"), dict) else {}
        try:
            did = sv.perform(action, mon)
        except Exception as e:
            if "FailSafe" in type(e).__name__:
                return outcome.failed("screen_agent", "stopped by the mouse fail-safe", steps=step)
            did = f"error: {e}"
        time.sleep(0.8 if action.get("type") != "wait" else 0.1)
        after = sv.grab(mon)
        change = sv.diff_score(before, after)
        changed = change >= 0.004
        unchanged = 0 if changed or action.get("type") in ("wait", "scroll") else unchanged + 1
        history.append(f"{did} — screen {'changed' if changed else 'did NOT visibly change'}")
        if unchanged >= 3:
            return outcome.unverified("screen_agent",
                                      "three actions in a row produced no visible change",
                                      steps=step, history=history[-4:])
    return outcome.unverified("screen_agent", f"step limit ({max_steps}) reached before the "
                              "goal was confirmed", steps=max_steps, history=history[-4:])


def _wait_for(description: str, monitor, timeout: float) -> str:
    from core import screen_vision as sv
    mons = sv.search_order(monitor)
    if not mons:
        return outcome.failed("screen_agent", "no display could be captured")
    deadline = time.monotonic() + timeout
    last = {m.get("index"): None for m in mons}
    checks = 0
    while time.monotonic() < deadline:
        if _halted():
            return outcome.failed("screen_agent", "HALT is active — stopped waiting")
        for m in mons:
            img = sv.grab(m)
            prev = last.get(m.get("index"))
            # Only spend a model call when the display changed since the last look.
            if prev is None or sv.diff_score(prev, img) >= 0.003:
                checks += 1
                hit = sv.locate(description, img=img, mon=m)
                if hit:
                    return outcome.done("screen_agent", appeared=description, x=hit["x"],
                                        y=hit["y"], display=hit["monitor"], checks=checks)
            last[m.get("index")] = img
        time.sleep(1.2)
    return outcome.unverified("screen_agent", f"'{description}' did not appear within "
                              f"{int(timeout)}s", checks=checks)


def screen_agent(parameters: dict, player=None, **_ctx) -> str:
    from core import screen_vision as sv
    p = parameters or {}
    mode = str(p.get("mode") or "read").strip().lower()
    monitor = p.get("monitor")
    target = str(p.get("target") or p.get("goal") or p.get("question") or "").strip()

    moved = sv.step_aside(player, True) if mode in ("do", "find", "click", "wait_for", "read") else False
    try:
        if mode == "read":
            answer, idx = sv.read(target, monitor)
            if not answer:
                return outcome.failed("screen_agent", "the screen could not be read")
            return outcome.untrusted("screen", f"[display {idx}]\n{answer}")

        if mode in ("find", "click", "double_click"):
            if not target:
                return outcome.failed("screen_agent", "describe the element in 'target'")
            hit = sv.locate(target, monitor)
            if not hit:
                return outcome.failed("screen_agent", f"'{target}' was not found on any display")
            if mode == "find":
                return outcome.done("screen_agent", found=hit["label"], x=hit["x"], y=hit["y"],
                                    display=hit["monitor"], confidence=f"{hit['confidence']:.2f}")
            from actions import computer_control as cc
            img_before = sv.grab(sv.resolve_monitor(hit["monitor"]))
            cc._click(hit["x"], hit["y"], "left", 2 if mode == "double_click" else 1)
            time.sleep(0.7)
            change = sv.diff_score(img_before, sv.grab(sv.resolve_monitor(hit["monitor"])))
            if change >= 0.004:
                return outcome.done("screen_agent", clicked=hit["label"], x=hit["x"], y=hit["y"],
                                    display=hit["monitor"], screen_changed=True)
            return outcome.unverified("screen_agent", "clicked, but the screen did not visibly "
                                      "change", clicked=hit["label"], x=hit["x"], y=hit["y"])

        if mode == "wait_for":
            if not target:
                return outcome.failed("screen_agent", "describe what to wait for in 'target'")
            try:
                timeout = max(2.0, min(180.0, float(p.get("timeout") or 30)))
            except (TypeError, ValueError):
                timeout = 30.0
            return _wait_for(target, monitor, timeout)

        if mode == "do":
            if not target:
                return outcome.failed("screen_agent", "say what to do in 'goal'")
            try:
                steps = max(1, min(20, int(p.get("max_steps") or 8)))
            except (TypeError, ValueError):
                steps = 8
            return _do(target, monitor, steps, player)

        if mode == "displays":
            from actions.display_manager import describe
            return "Displays: " + "; ".join(d["label"] for d in describe())

        return "Unknown mode. Use read, find, click, double_click, wait_for, do, or displays."
    except Exception as e:
        return outcome.failed("screen_agent", f"{type(e).__name__}: {e}")
    finally:
        if moved:
            sv.step_aside(player, False)


TOOL = {
    "name": "screen_agent",
    "description": (
        "Acts on what is visible on the screen, on any display. Modes: "
        "read — return the screen's text or answer a question about it AS TEXT (use this "
        "when you need the content for something else, e.g. copying numbers into a note; "
        "to simply look at the screen yourself use screen_process); "
        "find — locate a described element and return its coordinates; "
        "click / double_click — find an element and click it, then check the screen changed; "
        "wait_for — wait until something described appears (a dialog, a 'Download complete' "
        "message), up to 'timeout' seconds; "
        "do — carry out a short multi-step job on the screen toward 'goal' (click, type, "
        "scroll, keys), checking each step, e.g. 'in the open Settings window turn on dark "
        "mode'. It stops and returns status=ask instead of deleting, sending, buying or "
        "confirming anything the goal did not name. Prefer app_control or browser_control "
        "when those can do the job with named controls; use this when they cannot. "
        "The HUD minimises itself while this looks at the screen."
    ),
    "brief": "Screen text reading, element find/click, wait-for, and short multi-step screen jobs.",
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING", "description": "read | find | click | double_click | wait_for | do | displays"},
            "target": {"type": "STRING", "description": "read: the question (empty = transcribe); find/click/wait_for: element description; do: the goal"},
            "monitor": {"type": "STRING", "description": "Display number or 'primary'. Omit to search the display under the mouse first, then the others."},
            "timeout": {"type": "NUMBER", "description": "wait_for: seconds, 2-180 (default 30)"},
            "max_steps": {"type": "INTEGER", "description": "do: step limit, 1-20 (default 8)"},
        },
        "required": ["mode"],
    },
    "handler": screen_agent,
}
