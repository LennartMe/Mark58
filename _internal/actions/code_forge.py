"""
actions/code_forge.py — the assistant writes itself a new tool.

WHAT THE MODEL CAN REACH FROM HERE
    propose   generate a complete new plugin and stage it
    list      what is staged and waiting
    inspect   the audit findings and smoke-test result for one proposal
    discard   throw one away

WHAT IT CANNOT REACH FROM HERE
    approve. There is no action for it in this file and there is no parameter
    that reaches it. Installing a staged plugin requires a token that only the
    local Mark58 approval popup mints when the user presses YES, and core/forge.py
    refuses an approval that arrives without one.

    This is the same construction as core/confirm.py, and the reasoning in that
    file's header applies here word for word: a gate whose key the model can
    write is not a gate. The difference is only in the stakes — confirm.py
    guards a shutdown, this guards adding new executable code to a running
    assistant that has microphone access and control of the desktop.

WHY THE MODEL IS TOLD TO SAY SO OUT LOUD
    The tool description ends by requiring that the model tell the user a
    proposal is WAITING, rather than reporting that it has extended itself. A
    model that says "I've added that capability" when the code is sitting
    unreviewed in a staging folder has produced exactly the confident false
    success this project rewrote `open_app` to stop producing.
"""
from __future__ import annotations

import json

from core import forge, outcome


def _normalise_parameters(parameters) -> tuple[dict, str]:
    """Accept the few argument shapes model/tool bridges commonly produce.

    Gemini normally sends an object, but provider adapters and hand-written
    callers can hand over a JSON string or wrap the object under ``arguments`` /
    ``parameters``. The old handler called .get() immediately, turning those
    harmless shape differences into a failed forge call.
    """
    if parameters is None:
        return {}, ""
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except Exception as e:
            return {}, f"parameters_were_not_valid_json:{e}"
    if not isinstance(parameters, dict):
        return {}, f"parameters_must_be_an_object_not_{type(parameters).__name__}"

    params = dict(parameters)
    for key in ("arguments", "parameters", "args"):
        nested = params.get(key)
        if isinstance(nested, dict) and not any(
                k in params for k in ("action", "name", "goal", "notes")):
            params = dict(nested)
            break

    # Provider aliases seen in function-call bridges. They do not weaken the
    # tool schema; they only prevent a valid intent being discarded because a
    # bridge renamed one field.
    if "name" not in params and "plugin_name" in params:
        params["name"] = params.get("plugin_name")
    if "goal" not in params and "description" in params:
        params["goal"] = params.get("description")
    if "notes" not in params and "constraints" in params:
        params["notes"] = params.get("constraints")
    if not params.get("action") and (params.get("name") or params.get("goal")):
        params["action"] = "propose"
    return params, ""


def code_forge(parameters: dict, player=None) -> str:
    parameters, shape_error = _normalise_parameters(parameters)
    if shape_error:
        return outcome.failed("code_forge", shape_error)

    action = str(parameters.get("action", "list")).strip().lower()
    name = str(parameters.get("name", "")).strip()
    goal = str(parameters.get("goal", "")).strip()
    notes = str(parameters.get("notes", "")).strip()

    try:
        if action in ("propose", "create", "build", "generate", "write"):
            if not name or not goal:
                return outcome.ask("code_forge",
                                   "need_both_a_short_name_and_what_it_should_do",
                                   action="propose")
            if player is not None:
                try:
                    player.write_log(f"JARVIS: forging a new tool — {name}")
                except Exception:
                    pass

            def _say(text):
                if player is not None:
                    try:
                        player.write_log(f"JARVIS: {text}")
                    except Exception:
                        pass

            prop = forge.generate(name, goal, notes, notify=_say)

            blocking = [i for i in prop.issues if i.level == "block"]
            reviewing = [i for i in prop.issues if i.level == "review"]

            if blocking:
                return outcome.failed(
                    "code_forge", "audit_blocked", name=prop.name,
                    blocked=len(blocking), attempts=prop.attempts,
                    first=f"line {blocking[0].line}: {blocking[0].detail}",
                    note="the generated code used something that cannot be "
                         "statically checked, and rewriting it did not help; "
                         "nothing was installed")

            if not prop.smoke.get("ok"):
                return outcome.failed(
                    "code_forge", "smoke_test_failed", name=prop.name,
                    attempts=prop.attempts,
                    detail=str(prop.smoke.get("error", ""))[:200],
                    note="nothing was installed")

            return outcome.done(
                "code_forge", staged=prop.name, sha=prop.sha,
                review_findings=len(reviewing),
                attempts=prop.attempts,
                risk=prop.risk,
                touches=",".join(prop.caps.get("touches") or []) or "nothing",
                replaces=prop.replaces or "nothing",
                awaiting="human_approval_in_local_yes_no_popup",
                note="written_and_checked_but_NOT_active")

        if action in ("list", "pending", "queue"):
            items = forge.pending()
            if not items:
                return outcome.done("code_forge", pending=0)
            facts = {}
            for idx, item in enumerate(items[:6], start=1):
                flag = "BLOCKED" if item.get("blocked") else \
                       ("clean" if item.get("clean") else "needs_review")
                facts[f"proposal_{idx}"] = f"{item['name']} [{flag}] — {item['goal'][:80]}"
            return outcome.done("code_forge", pending=len(items), **facts)

        if action in ("inspect", "show", "details", "review"):
            if not name:
                return outcome.ask("code_forge", "which_proposal", action="inspect")
            items = {i["name"]: i for i in forge.history(60)}
            item = items.get(forge._clean_name(name))
            if not item:
                return outcome.failed("code_forge", "no_such_proposal", name=name)
            facts = {"name": item["name"], "status": item["status"],
                     "goal": item["goal"][:160],
                     "risk": item.get("risk", "unknown"),
                     "attempts": item.get("attempts", 1),
                     "touches": ",".join(
                         (item.get("caps") or {}).get("touches") or []) or "nothing",
                     "smoke": "passed" if item.get("smoke", {}).get("ok") else "failed"}
            for idx, issue in enumerate(item.get("issues", [])[:6], start=1):
                facts[f"finding_{idx}"] = (
                    f"{issue['level']} line {issue['line']}: {issue['detail']}")
            return outcome.done("code_forge", findings=len(item.get("issues", [])),
                                **facts)

        if action in ("revise", "redo", "rework", "fix", "improve"):
            # Asking for a different draft of something that is staged, not
            # installed, and cannot install itself is not a privileged act, so
            # unlike approve this one is reachable. The rewrite lands back in
            # staging behind the same popup.
            if not name:
                return outcome.ask("code_forge", "which_proposal", action="revise")
            if not notes:
                return outcome.ask("code_forge", "what_should_change",
                                   action="revise", name=name)
            prop = forge.revise(name, notes)
            if prop.is_blocked or not prop.smoke.get("ok"):
                return outcome.failed(
                    "code_forge", "revision_did_not_pass_checks", name=prop.name,
                    attempts=prop.attempts,
                    detail=str(prop.smoke.get("error", ""))[:160],
                    note="nothing was installed")
            return outcome.done(
                "code_forge", revised=prop.name, sha=prop.sha,
                attempts=prop.attempts, risk=prop.risk,
                awaiting="human_approval_in_local_yes_no_popup",
                note="rewritten_and_checked_but_NOT_active")

        if action in ("discard", "reject", "delete", "cancel"):
            if not name:
                return outcome.ask("code_forge", "which_proposal", action="discard")
            if forge.discard(name, notes):
                return outcome.done("code_forge", discarded=name)
            return outcome.failed("code_forge", "no_such_proposal", name=name)

        if action in ("approve", "install", "activate", "enable"):
            # Reached only when the model tries anyway. Answer plainly rather
            # than with an error, so it says something true to the user instead
            # of retrying with different arguments.
            return outcome.failed(
                "code_forge", "approval_is_not_available_to_you", name=name,
                note="installing generated code requires the user to review and "
                     "approve it in the local Yes/No popup; tell them it is "
                     "waiting there")

        return outcome.failed("code_forge", "unknown_action", action=action)

    except Exception as e:
        return outcome.failed(
            "code_forge", "generation_pipeline_error", action=action,
            error_type=type(e).__name__, detail=str(e)[:240])


TOOL = {
    "name": "code_forge",
    "description": (
        "Writes a NEW tool for yourself when the user asks for something no "
        "existing tool covers — a capability gap, a driver for a device you "
        "cannot currently reach, a small integration nobody has built. "
        "Use action='propose' with a short snake_case 'name' and a clear 'goal'. "
        "Before proposing, be sure no existing tool already does it; a new tool "
        "that duplicates one of yours makes you worse, not better. "
        "CRITICAL — WHAT TO SAY AFTERWARDS: a proposal is generated, checked and "
        "STAGED. It is NOT running and you cannot make it run. Tell the user it "
        "has been written and is waiting for them to approve it in the local "
        "Mark58 Yes/No popup. Never say you have gained a new ability, never "
        "say it is ready to use, and never claim to have installed anything. "
        "If the status comes back 'failed', the code was rejected by the safety "
        "audit or would not run — say that plainly and say what was wrong. "
        "A failed proposal has already been rewritten and re-checked several "
        "times automatically, so do not simply call propose again with the "
        "same goal; either change the goal or tell the user it cannot be built "
        "this way. Use action='revise' with 'notes' when the user asks for a "
        "change to something already staged."
    ),
    "brief": (
        "Generates a new plugin for a capability you lack, then stages it for the "
        "user to approve in a local Yes/No popup. Cannot install anything itself."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "propose | list | inspect | revise | discard",
            },
            "name": {
                "type": "STRING",
                "description": ("snake_case name for the new tool, e.g. "
                                "'desk_lamp_control'. Short and descriptive."),
            },
            "goal": {
                "type": "STRING",
                "description": (
                    "What the tool must do, in enough detail that someone could "
                    "build it without asking a follow-up question. Include what "
                    "it takes as input and what it should return."
                ),
            },
            "notes": {
                "type": "STRING",
                "description": ("Any extra constraints: an API to use, a device "
                                "address, a format to return."),
            },
        },
        "required": ["action"],
    },
    "handler": code_forge,
}
