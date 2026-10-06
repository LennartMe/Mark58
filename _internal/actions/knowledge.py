"""
actions/knowledge.py — the continuous-learning surface.

HOW THIS DIFFERS FROM THE EXISTING `remember` TOOL
    memory/memory_manager.py stores curated facts about the USER: their name,
    that they prefer Celsius, where their vault lives. Small, hand-written,
    loaded into the prompt whole.

    This stores what the assistant has WORKED OUT, about anything — that the
    printer at this address needs a 3-second settle before it accepts a job,
    that "the big screen" means display 2, that this user's `wp` always means
    WhatsApp and never WordPress. Volume is high, the source is experience
    rather than instruction, and each claim carries a confidence that moves as
    evidence arrives.

    The important property: calling learn() twice on the same claim is a
    CONFIRMATION, not a duplicate. That is what makes it safe for the assistant
    to record the same conclusion every time it reaches it, without curating,
    and what makes confidence mean something after a week.

WHY THE MODEL IS TOLD TO RECORD CORRECTIONS SPECIFICALLY
    The single highest-value thing to learn is the thing the user just had to
    say twice. A correction is a labelled training example arriving in real
    time, and it is exactly what was being thrown away: the assistant would be
    corrected, apologise, comply, and arrive at the identical mistake the next
    day. The tool description leans on this deliberately.
"""
from __future__ import annotations

from core import cognition, outcome


def knowledge(parameters: dict, player=None) -> str:
    action = str(parameters.get("action", "recall")).strip().lower()
    topic = str(parameters.get("topic", "")).strip()
    claim = str(parameters.get("claim", "")).strip()
    query = str(parameters.get("query", "")).strip()

    try:
        if action in ("learn", "remember", "record", "note"):
            if not claim:
                return outcome.ask("knowledge", "what_should_be_learned", action="learn")
            row = cognition.learn(topic or "general", claim,
                                  source=str(parameters.get("source", "conversation")))
            if not row:
                return outcome.failed("knowledge", "empty_claim")
            return outcome.done("knowledge", learned=row["claim"][:120],
                                topic=row["topic"],
                                confidence=round(row["confidence"], 2),
                                confirmations=row["hits"])

        if action in ("recall", "search", "lookup", "what_do_i_know"):
            rows = cognition.recall(query or claim, topic=topic, limit=6)
            if not rows:
                return outcome.done("knowledge", found=0,
                                    searched=(query or topic or "everything")[:80])
            facts = {}
            for idx, row in enumerate(rows, start=1):
                facts[f"claim_{idx}"] = (f"[{row['topic']}] {row['claim'][:160]} "
                                         f"(confidence {row['confidence']:.2f})")
            return outcome.done("knowledge", found=len(rows), **facts)

        if action in ("refute", "wrong", "correct", "contradict"):
            if not claim:
                return outcome.ask("knowledge", "which_claim_was_wrong", action="refute")
            if cognition.refute(topic or "general", claim):
                return outcome.done("knowledge", refuted=claim[:120],
                                    note="confidence_lowered_not_deleted")
            return outcome.failed("knowledge", "claim_not_held", claim=claim[:120])

        if action in ("forget", "delete", "remove"):
            if not topic:
                return outcome.ask("knowledge", "which_topic_or_claim", action="forget")
            removed = cognition.forget(topic, claim)
            if removed:
                return outcome.done("knowledge", forgotten=removed, topic=topic)
            return outcome.failed("knowledge", "nothing_matched", topic=topic)

        if action in ("stats", "summary", "status"):
            stats = cognition.knowledge_stats()
            facts = {f"topic_{i}": f"{t['topic']}={t['count']}"
                     for i, t in enumerate(stats["topics"][:6], start=1)}
            return outcome.done("knowledge", total=stats["total"],
                                stale=stats["stale"],
                                low_confidence=stats["low_confidence"], **facts)

        return outcome.failed("knowledge", "unknown_action", action=action)

    except Exception as e:
        return outcome.failed("knowledge", f"{type(e).__name__}:{e}", action=action)


TOOL = {
    "name": "knowledge",
    "description": (
        "Your own accumulated learning about this machine, this user and how "
        "things actually behave here — separate from the user's stored personal "
        "facts, which are handled elsewhere. "
        "Call action='learn' whenever you WORK SOMETHING OUT that would save "
        "effort next time: a correction the user just made, a name they use for "
        "something, a step that turned out to be needed, a tool argument that "
        "turned out to be wrong. Do this without being asked — a correction you "
        "do not record is a correction you will need again. "
        "Call action='recall' before a task you have done before, to check "
        "whether you already learned something about it. "
        "Call action='refute' when something you had learned turns out to be "
        "false; this lowers its confidence rather than deleting it. "
        "Learning the same thing twice is a confirmation, not a duplicate — "
        "record freely."
    ),
    "brief": "Records and recalls what you have learned from experience; learn on corrections, recall before repeat tasks.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "learn | recall | refute | forget | stats",
            },
            "topic": {
                "type": "STRING",
                "description": (
                    "A short category for the claim: 'printer', 'displays', "
                    "'user_vocabulary', 'spotify'. Keep these reusable — the "
                    "same topic string for the same subject every time."
                ),
            },
            "claim": {
                "type": "STRING",
                "description": (
                    "The thing learned, as one plain sentence that will still "
                    "make sense in a month with no surrounding conversation. "
                    "'The user's \"big screen\" means display 2', not 'it's the "
                    "second one'."
                ),
            },
            "query": {
                "type": "STRING",
                "description": "For recall: what you are trying to remember about.",
            },
            "source": {
                "type": "STRING",
                "description": "Where this came from: conversation | observation | correction.",
            },
        },
        "required": ["action"],
    },
    "handler": knowledge,
}
