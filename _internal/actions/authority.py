"""
actions/authority.py — the assistant can say how much authority it has, and
change it when the user tells it to.

WHY THIS IS A TOOL AND NOT JUST A CONSOLE SETTING
    The console is the place you go when you are already at the machine. Most of
    the time the user is not — they are across the room saying "stop asking me,
    just do it". Without a tool, that sentence has nowhere to go: the model
    would apologise, promise to stop asking, and then ask again on the next
    shutdown, because the policy it is apologising about lives in a file it
    cannot reach.

THE ONE THING THIS TOOL MUST NOT BECOME
    A way for the model to grant itself authority. Everything about
    core/autonomy.py exists because a `confirmed=yes` parameter written by the
    model is not a confirmation. Handing the model `raise_level()` would rebuild
    that hole with extra steps.

    So the rule here is direction-dependent, and it is the only rule that
    matters in this file:

        LOWERING  authority — always allowed, no questions
        RAISING   authority — only ever as the immediate result of the user
                  saying so, and never to `sovereign`, which is a setting a
                  person selects deliberately on a screen that tells them what
                  it removes

    The model can still lie about having been asked. That is unavoidable in any
    voice interface and is not what this defends against: the defence is that
    every change is written to `level_changes` with `who='voice'`, so a level
    the user did not ask for is visible in the console next to the moment it
    happened, and HALT is one word away.

    `sovereign` is deliberately unreachable from here. It is the level with no
    floor and no rate limit, and a mis-heard sentence should not be able to
    select it.
"""
from __future__ import annotations

from core import autonomy, outcome

# What the model is allowed to select by voice. `sovereign` is absent on
# purpose — see the docstring.
VOICE_SELECTABLE = ("ask", "guarded", "full")

_PLAIN = {
    "ask":       "asks before anything it cannot undo",
    "guarded":   "acts on its own, but still asks before a shutdown or closing an app",
    "full":      "acts on everything without asking, including shutdown",
    "sovereign": "acts on everything with no floor and no rate limit",
}


def authority(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "status")).strip().lower()

    try:
        # ── what am I allowed to do ───────────────────────────────────────────
        if action == "status":
            st = autonomy.stats(24)
            br = st["breaker"]
            return outcome.done(
                "authority",
                level=st["level"],
                means=_PLAIN.get(st["level"], ""),
                halted=st["halted"],
                ran_without_asking_today=st["allowed"],
                asked_today=st["asked"],
                refused_today=st["denied"],
                recent_failures=f"{br['failures']}/{br['samples']}",
                pinned=", ".join(sorted(autonomy.policy().rules)) or "nothing",
            )

        # ── change it ─────────────────────────────────────────────────────────
        if action == "set":
            want = str(parameters.get("level", "")).strip().lower()

            if want == "sovereign":
                return outcome.failed(
                    "authority", "sovereign_not_settable_by_voice",
                    explain=("Sovereign removes the floor that keeps HALT reachable. "
                             "Tell the user it is a deliberate choice they make on the "
                             "console at /console, not something you will set for them."))

            if want not in VOICE_SELECTABLE:
                return outcome.failed("authority", "unknown_level", asked_for=want,
                                      available=", ".join(VOICE_SELECTABLE))

            current = autonomy.level()
            if want == current:
                return outcome.done("authority", level=current, changed=False,
                                    note="already at this level")

            raising = autonomy._RANK[want] > autonomy._RANK[current]
            got = autonomy.set_level(want, who="voice",
                                     reason="the user asked out loud")

            if got != want:
                return outcome.failed("authority", "level_did_not_change",
                                      level=got)

            return outcome.done(
                "authority", level=got, previous=current,
                means=_PLAIN.get(got, ""),
                direction="raised" if raising else "lowered",
                note=("Say in one short sentence what you will now do without asking. "
                      "Do not thank them and do not list the levels."))

        # ── stop ──────────────────────────────────────────────────────────────
        if action == "halt":
            autonomy.halt("the user asked out loud")
            return outcome.done(
                "authority", halted=True,
                note=("Everything that touches this machine is now refused. Say so "
                      "in one short sentence and stop what you were doing."))

        if action == "resume":
            lifted = autonomy.resume(who="voice")
            return outcome.done("authority", halted=False, was_halted=lifted,
                                level=autonomy.level())

        # ── why did it ask / why did it refuse ────────────────────────────────
        if action == "recent":
            rows = autonomy.recent(12)
            if not rows:
                return outcome.done("authority", decisions=0,
                                    note="nothing has gone through the gate yet")
            facts = {
                f"{i}": f"{r['title']} — {r['verdict']} ({r['reason']})"
                for i, r in enumerate(rows, start=1)
            }
            return outcome.done("authority", decisions=len(rows), **facts)

        return outcome.failed("authority", "unknown_action", action=action)

    except Exception as e:
        return outcome.failed("authority", f"{type(e).__name__}:{e}", action=action)


TOOL = {
    "name": "authority",
    "description": (
        "Reads and changes how much you are allowed to do without asking. "
        "Use action='status' when the user asks what you can do on your own, "
        "why you asked them something, or whether you are stopped. "
        "Use action='set' with level='full' when they tell you to stop asking, "
        "to just do things, or to act on your own; level='guarded' to go back to "
        "asking only before a shutdown; level='ask' when they want to approve "
        "everything again. You CANNOT set level='sovereign' — that is chosen by "
        "the user on the console. "
        "Use action='halt' the moment they tell you to stop, and action='resume' "
        "only when they explicitly say to carry on. "
        "Use action='recent' for 'why did you refuse that' or 'what have you been "
        "doing on your own'. "
        "Never raise your own authority unless the user has just asked you to in "
        "that turn. Report what changed in one short sentence; do not read the "
        "levels out as a list."
    ),
    "brief": "Read or change how much you can do without asking, and stop everything.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | set | halt | resume | recent",
            },
            "level": {
                "type": "STRING",
                "description": ("Only with action='set'. One of: ask, guarded, full. "
                                "'sovereign' is refused here by design."),
            },
        },
        "required": ["action"],
    },
    "handler": authority,
}
