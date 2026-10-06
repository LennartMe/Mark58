"""
actions/self_reflect.py — the assistant checks its own work.

WHAT THIS IS NOT
    It is not asking a language model how it thinks it is doing. That produces
    a paragraph shaped like a self-assessment regardless of whether anything is
    wrong, costs a round trip, and is unfalsifiable. If the assistant had
    failed `open_app` thirty times that day, a model asked to introspect would
    have no way to know.

WHAT IT IS
    Arithmetic over core/cognition.py's episode ledger, which every action and
    plugin writes to automatically. Failure rates per tool, tools that can
    rarely verify their own work, tools that are slow enough to be holding up
    the conversation, and — the one that matters most — identical calls that
    failed repeatedly, which is the fingerprint of a loop the assistant is not
    learning from.

    The model still writes the sentence the user hears. It just writes it from
    counts rather than from an impression of itself.

THE FEEDBACK LOOP
    action='learn_from_failures' closes the circle: it takes the repeated
    failures out of the ledger and writes them into the knowledge store as
    claims, so the next attempt at the same thing has a prior attached to it.
    That is the actual mechanism by which this system improves without a person
    editing anything — reflection finds the pattern, knowledge remembers it,
    the next call reads it back.
"""
from __future__ import annotations

from core import cognition, outcome


def self_reflect(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "reflect")).strip().lower()
    try:
        window = max(1, min(int(parameters.get("window_hours") or 168), 24 * 90))
    except (TypeError, ValueError):
        window = 168

    try:
        if action in ("reflect", "assess", "review", "self_check", "how_am_i_doing"):
            report = cognition.reflect(window_hours=window)
            findings = report["findings"]
            facts = {"calls": report["total_calls"], "window_hours": window}
            for idx, finding in enumerate(findings[:6], start=1):
                facts[f"finding_{idx}"] = (
                    f"{finding['severity']}: {finding['area']} — {finding['detail']}")
                if finding.get("suggestion"):
                    facts[f"fix_{idx}"] = finding["suggestion"][:140]
            problems = sum(1 for f in findings if f["severity"] == "problem")
            return outcome.done("self_reflect", problems=problems,
                                findings=len(findings), **facts)

        if action in ("tools", "tool_stats", "performance"):
            stats = cognition.tool_stats(window)
            if not stats:
                return outcome.done("self_reflect", calls=0, window_hours=window)
            facts = {}
            for idx, row in enumerate(stats[:8], start=1):
                facts[f"tool_{idx}"] = (
                    f"{row['name']} calls={row['calls']} "
                    f"fail={row['failure_rate']:.0%} avg={row['avg_ms']}ms")
            return outcome.done("self_reflect", tools=len(stats), **facts)

        if action in ("failures", "recent_failures", "what_went_wrong"):
            rows = cognition.recent(limit=10, status="failed")
            if not rows:
                return outcome.done("self_reflect", failures=0, window_hours=window)
            facts = {}
            for idx, row in enumerate(rows[:6], start=1):
                facts[f"failure_{idx}"] = (
                    f"{row['name']}: {(row['error'] or row['result'] or '')[:120]}")
            return outcome.done("self_reflect", failures=len(rows), **facts)

        if action in ("learn_from_failures", "learn", "improve"):
            # The loop: turn repeated failures into priors.
            report = cognition.reflect(window_hours=window, persist=False)
            learned = 0
            for finding in report["findings"]:
                if finding["severity"] != "problem":
                    continue
                claim = (f"The tool '{finding['area']}' has been unreliable: "
                         f"{finding['detail']}. "
                         f"{finding.get('suggestion', '')}").strip()
                if cognition.learn("self_performance", claim, source="reflection",
                                   confidence=0.7):
                    learned += 1
            if not learned:
                return outcome.done("self_reflect", learned=0,
                                    note="no_problems_worth_recording")
            return outcome.done("self_reflect", learned=learned,
                                topic="self_performance")

        if action in ("history", "past_reflections"):
            rows = cognition.reflection_history(limit=5)
            if not rows:
                return outcome.done("self_reflect", reflections=0)
            facts = {f"pass_{i}": row["summary"][:140]
                     for i, row in enumerate(rows, start=1)}
            return outcome.done("self_reflect", reflections=len(rows), **facts)

        return outcome.failed("self_reflect", "unknown_action", action=action)

    except Exception as e:
        return outcome.failed("self_reflect", f"{type(e).__name__}:{e}", action=action)


TOOL = {
    "name": "self_reflect",
    "description": (
        "Examines your OWN recent behaviour from a recorded ledger of every "
        "tool call: which tools are failing, which cannot verify their own "
        "work, which are slow, and which calls have failed repeatedly with "
        "identical arguments. "
        "Use action='reflect' when the user asks how you are doing, whether "
        "something is wrong with you, why you keep getting something wrong, or "
        "asks you to check yourself. "
        "Use action='failures' for 'what went wrong earlier'. "
        "Use action='learn_from_failures' after a reflection turns up real "
        "problems — it writes them into your knowledge store so the next "
        "attempt starts with what you found out. "
        "These are measured counts, not impressions: report the numbers as "
        "they come back and do not soften them."
    ),
    "brief": "Self-assessment from a ledger of your own tool calls: failure rates, slow tools, repeated failures.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("reflect | tools | failures | learn_from_failures "
                                "| history"),
            },
            "window_hours": {
                "type": "STRING",
                "description": "How far back to look. Default 168 (one week).",
            },
        },
        "required": ["action"],
    },
    "handler": self_reflect,
}
