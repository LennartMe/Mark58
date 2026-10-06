"""
Let JARVIS change its own face.

This is the voice path into the Mark 58 interface. It ends at exactly the same
validator the Ctrl+K console uses, so anything you can type into that panel you
can also ask for out loud, and the two can never drift apart.

    "Make yourself amber."                 → accent, whole palette rotates
    "Bigger sphere."                       → set sphere.radius
    "Put the transcript on the left."      → set layout.transcript_side
    "Calm everything down."                → preset quiet
    "What colour are you right now?"       → get theme.state.LISTENING
    "Keep that."                           → save

Nothing here writes to disk unless asked. A change is live immediately and
lasts for the session; `save` is what makes it survive a restart, and `revert`
is the undo. That split is deliberate — experimenting with how the thing looks
should not be a decision you have to commit to.

Requires ui.mark57.JarvisUI. Any other UI object makes the handler say
so plainly rather than raising, since the action loader keeps the tool
registered either way.
"""
from __future__ import annotations

import colorsys
import json

# Named looks. These are not random bundles — each one is a position on a
# single axis the user is likely to actually want to move along.
PRESETS: dict[str, dict] = {
    # Everything as shipped.
    "default": {},

    # For working next to. Motion off, fewer distractions, dimmer.
    "quiet": {
        "motion.reduced": True,
        "ambient.motes": 12,
        "ambient.floor": False,
        "sphere.bloom": 0.62,
        "sphere.gyro": 0,
        "sphere.spin": 0.06,
        "theme.glass.shadow": 0.35,
    },

    # For showing someone. Larger, brighter, more reactive.
    "vivid": {
        "sphere.radius": 0.30,
        "sphere.bloom": 1.35,
        "sphere.displace": 0.46,
        "sphere.halo_alpha": 0.92,
        "sphere.ring_gain": 1.35,
        "ambient.motes": 110,
        "ambient.drift_alpha": 0.72,
    },

    # More structure in the sphere itself.
    "dense": {
        "sphere.rows": 38,
        "sphere.cols": 62,
        "sphere.point": 1.9,
        "sphere.meridians": 9,
        "sphere.parallels": 6,
        "sphere.wire_alpha": 0.22,
    },

    # Nearly clear glass. Best on a dark desktop.
    "clear": {
        "theme.glass.alpha": 0.035,
        "theme.glass.edge": 0.45,
        "theme.glass.blur": 1.5,
    },

    # For a bright room, where translucency costs you the text.
    "solid": {
        "theme.glass.alpha": 0.22,
        "theme.glass.sheen": 0.10,
        "theme.glass.blur": 0.7,
        "theme.glass.shadow": 0.8,
    },

    # Sphere alone. Everything else out of the way.
    "bare": {
        "layout.show_rail": False,
        "layout.show_transcript": False,
        "sphere.radius": 0.34,
    },
}

# Colours people ask for by name. Anything not here can still be given as hex.
NAMED_COLOURS: dict[str, str] = {
    "blue": "#42d7ff",   "cyan": "#42d7ff",   "azure": "#4aa8ff",
    "violet": "#9d7bff", "purple": "#9d7bff", "indigo": "#7b74ff",
    "amber": "#ffb545",  "orange": "#ff8a3d", "gold": "#ffcf6a",
    "green": "#49e3a6",  "emerald": "#3ddc91", "mint": "#7af3c8",
    "red": "#ff5470",    "crimson": "#ff4060", "rose": "#ff7a9c",
    "pink": "#ff79c6",   "magenta": "#ff5cc8",
    "white": "#dfe9f2",  "silver": "#c3d2de",  "grey": "#9fb0bd",
    "gray": "#9fb0bd",   "teal": "#2fd4c4",
}


def _ui(player):
    """The Mark 58 facade, or None if the old interface is running."""
    return player if (player is not None and hasattr(player, "ui_patch")) else None


def _log(player, msg: str) -> None:
    try:
        if player is not None:
            player.write_log(f"SYS: {msg}")
    except Exception:
        pass
    print(f"[Interface] {msg}")


def _as_dict(value) -> dict:
    """Accept a dict, or the JSON string a model sends when the schema will not
    carry free-form object keys."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            out = json.loads(value)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_colour(name: str) -> str | None:
    s = (name or "").strip().lower()
    if s in NAMED_COLOURS:
        return NAMED_COLOURS[s]
    if s.startswith("#") and len(s) == 7:
        try:
            int(s[1:], 16)
            return s
        except ValueError:
            return None
    return None


def _accent(ui, hex_colour: str) -> dict[str, str]:
    """Rotate the whole palette to a new accent.

    Setting one colour and leaving the rest gives you a sphere that no longer
    matches its own interface. The state colours are a designed set — listening
    cold, thinking pulled toward indigo, speaking opened out — so the accent
    rotates the hue of all of them together and preserves the intervals between
    them. Saturation and value are left alone, which is what stops a bright
    accent from bleaching the whole thing.
    """
    def hsv(h: str):
        return colorsys.rgb_to_hsv(int(h[1:3], 16) / 255,
                                   int(h[3:5], 16) / 255,
                                   int(h[5:7], 16) / 255)

    base = ui.ui_get("theme.state.LISTENING", "#42d7ff")
    dh = hsv(hex_colour)[0] - hsv(base)[0]
    grey = hsv(hex_colour)[1] < 0.10

    changes: dict[str, str] = {}
    for state in ("INITIALISING", "LISTENING", "THINKING", "PROCESSING",
                  "SPEAKING", "SLEEPING"):
        cur = ui.ui_get(f"theme.state.{state}")
        if not cur:
            continue
        h, s, v = hsv(cur)
        if grey:
            s *= 0.12
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        changes[f"theme.state.{state}"] = "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))
    return changes


def interface_action(parameters: dict, player=None, session_memory=None) -> str:
    ui = _ui(player)
    if ui is None:
        return ("I can't change my appearance from here — the Mark 58 "
                "interface isn't the one running. Start it with "
                "`from ui.mark57 import JarvisUI` in main.py and I'll be able to.")

    params = parameters or {}
    action = str(params.get("action", "set")).strip().lower()

    # ── read ──────────────────────────────────────────────────────────────
    if action == "get":
        path = (params.get("path") or "").strip()
        if not path:
            spec = ui.ui_spec()
            return ("Here's everything I'm currently set to:\n"
                    + json.dumps(spec, indent=2)[:3500])
        value = ui.ui_get(path)
        if value is None:
            return f"I don't have a property called {path}."
        return f"{path} is {value}."

    if action == "options":
        opts = ui.ui_options()
        lines = [f"  {k:28s} {v}" for k, v in sorted(opts.items())]
        return "What I can change about myself:\n" + "\n".join(lines)

    if action == "presets":
        return ("Named looks: " + ", ".join(sorted(PRESETS)) +
                ". Ask for any of them by name.")

    # ── write ─────────────────────────────────────────────────────────────
    if action in ("save", "keep"):
        return ("Saved — I'll look like this next time."
                if ui.ui_save() else
                "I couldn't write config/ui_spec.json. The rest of the change "
                "is still live for this session.")

    if action in ("revert", "undo"):
        ui.ui_revert()
        return "Back to how I was the last time we saved."

    if action in ("reset", "defaults"):
        ui.ui_reset()
        return "Back to my shipped appearance. Say 'keep that' if you want it."

    if action == "console":
        ui.ui_console(True)
        return "Settings are open. Everything in there is live."

    if action == "reload":
        return ("Interface reloaded." if ui.ui_reload() else
                "The new interface code wouldn't import, so I've kept the one "
                "that's running. The error is in the transcript.")

    if action != "set":
        return (f"I don't know the action '{action}'. I can set, get, options, "
                f"presets, save, revert, reset, console or reload.")

    # ── set: preset, then accent, then explicit properties ────────────────
    changes: dict = {}
    applied: list[str] = []

    preset = (params.get("preset") or "").strip().lower()
    if preset:
        if preset not in PRESETS:
            return (f"I don't have a look called '{preset}'. I have: "
                    + ", ".join(sorted(PRESETS)) + ".")
        if preset == "default":
            ui.ui_reset()
            applied.append("reset to my default look")
        else:
            changes.update(PRESETS[preset])
            applied.append(f"switched to the {preset} look")

    accent = (params.get("colour") or params.get("color") or "").strip()
    if accent:
        hex_colour = _resolve_colour(accent)
        if hex_colour is None:
            return (f"I don't know the colour '{accent}'. Give me a name like "
                    f"amber or violet, or a hex value like #42d7ff.")
        changes.update(_accent(ui, hex_colour))
        applied.append(f"changed my colour to {accent.lower()}")

    explicit = _as_dict(params.get("changes"))
    if explicit:
        changes.update(explicit)

    if not changes:
        return ("Nothing to change — tell me a colour, a named look, or a "
                "property to set. Ask for 'options' to see the full list.")

    rejected = ui.ui_patch(changes)
    accepted = [k for k in changes if k not in rejected]

    if not accepted:
        first = list(rejected.items())[0]
        return f"I couldn't do that — {first[0]}: {first[1]}."

    if not applied:
        if len(accepted) == 1:
            applied.append(f"set {accepted[0]} to {changes[accepted[0]]}")
        else:
            applied.append(f"changed {len(accepted)} things about myself")

    msg = "Done — " + ", and ".join(applied) + "."
    if rejected:
        bad = list(rejected.items())[0]
        msg += f" I left {bad[0]} alone though: {bad[1]}."
    msg += " Say 'keep that' if you want it to stick."
    _log(player, msg)
    return msg


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "interface",
    "description": (
        "Change how you look — your colour, the sphere at the centre, the "
        "frosted glass, the layout, how much movement there is. Also reads "
        "back what you are currently set to. Changes take effect immediately "
        "and last for the session; use action='save' to keep them permanently "
        "and action='revert' to undo. Use this whenever the user comments on "
        "your appearance or asks you to change it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "set — make a change (the default). get — read one "
                    "property, or all of them if no path is given. options — "
                    "list everything that can be changed, with ranges. "
                    "presets — list the named looks. save — keep the current "
                    "look permanently. revert — undo back to the last save. "
                    "reset — return to the shipped defaults. console — open "
                    "the settings panel. reload — re-import the interface code."
                ),
            },
            "colour": {
                "type": "STRING",
                "description": (
                    "A colour name (amber, violet, green, teal, silver…) or a "
                    "hex value like #42d7ff. Rotates the whole palette to it "
                    "rather than setting one colour in isolation."
                ),
            },
            "preset": {
                "type": "STRING",
                "description": (
                    "A named look: default, quiet (calm, still, for working "
                    "next to), vivid (large and reactive), dense (more detail "
                    "in the sphere), clear (barely-there glass), solid (more "
                    "opaque glass, for a bright room), bare (sphere only)."
                ),
            },
            "changes": {
                "type": "STRING",
                "description": (
                    "JSON object of specific properties to set, keyed by "
                    "dotted path. For example "
                    "{\"sphere.radius\": 0.3, \"layout.transcript_side\": "
                    "\"left\", \"motion.reduced\": true}. Call action="
                    "'options' first if you are unsure what exists — do not "
                    "guess at property names."
                ),
            },
            "path": {
                "type": "STRING",
                "description": "For action='get': a single dotted property path.",
            },
        },
        "required": ["action"],
    },
    "handler": interface_action,
}
