"""
JARVIS plugin — WhatsApp incoming-call guard.

whatsapp_call.py rings other people. This one handles the other direction: the
phone rings, and JARVIS tells you who it is and does what you say.

    "Yusuf is calling."          → "aç"                       → answered
                                 → "açma"                     → declined
                                 → "açma, müsait değilim de"  → declined, and
                                                                the message you
                                                                wrote is sent
                                 → "aç, adresi de yolla"      → answered, and
                                                                the message you
                                                                wrote is sent

    Nothing above is a keyword. The model reads what you said, in whatever
    language you said it in, and fills in a decision and a `message` field —
    so "decline and tell them I'm in a meeting" and "reddet, toplantıdayım
    de" reach this file as the same two values.

NEEDS  _whatsapp_core.py  NEXT TO IT
    Same shared driver whatsapp_call.py uses, and it works on all three systems:
    the desktop app through UI Automation on Windows and the Accessibility API
    on macOS, and WhatsApp Web through Playwright everywhere — which is the only
    option on Linux, where WhatsApp has no official desktop client.

THREE MODES
    announce     say who is calling and wait for your voice. Nothing is pressed
                 until you decide. This is the default, because a call answered
                 without you is worse than a call missed.
    auto_reply   decline immediately and send the sentence you wrote in
                 settings. For meetings — the caller gets a real answer rather
                 than a ring that dies.
    off          watch nothing.

    A per-contact list overrides the mode both ways: people you always want
    announced, and people who are always auto-declined.

IT ONLY WATCHES WHEN YOU SAY SO
    Plugins have no startup hook and this one deliberately does not want one:
    a background thread that reads your WhatsApp window should start because you
    asked, not because a file was in a folder. "Turn on the call guard" starts
    it, the same way water_reminder starts its loop.

WHAT IT DOES NOT DO
    It does not talk to the caller. Answering puts the call on your speakers and
    your microphone, exactly as if you had clicked it yourself — JARVIS steps
    back at that point. Playing a spoken message *to* the caller needs the audio
    routed into the call, which is a different piece of work.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from memory.config_manager import get_plugin_config
from plugins import _whatsapp_core as wa

_NS = "whatsapp_guard"

# How often the transport is asked whether anything is ringing.
#
# This is half of the delay a person actually feels — a poll that has just run
# when the phone starts ringing does not look again for a full interval. It was
# 1.2s, which averaged 0.6s of dead time for nothing: with the main window
# skipped, a look costs about 3ms when nothing is ringing (measured: five polls
# in 15ms). Spending it four times as often is free and the call is seen almost
# as soon as it appears.
_POLL_SECONDS = 0.35
# How long a ringing call stays answerable after it is announced. Longer than
# the ring itself on purpose: it is better to tell the user "that call already
# ended" than to have forgotten which call they meant.
_PENDING_SECONDS = 45.0
# After the transport fails, how long before trying to attach again. Stops a
# closed WhatsApp from being relaunched over and over in the background.
_REATTACH_SECONDS = 30.0
# How long a ringing call may stay unidentified before it is handled anyway.
#
# The very first call in a given WhatsApp language arrives before anyone has
# worked out which button answers and which refuses — that answer is fetched in
# the background the instant the call is seen, and cached for every call after.
# So the loop simply looks again a third of a second later, and again, until the
# answer lands. That is what makes it feel instant on every call but the first,
# and still act on the first.
#
# It must be well under the ~30s a WhatsApp call rings for: whatever happens,
# something has to happen while the phone is still ringing.
_RESOLVE_GRACE = 5.0


PLUGIN = {
    "name": "whatsapp_guard",
    "description": (
        "Watches for INCOMING WhatsApp calls and answers, declines or "
        "auto-replies to them. Use when the user wants JARVIS to handle calls "
        "that come IN — e.g. 'watch my WhatsApp calls', 'gelen aramaları "
        "karşıla', 'turn on the call guard', 'answer it', 'aç', 'don't answer', "
        "'açma', 'decline and tell them I'm busy', 'reddet ve müsait "
        "olmadığımı söyle', 'stop watching my calls', 'did I miss any calls'. "
        "When a call is ringing and the user says to answer it use "
        "action='accept'; to refuse it and say nothing use action='decline'; "
        "to refuse it AND text the caller use action='decline_message'; to "
        "answer it AND text the caller use action='accept_message'. "
        "ANY request that decides the call AND says something to pass on "
        "carries a message: put what the user wants sent into 'message', in "
        "their own words and their own language, and use the _message form of "
        "the action. 'Decline and tell them I'm busy', 'reddet ve müsait "
        "olmadığımı söyle', 'açma, meşgulüm de', 'refuse and send a message "
        "that I'm not available' are action='decline_message'; 'answer it and "
        "send them the address', 'aç ve birazdan arayacağımı yaz' are "
        "action='accept_message'. Never plain 'decline' or plain 'accept' when "
        "the user asked for something to be sent. "
        "If the tool reports that the message was NOT sent, say so plainly and "
        "repeat the reason it gives; never tell the user a message was sent "
        "unless the tool said it was. "
        "Do NOT use 'whatsapp_call' for any of this — that one PLACES a call to "
        "somebody, it never answers one."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'on' start watching (default), 'off' stop watching, "
                    "'status' report what is happening and list missed calls, "
                    "'accept' answer the call ringing now, 'decline' refuse it, "
                    "'decline_message' refuse it and text the caller, "
                    "'accept_message' answer it and text the caller, "
                    "'probe' watch for 30 seconds and report every control "
                    "WhatsApp shows (for setting the guard up — use only when "
                    "the user explicitly asks to test or debug call detection)."
                ),
                "enum": ["on", "off", "status", "accept", "decline",
                         "decline_message", "accept_message", "probe"],
            },
            "message": {
                "type": "STRING",
                "description": (
                    "The text to send the caller, in the USER'S OWN LANGUAGE, "
                    "taken from what they just said. Use it with "
                    "action='decline_message' or action='accept_message' for "
                    "the call ringing now, AND "
                    "with action='on' + mode='auto_reply' to set what every "
                    "declined call is told for the rest of the session. "
                    "'If anyone calls, decline it and say I'm not available' "
                    "already contains it — pass 'I'm not available right now.' "
                    "Leave it out only when the user gave no hint of what to "
                    "say; their saved message is then used."
                ),
            },
            "mode": {
                "type": "STRING",
                "description": (
                    "Only with action='on'. 'announce' (default) says who is "
                    "calling and waits for the user. 'auto_reply' declines "
                    "every call AND texts the caller. 'auto_decline' declines "
                    "every call and sends NOTHING — use this whenever the user "
                    "asks to refuse calls without mentioning telling anyone, "
                    "or says not to message them: 'sadece reddet', 'hiçbir şey "
                    "yazma', 'just decline them', 'don't send any message'."
                ),
                "enum": ["announce", "auto_reply", "auto_decline"],
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    # Its own namespace on purpose. The settings UI shows one section per
    # namespace, so sharing whatsapp_call's would hide these fields behind that
    # plugin's card. Two cards: one for reaching WhatsApp, one for what to do
    # when it rings.
    "namespace": _NS,
    "title": "🛡  WHATSAPP CALL GUARD",
    "fields": [
        {"key": "busy_message",
         "label": "Message sent when a call is auto-declined",
         "type": "text",
         "placeholder": "Şu an müsait değilim, birazdan döneceğim."},
        {"key": "always_announce",
         "label": "Always announce these (comma-separated names)",
         "type": "text", "placeholder": "Anne, Yusuf"},
        {"key": "always_decline",
         "label": "Always decline these (comma-separated names)",
         "type": "text", "placeholder": "Spam, Unknown"},
        {"key": "decline_unknown",
         "label": "Auto-decline callers whose name cannot be read",
         "type": "toggle", "default": False},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────
def _guard_cfg() -> dict:
    stored = get_plugin_config(_NS) or {}

    def _list(key: str) -> list[str]:
        raw = str(stored.get(key) or "")
        return [wa.fold(p) for p in raw.split(",") if p.strip()]

    decline_unknown = stored.get("decline_unknown")
    if not isinstance(decline_unknown, bool):
        decline_unknown = str(decline_unknown or "").strip().lower() in (
            "1", "true", "on", "yes")

    return {
        "busy_message": str(stored.get("busy_message") or "").strip(),
        "always_announce": _list("always_announce"),
        "always_decline": _list("always_decline"),
        "decline_unknown": decline_unknown,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Live state — one watcher per process
# ─────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state: dict = {
    "running": False,
    "thread": None,
    "stop": None,
    "mode": "announce",
    "pending": None,        # the Incoming currently being decided on
    "pending_at": 0.0,
    "seen": [],             # recent Incoming.key values, so one ring = one announce
    "missed": [],           # [(when, caller, what-happened), ...]
    "transport": None,
    "message": "",          # what the user said to tell callers, this session
}


# ─────────────────────────────────────────────────────────────────────────────
# HUD helpers (no-ops when player is None)
# ─────────────────────────────────────────────────────────────────────────────
def _log(player, msg: str) -> None:
    if player:
        try:
            player.write_log(msg)
        except Exception:
            pass


def _say(player, instruction: str) -> None:
    if player and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


def _panel(player) -> None:
    if not player:
        return
    with _lock:
        mode = _state["mode"]
        running = _state["running"]
        missed = list(_state["missed"])[-8:]
    head = f"{'🟢' if running else '⚪'}  Guard {'on' if running else 'off'}"
    if running:
        head += f"  ·  {mode.replace('_', ' ')}"
    body = "\n".join(f"{when}  {who or 'unknown'} — {what}"
                     for when, who, what in reversed(missed)) or "No calls yet."
    try:
        player.show_content("🛡 WHATSAPP CALLS", f"{head}\n\n{body}")
    except Exception:
        pass


def _record(caller: str, what: str) -> None:
    with _lock:
        _state["missed"].append((datetime.now().strftime("%H:%M"), caller, what))
        del _state["missed"][:-40]


# ─────────────────────────────────────────────────────────────────────────────
# Deciding what to do with one ring
# ─────────────────────────────────────────────────────────────────────────────
def _listed(caller: str, names: list[str]) -> bool:
    """Is this caller on one of the user's lists? Folded on both sides, so
    'anne' matches 'Anne ❤' and case and accents never matter."""
    if not names:
        return False
    folded = wa.fold(caller)
    if not folded:
        return False
    return any(n and (n in folded or folded in n) for n in names)


def _handle(player, call, mode: str, gcfg: dict, transport) -> None:
    """One ringing call, decided and acted on."""
    caller = (call.caller or "").strip()
    kind = "video call" if call.call_type == "video" else "call"

    # NOTHING above the announcement is allowed to wait on anything.
    #
    # The first version worked out which button answers and which refuses before
    # saying a word, and that reads the labels over the network the first time a
    # given WhatsApp language is seen. Measured on a real call: ten seconds of
    # silence while the phone rang, because the first model in the list was
    # timing out. The user is watching a ringing phone — the assistant has to be
    # ahead of them, not behind.
    #
    # Both facts the announcement needs are already in hand: who is calling
    # comes from the window title, and that it IS a call comes from the shape.
    # Which button does what is only needed once somebody has said "answer it",
    # and by then the background resolve started at first sighting has finished.
    if not caller:
        # Everything downstream needs a name — the message is sent by opening
        # that person's conversation — so when it cannot be read, put what WAS
        # on the window into the log. The alternative is asking the user to run
        # a probe and ring themselves again, which is a lot to ask twice.
        _log(player, "JARVIS: Could not read the caller's name. The call window "
                     "published: " + (", ".join(call.controls[:12]) or "nothing"))

    policy_decline = (_listed(caller, gcfg["always_decline"])
                      or (gcfg["decline_unknown"] and not caller))
    auto = (mode in ("auto_reply", "auto_decline")
            and not _listed(caller, gcfg["always_announce"]))

    # Silence is a choice the user is allowed to make, and it is honoured
    # absolutely: in auto_decline nothing is typed to anybody, whatever is
    # saved in the settings.
    quiet = mode == "auto_decline"

    if policy_decline or auto:
        # This path presses a button, so it is the one place that may wait.
        _decline_now(player, call, transport, gcfg, with_message=not quiet,
                     reason="on your always-decline list" if policy_decline and caller
                     else "the caller could not be identified" if policy_decline
                     else "auto-reply is on" if not quiet
                     else "you asked for calls to be refused silently")
        return

    with _lock:
        _state["pending"] = call
        _state["pending_at"] = time.monotonic()
    who = caller or "Someone whose name I can't read"
    _log(player, f"JARVIS: Incoming WhatsApp {kind} — {who}.")
    _say(player,
         f"A WhatsApp {kind} is coming in right now from: {who}. Tell the user "
         f"who is calling and ask whether to answer it. One short sentence, in "
         f"their own language. Do not call any tool yet — wait for their answer.")
    _panel(player)


def _note_for(message: str, gcfg: dict) -> str:
    """What actually gets sent, in order of who said it most recently.

    What the user just SAID wins over what they once configured.

    "Decline and tell them I'm not available" already contains the message, in
    their own language, and the assistant transcribing it is the same one that
    will phrase it. Requiring them to have filled in a settings field first is
    why the last real call was declined in silence. The saved message stays as
    the fallback, and for auto-reply mode — where nobody is speaking — it is
    the only source.
    """
    with _lock:
        session_note = _state.get("message") or ""
    return (message or "").strip() or session_note or gcfg["busy_message"]


def _send_to_caller(transport, caller: str, note: str) -> tuple[bool, str]:
    """Text the caller. -> (sent, why-not, in words the model can pass on).

    Both call decisions that carry a message come through here, and the
    sequence itself lives one level further down in the transport's
    send_message_to(): open the conversation, prove it is the right one, type,
    confirm the box emptied. Nothing in this file assembles that by hand any
    more — it was assembled twice, checked differently each time, and the
    difference is how a message that never went got reported as sent.
    """
    if not note:
        return False, ("nothing was ever said about what to send, so NOTHING "
                       "was sent - ask the user what they want the caller told")
    if not caller:
        return False, ("the caller could not be identified, so there was no "
                       "conversation to send it to and NOTHING was sent")
    try:
        return transport.send_message_to(caller, note, wa.cfg())
    except wa.Unsupported as e:
        return False, f"{e}, so NOTHING was sent"
    except Exception as e:
        print(f"[WhatsAppGuard] sending to {caller!r} failed: "
              f"{type(e).__name__}: {e}")
        return False, f"sending the message failed ({e}), so NOTHING was sent"


def _decline_now(player, call, transport, gcfg: dict, with_message: bool,
                 reason: str = "", message: str = "") -> str:
    """Decline, and optionally send the user's own busy message afterwards."""
    caller = (call.caller or "").strip()

    # This is a pressing path, so unlike the announcement it is allowed to wait
    # for the control mapping. Only ever reached on the automatic routes — an
    # always-decline contact or auto-reply mode — where nobody is waiting on a
    # sentence from JARVIS.
    if not call.identified:
        # A cached re-read only. The watch loop already gave the background
        # lookup its grace period, so if it has landed this picks it up for
        # free; if it has not, blocking here would just spend the rest of the
        # ring waiting instead of telling the user something useful.
        try:
            fresh = transport.incoming()
        except Exception as e:
            print(f"[WhatsAppGuard] re-reading the call failed: {e}")
            fresh = None
        if fresh is None:
            _record(caller, "stopped ringing")
            return f"The call from {caller or 'an unknown caller'} stopped ringing."
        if not fresh.identified:
            _record(caller, "controls unresolved — left alone")
            _log(player, f"JARVIS: Could not identify the call controls "
                         f"({', '.join(fresh.controls[:6]) or 'none published'}) "
                         f"— left the call alone.")
            return ("A call came in but I couldn't tell which button declines "
                    "it, so I left it alone rather than guess.")
        call = fresh

    try:
        declined = transport.decline(call)
    except wa.Unsupported as e:
        _record(caller, f"could not decline ({e})")
        return f"I couldn't decline that call: {e}."
    except Exception as e:
        _record(caller, f"could not decline ({e})")
        return f"I couldn't decline that call: {e}."

    if not declined:
        _record(caller, "decline failed")
        return "I found the call but couldn't press decline."

    # LET THE CALL WINDOW GO BEFORE TYPING ANYTHING.
    #
    # Declining does not close the panel instantly, and while it is closing it
    # is the window with the keyboard. It belongs to WhatsApp, so every check
    # that asks "does WhatsApp have the keyboard" answers yes — correctly, and
    # uselessly, because a call panel has no text box: the reply was typed into
    # it and vanished, and the user was told the message was not sent.
    #
    # Waiting for it is cheap and needs no new way of recognising anything —
    # incoming() already reports whether a call window is on screen, and a look
    # costs under a millisecond when there is nothing ringing.
    gone = False
    for _ in range(24):                      # ~3 seconds, in eighths
        try:
            if transport.incoming() is None:
                gone = True
                break
        except Exception:
            gone = True                      # cannot see one; do not wait on it
            break
        time.sleep(0.125)
    if not gone:
        print("[WhatsAppGuard] the call window is still up after three seconds "
              "— sending anyway.")

    sent = False
    failure = ""                      # why the message did not go, in the model's words

    note = _note_for(message, gcfg)
    if with_message:
        sent, failure = _send_to_caller(transport, caller, note)

    what = "declined"
    if sent:
        what += " + message sent"
    elif with_message:
        what += " (message NOT sent)"
    _record(caller, what)
    _log(player, f"JARVIS: WhatsApp call from {caller or 'unknown'} — {what}.")
    _panel(player)

    # Say what did NOT happen, explicitly.
    #
    # The first version returned "Declined the call from X." and left the rest
    # unsaid — and the model, having been asked to decline AND send a message,
    # filled the gap for itself: "Call declined, and I've sent the message that
    # you're unavailable, sir." Nothing had been sent. A tool that reports only
    # its successes is an invitation to invent the rest, so a failure is now
    # stated in the result rather than left out of it.
    who = caller or "an unknown caller"
    because = f" ({reason})" if reason else ""
    if not with_message:
        return f"Declined the call from {who}{because}."
    if sent:
        return f"Declined the call from {who} and sent your message{because}."
    return (f"Declined the call from {who}{because}, but {failure}. Tell the "
            f"user plainly that the message was NOT sent and why - do not say "
            f"it was sent.")


# ─────────────────────────────────────────────────────────────────────────────
# The watcher
# ─────────────────────────────────────────────────────────────────────────────
def _report_windows(player, transport) -> None:
    """Say what WhatsApp just put on screen, and what the guard made of it.

    This runs the moment the set of WhatsApp windows changes — which is the
    moment a call starts ringing — and nothing else. It exists because three
    separate rounds of "it does not trigger" were each debugged by guessing,
    changing something, and asking for another call. The window that appears is
    the whole story, so the guard tells it once, by itself, instead of needing a
    probe run alongside.
    """
    try:
        fam = wa._whatsapp_family(getattr(transport, "_pids", None))
        main = {h for h in (getattr(transport, "_focus_hwnd", None),
                            getattr(transport, "_scan_hwnd", None)) if h}
        lines = [f"[WhatsAppGuard] windows changed: {len(fam)} | "
                 f"main handles: {sorted(main) or 'NONE'}"]
        for w in fam:
            r = w["rect"]
            tag = "MAIN-skipped" if w["hwnd"] in main else "candidate"
            lines.append(f"  hwnd={w['hwnd']} [{tag}] {r[2]-r[0]}x{r[3]-r[1]} "
                         f"class={w['class']} title={w['title']!r}")
            if w["hwnd"] in main:
                continue
            data = wa._scan_call_window(w["hwnd"])
            if data is None:
                lines.append("    scan -> None (timed out or the window went away)")
                continue
            _wr, btns, txts = data
            named = [n for n, _r in btns if n]
            lines.append(f"    buttons: {named or 'none named'}")
            lines.append(f"    texts:   {[t for _y, t in txts] or 'none'}")
            lines.append(f"    ringing_shape -> {wa._ringing_shape(btns)} "
                         f"(needs 2-6 named buttons)")
        for ln in lines:
            print(ln)
        head = fam and [w for w in fam if w["hwnd"] not in main]
        if head:
            _log(player, f"JARVIS: WhatsApp opened a new window "
                         f"({head[0]['title'] or 'untitled'}) — see the console "
                         f"if nothing was announced.")
    except Exception as e:
        print(f"[WhatsAppGuard] window report failed: {e}")


def _worker(player, stop: threading.Event, conf: dict) -> None:
    transport, why = wa.get(conf)
    if transport is None:
        _say(player, "Tell the user, in one short sentence in their own "
                     f"language: the WhatsApp call guard could not start - {why}")
        with _lock:
            _state["running"] = False
        return

    with _lock:
        _state["transport"] = transport
    _log(player, "JARVIS: Watching for incoming WhatsApp calls.")
    _panel(player)

    next_attach = 0.0
    unresolved_since = 0.0
    last_windows: tuple = ()
    while not stop.wait(_POLL_SECONDS):
        # Enumerating windows costs about 0.6ms and only reports when the set
        # actually changes, so this is a permanent aid rather than a debug flag
        # somebody has to remember to switch on.
        try:
            now_windows = tuple(sorted(
                w["hwnd"] for w in wa._whatsapp_family(
                    getattr(transport, "_pids", None))))
        except Exception:
            now_windows = ()
        if now_windows != last_windows:
            last_windows = now_windows
            _report_windows(player, transport)

        with _lock:
            mode = _state["mode"]
            pending = _state["pending"]
            pending_at = _state["pending_at"]

        # A ring the user never answered stops being answerable.
        if pending and time.monotonic() - pending_at > _PENDING_SECONDS:
            with _lock:
                _state["pending"] = None
            _record(pending.caller, "missed")
            _panel(player)

        try:
            call = transport.incoming()
        except wa.Unsupported:
            _say(player, "Tell the user, in one short sentence in their own "
                         "language: this way of reaching WhatsApp cannot see "
                         "incoming calls, so the guard has stopped")
            break
        except Exception as e:
            # WhatsApp was closed, or the window went away. Back off rather than
            # relaunching it in the background every second. Printed EVERY time
            # rather than once per backoff window: an exception here silently
            # swallowed is how "it just does not trigger" looked from outside.
            print(f"[WhatsAppGuard] incoming() raised "
                  f"{type(e).__name__}: {e} — will retry.")
            if time.monotonic() >= next_attach:
                next_attach = time.monotonic() + _REATTACH_SECONDS
            continue

        if call is None:
            unresolved_since = 0.0
            continue

        with _lock:
            already = call.key in _state["seen"]
        if already:
            continue

        # Not yet known which control answers and which refuses. Do NOT wait for
        # it here — waiting is what made the first call of a session fail: the
        # request took seconds, the caller rang off, and by the time the answer
        # arrived there was no call left to decline. Come back in a third of a
        # second instead; the lookup was started in the background the moment
        # this call was first seen.
        if not call.identified:
            if not unresolved_since:
                unresolved_since = time.monotonic()
                print("[WhatsAppGuard] call seen, controls not resolved yet — "
                      "waiting for the lookup that just started.")
            if time.monotonic() - unresolved_since < _RESOLVE_GRACE:
                continue
            print("[WhatsAppGuard] controls still unresolved after "
                  f"{_RESOLVE_GRACE:.0f}s — handling it anyway.")

        unresolved_since = 0.0
        with _lock:
            _state["seen"].append(call.key)
            del _state["seen"][:-20]

        try:
            _handle(player, call, mode, _guard_cfg(), transport)
        except Exception as e:
            print(f"[WhatsAppGuard] handling a call failed: {e}")

    with _lock:
        _state["running"] = False
        _state["pending"] = None
    _log(player, "JARVIS: Stopped watching WhatsApp calls.")
    _panel(player)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "on").strip().lower()

    conf = wa.cfg()
    transport_cls, why = wa.choose(conf)
    if transport_cls is None:
        return f"I can't reach WhatsApp on this system: {why}."

    # -------- decisions about the call ringing right now --------
    if action in ("accept", "answer", "accept_message", "answer_message",
                  "decline", "reject", "decline_message"):
        return _decide(player, action, conf, str(params.get("message") or ""))

    # -------- probe --------
    if action in ("probe", "debug", "test"):
        threading.Thread(target=_probe, args=(player, conf), daemon=True,
                         name="whatsapp-probe").start()
        return ("status=probing seconds=30 — the user should have somebody call "
                "them on WhatsApp now; results appear in the panel")

    # -------- status --------
    if action == "status":
        with _lock:
            running, mode = _state["running"], _state["mode"]
            missed = list(_state["missed"])
        _panel(player)
        if not running:
            return ("The WhatsApp call guard is off. "
                    + (f"{len(missed)} call(s) in this session's log."
                       if missed else "Nothing logged yet."))
        recent = "; ".join(f"{who or 'unknown'} at {when} ({what})"
                           for when, who, what in missed[-4:])
        return (f"The call guard is on in {mode.replace('_', ' ')} mode via "
                f"{transport_cls.label}."
                + (f" Recent: {recent}." if recent else " No calls yet."))

    # -------- off --------
    if action in ("off", "stop", "disable"):
        with _lock:
            running, stop = _state["running"], _state["stop"]
        if not running or stop is None:
            return "The WhatsApp call guard isn't running."
        stop.set()
        return "I've stopped watching your WhatsApp calls."

    # -------- on --------
    mode = (params.get("mode") or "announce").strip().lower()
    if mode not in ("announce", "auto_reply", "auto_decline"):
        mode = "announce"

    # "If anyone calls, decline it and say I'm not available" is a complete
    # instruction: the sentence to send is IN it. Demanding the user go and fill
    # in a settings field first — having just been told what to say — is the
    # assistant refusing to listen. What they said wins; the saved setting is
    # the fallback for when they said nothing.
    gcfg = _guard_cfg()
    spoken = str(params.get("message") or "").strip()
    session_note = spoken or gcfg["busy_message"]
    if mode == "auto_decline":
        _state["message"] = ""          # nothing is ever sent in this mode
    if mode == "auto_reply" and not session_note:
        return ("I'll need to know what to tell them. What should I say to "
                "people who call — something like 'I'm not available right "
                "now'?")

    with _lock:
        if _state["running"]:
            _state["mode"] = mode
            if session_note:
                _state["message"] = session_note
            _panel(player)
            return (f"The call guard was already running — switched it to "
                    f"{mode.replace('_', ' ')} mode.")
        stop = threading.Event()
        _state.update({"running": True, "stop": stop, "mode": mode,
                       "pending": None, "seen": [], "message": session_note})

    thread = threading.Thread(target=_worker, args=(player, stop, conf),
                              daemon=True, name="whatsapp-guard")
    with _lock:
        _state["thread"] = thread
    thread.start()

    if mode == "auto_decline":
        return ("status=guarding mode=auto_decline messages=none — This is "
                "DONE. Every call will be refused and NOTHING will be sent to "
                "anyone. Confirm that in one short sentence, in the user's own "
                "language.")
    if mode == "auto_reply":
        # Say that it is SETTLED, not that it is quotable.
        #
        # This used to end with "quote this message back so they can correct
        # it", and the model read that as permission to ask — it had already
        # been given the sentence, had already stored it, and still came back
        # with "what would you like me to tell them?". A tool result that
        # describes an accomplished fact has to sound like one.
        return (f"status=guarding mode=auto_reply message_is_set=true "
                f"message=\"{session_note}\" — This is DONE. The message is "
                f"already stored and will be sent to every declined caller. Do "
                f"NOT ask the user what to say; they have already said it. "
                f"Confirm in one short sentence, in their own language, that "
                f"calls will be declined and tell them what the caller will be "
                f"sent.")
    return ("status=guarding mode=announce — This is DONE. Confirm in one "
            "short sentence, in the user's own language, that you will tell "
            "them who is calling and wait for their decision. Do not ask any "
            "further questions.")


def _probe(player, conf: dict) -> None:
    """Watch for thirty seconds and report every control that appears.

    The accept and decline controls are matched by a ~20-language vocabulary
    and, failing that, by layout — and both rules were written without ever
    having observed a real incoming call, because a ringing phone cannot be read
    out of documentation. This is how that gets fixed: somebody calls, and the
    tree says what it actually publishes. Only what is NEW since the last look
    is reported, so the steady-state interface does not bury the two lines that
    matter.
    """
    transport, why = wa.get(conf)
    if transport is None:
        _say(player, "Tell the user, in one short sentence in their own "
                     f"language: the probe could not reach WhatsApp - {why}")
        return

    seen: set[str] = set()
    fresh: list[str] = []
    deadline = time.monotonic() + 30.0
    _log(player, "JARVIS: Probing WhatsApp for 30 seconds — call me now.")

    while time.monotonic() < deadline:
        try:
            for line in transport.probe():
                if line not in seen:
                    seen.add(line)
                    fresh.append(line)
                    print(f"[WhatsAppGuard][probe] {line}")
        except Exception as e:
            print(f"[WhatsAppGuard][probe] {e}")
        time.sleep(1.0)

    body = "\n\n".join(fresh[-12:]) or "Nothing was published — WhatsApp may " \
                                       "have been closed the whole time."
    if player:
        try:
            player.show_content("🔎 WHATSAPP PROBE", body)
        except Exception:
            pass
    _log(player, f"JARVIS: Probe finished — {len(fresh)} distinct view(s) seen. "
                 f"The full list is in the console.")
    _say(player, "Tell the user, in one short sentence in their own language: "
                 "the WhatsApp probe has finished and the results are on screen "
                 "and in the console")


def _decide(player, action: str, conf: dict, message: str = "") -> str:
    """Answer or refuse the call that is ringing right now."""
    with _lock:
        call = _state["pending"]
        pending_at = _state["pending_at"]
        transport = _state["transport"]

    if call is None or transport is None:
        return "There's no WhatsApp call waiting for an answer right now."
    if time.monotonic() - pending_at > _PENDING_SECONDS:
        with _lock:
            _state["pending"] = None
        return "That call has already stopped ringing."

    # Re-read the live window rather than trusting the snapshot taken when it
    # started ringing, and this time allow the wait: the user has just spoken,
    # a button is about to be pressed, and the mapping must be right rather than
    # merely fast. In practice it is already cached — the background resolve
    # started the moment the call was first seen.
    caller = (call.caller or "").strip() or "an unknown caller"
    try:
        # The user has just spoken, so a moment's wait is affordable here in a
        # way it never is on the ring path.
        fresh = transport.incoming(blocking=True)
    except Exception as e:
        print(f"[WhatsAppGuard] re-reading the call failed: {e}")
        fresh = None

    with _lock:
        _state["pending"] = None

    if fresh is None:
        _record(call.caller, "stopped ringing")
        _panel(player)
        return f"The call from {caller} has already stopped ringing."
    if not fresh.identified:
        _record(call.caller, "controls unresolved")
        _panel(player)
        return (f"{caller} is still ringing, but I couldn't work out which "
                f"button answers and which refuses, so I haven't touched it — "
                f"you'll have to pick it up yourself. The controls it shows "
                f"are: {', '.join(fresh.controls[:6]) or 'none I can read'}.")
    call = fresh

    if action in ("accept", "answer", "accept_message", "answer_message"):
        with_message = action in ("accept_message", "answer_message")
        try:
            ok = transport.accept(call)
        except wa.Unsupported as e:
            return f"I can't answer calls through this transport — {e}."
        except Exception as e:
            return f"I couldn't answer that call: {e}."
        if not ok:
            _record(call.caller, "answer failed")
            _panel(player)
            return "I found the call but couldn't press answer."

        if not with_message:
            _record(call.caller, "answered")
            _log(player, f"JARVIS: Answered the WhatsApp call from {caller}.")
            _panel(player)
            # Nothing more is said. The call is live in the user's ear now, and
            # talking over it is exactly the noise this plugin exists to avoid.
            return f"status=answered caller={caller}"

        # Answering AND writing. The call is live from here on, so the sending
        # runs against a WhatsApp that has just swapped its conversation window
        # for a call window — which is precisely the case the message path is
        # built for, and it reports honestly when the window never comes back.
        sent, failure = _send_to_caller(transport, call.caller,
                                        _note_for(message, _guard_cfg()))
        what = "answered" + (" + message sent" if sent
                             else " (message NOT sent)")
        _record(call.caller, what)
        _log(player, f"JARVIS: WhatsApp call from {caller} — {what}.")
        _panel(player)
        if sent:
            return (f"status=answered+message_sent caller={caller} — the call "
                    f"is live and the message went to them. Say so in one "
                    f"short sentence, in the user's own language.")
        return (f"status=answered message_sent=false caller={caller} — the "
                f"call was answered, but {failure}. Tell the user plainly "
                f"that the message was NOT sent and why - do not say it was.")

    return _decline_now(player, call, transport, _guard_cfg(),
                        with_message=(action == "decline_message"),
                        message=message)
