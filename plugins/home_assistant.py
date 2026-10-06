"""
JARVIS plugin — Smart light control (Tuya Cloud).

Turn a Tuya / Smart Life bulb on or off, set a colour, a brightness, a white
temperature, or a mood ("ocean", "sunset", "movie", "focus"), and read its
state — all by voice.

FOR EVERYONE, not one bulb: there are no keys in this file. Each user creates a
free Tuya IoT project, enters their own Access ID/Secret, region and Device ID in
the ⚙ plugin-settings tab, and presses CONNECT & TEST. Until they do, every
command returns a short explanation of what is missing rather than an error.

Cross-OS (pure cloud HTTP, same on Windows/macOS/Linux) and every-language: the
assistant maps the user's request — in any language — onto the `action`/`color`
parameters, so there is NO language-specific keyword matching here. Return lines
are English; JARVIS re-speaks them in the user's own language.

Requires:  pip install tinytuya   (the plugin still loads and explains this if
it's missing).
"""
from __future__ import annotations

import colorsys

from memory.config_manager import get_plugin_config

_NS = "home_assistant"

# ─────────────────────────────────────────────────────────────────────────────
# No credentials live in this file. Every user enters their own in the
# ⚙ plugin-settings tab; anything still containing PUT / YOUR / HERE reads as
# "not set" and the plugin explains what to do instead of failing.
#
# Where the four values come from (iot.tuya.com, free account):
#   API Key / Secret → Cloud → your project → Overview → Access ID & Secret
#   Region           → the data centre you chose when creating the project
#   Device ID        → Cloud → Devices → your bulb → Device ID
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_API_KEY    = "PUT_YOUR_TUYA_API_KEY_HERE"
_DEFAULT_API_SECRET = "PUT_YOUR_TUYA_API_SECRET_HERE"
_DEFAULT_REGION     = "eu"          # not a secret — a sensible starting point
_DEFAULT_DEVICE_ID  = "PUT_YOUR_TUYA_DEVICE_ID_HERE"


# ── colour + mood palette (values are English tokens the assistant passes) ───
_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "white": (255, 255, 255), "warm white": (255, 200, 120), "cool white": (200, 220, 255),
    "yellow": (255, 220, 0), "orange": (255, 100, 0), "pink": (255, 80, 150),
    "purple": (150, 0, 255), "cyan": (0, 220, 255), "teal": (0, 180, 160),
    "magenta": (255, 0, 200), "coral": (255, 80, 60), "lavender": (180, 130, 255),
    "gold": (255, 180, 0), "lime": (120, 255, 0), "indigo": (60, 0, 200),
    "space": (10, 10, 80), "ocean": (0, 80, 180), "forest": (20, 100, 30),
    "sunset": (255, 60, 20), "sunrise": (255, 160, 40), "midnight": (5, 5, 30),
    "fire": (255, 50, 0), "ice": (180, 230, 255), "rose": (255, 60, 100),
    "neon": (0, 255, 100), "galaxy": (40, 0, 120), "romantic": (180, 20, 60),
    "focus": (200, 220, 255), "relax": (255, 180, 80), "sleep": (30, 5, 5),
    "party": (255, 0, 200), "study": (210, 230, 255), "movie": (10, 10, 40),
    "morning": (255, 200, 80), "cozy": (255, 140, 40), "cyberpunk": (0, 255, 180),
    "candy": (255, 100, 180),
}
_ALIASES: dict[str, str] = {
    "crimson": "red", "scarlet": "red", "sky": "ice", "navy": "midnight",
    "dark blue": "midnight", "emerald": "forest", "mint": "neon", "turquoise": "teal",
    "violet": "purple", "lilac": "lavender", "amber": "gold", "peach": "coral",
    "fuchsia": "magenta", "warm": "warm white", "cool": "cool white",
    "night": "sleep", "day": "morning", "calm": "relax", "chill": "relax",
    "energetic": "neon", "love": "romantic", "nature": "forest", "happy": "yellow",
}

PLUGIN = {
    "name": "home_assistant",
    "description": (
        "Controls the user's Tuya / Smart Life SMART LIGHT. Use when the user "
        "wants to turn the light on or off, change its colour or brightness, set "
        "a white temperature, apply a mood/scene, or ask its state — e.g. 'turn "
        "the light on/off', 'make it red', 'set it to ocean/sunset/movie/focus', "
        "'dim to 30%', 'warm white', 'is the light on'. Map the intent onto "
        "'action' and fill 'color' / 'brightness' / 'warmth' as needed. This "
        "controls a physical light only."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "What to do: 'on', 'off', 'color' (set a colour or mood — put "
                    "it in 'color'), 'brightness' (put 1-100 in 'brightness'), "
                    "'white' (put 'warm'/'neutral'/'cool' in 'warmth'), 'status'."
                ),
                "enum": ["on", "off", "color", "brightness", "white", "status"],
            },
            "color": {
                "type": "STRING",
                "description": ("Colour or mood name in English, e.g. 'red', 'blue', "
                                "'ocean', 'sunset', 'romantic', 'movie', 'focus'."),
            },
            "brightness": {"type": "INTEGER", "description": "Brightness 1-100 (for 'brightness', or with a colour)."},
            "warmth": {"type": "STRING", "description": "For 'white': 'warm', 'neutral' or 'cool'."},
        },
        "required": ["action"],
    },
}

# Rendered by the ⚙ plugin-settings "token" tab — the user's own Tuya keys.
PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "💡  SMART LIGHT (Tuya)",
    "fields": [
        {"key": "api_key", "label": "Tuya API Key (Access ID)", "type": "password",
         "placeholder": "iot.tuya.com → Cloud → your project → Access ID"},
        {"key": "api_secret", "label": "Tuya API Secret", "type": "password",
         "placeholder": "iot.tuya.com → Cloud → your project → Access Secret"},
        {"key": "region", "label": "Data Center Region", "type": "choice",
         "options": ["eu", "us", "cn", "in"], "default": "eu"},
        {"key": "device_id", "label": "Device ID", "type": "text",
         "placeholder": "iot.tuya.com → Devices → your bulb → Device ID"},
    ],
    "action": {"label": "CONNECT & TEST", "run": lambda values: _test_connection(values or {})},
}


# ── colour helpers (pure — unit-testable) ────────────────────────────────────

def _resolve_color(query: str):
    q = (query or "").lower().strip()
    if q in _COLORS:
        return q, _COLORS[q]
    if q in _ALIASES:
        tgt = _ALIASES[q]
        return tgt, _COLORS[tgt]
    for name, rgb in _COLORS.items():
        if name in q or q in name:
            return name, rgb
    for alias, target in _ALIASES.items():
        if alias in q or q in alias:
            return target, _COLORS.get(target)
    return None, None


def _rgb_to_hsv_tuya(r: int, g: int, b: int, brightness: int | None = None) -> dict:
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    v_val = max(100, int(brightness * 10)) if brightness else max(100, int(v * 1000))
    return {"h": int(h * 360), "s": int(s * 1000), "v": v_val}


# ── Tuya cloud plumbing (lazy import; credentials from config only) ──────────

def _cloud_from(values: dict):
    """Build a tinytuya.Cloud from a settings dict. Raises with a friendly
    message if the library is missing or the fields are incomplete."""
    try:
        import tinytuya
    except ImportError as e:
        raise RuntimeError("tinytuya not installed — run: pip install tinytuya") from e
    api_key = (values.get("api_key") or "").strip()
    api_secret = (values.get("api_secret") or "").strip()
    device_id = (values.get("device_id") or "").strip()
    region = (values.get("region") or "eu").strip() or "eu"
    if not (api_key and api_secret and device_id):
        raise ValueError("missing credentials")
    cloud = tinytuya.Cloud(apiRegion=region, apiKey=api_key,
                           apiSecret=api_secret, apiDeviceID=device_id)
    return cloud, device_id


def _looks_unset(v: str) -> bool:
    """True for a blank value or an open-source placeholder (PUT/YOUR/HERE/XXXX)."""
    u = (v or "").strip().upper()
    return (not u) or any(tok in u for tok in ("PUT", "YOUR", "HERE", "XXXX"))


def _effective_cfg() -> dict:
    """Token-UI values first, falling back to the private defaults above."""
    cfg = get_plugin_config(_NS)
    return {
        "api_key":    (cfg.get("api_key") or "").strip()    or _DEFAULT_API_KEY,
        "api_secret": (cfg.get("api_secret") or "").strip() or _DEFAULT_API_SECRET,
        "region":     (cfg.get("region") or "").strip()     or _DEFAULT_REGION,
        "device_id":  (cfg.get("device_id") or "").strip()  or _DEFAULT_DEVICE_ID,
    }


def _cloud():
    return _cloud_from(_effective_cfg())


def _send(commands: list[dict]) -> None:
    cloud, device_id = _cloud()
    result = cloud.sendcommand(device_id, {"commands": commands})
    if isinstance(result, dict) and result.get("success") is False:
        raise RuntimeError(result.get("msg") or "cloud rejected the command")


def _read_status() -> dict:
    cloud, device_id = _cloud()
    status = cloud.getstatus(device_id)
    if not status or status.get("success") is False:
        raise RuntimeError((status or {}).get("msg") or "could not read status")
    return {item["code"]: item["value"]
            for item in status.get("result", []) if "code" in item}


# ── light operations ─────────────────────────────────────────────────────────

def _set_color(color: str, brightness: int | None = None) -> str:
    name, rgb = _resolve_color(color)
    if rgb is None:
        return f"I don't know the colour '{color}'."
    _send([
        {"code": "switch_led", "value": True},
        {"code": "work_mode", "value": "colour"},
        {"code": "colour_data_v2", "value": _rgb_to_hsv_tuya(*rgb, brightness=brightness)},
    ])
    return f"Set the light to {name}." + (f" at {brightness}%." if brightness else "")


def _set_brightness(level: int) -> str:
    level = max(1, min(100, int(level)))
    _send([
        {"code": "switch_led", "value": True},
        {"code": "bright_value_v2", "value": max(10, level * 10)},
    ])
    return f"Brightness set to {level}%."


def _set_white(warmth: str = "warm") -> str:
    warmth = (warmth or "warm").lower()
    temp = {"warm": 0, "neutral": 500, "cool": 1000}.get(warmth, 0)
    _send([
        {"code": "switch_led", "value": True},
        {"code": "work_mode", "value": "white"},
        {"code": "temp_value_v2", "value": temp},
        {"code": "bright_value_v2", "value": 800},
    ])
    return f"Set the light to {warmth} white."


def _status_text() -> str:
    dps = _read_status()
    if not dps.get("switch_led", False):
        return "The light is off."
    mode = dps.get("work_mode", "white")
    bri = max(1, int(dps.get("bright_value_v2", 1000) / 10))
    return f"The light is on — {mode} mode, {bri}% brightness."


# ── settings-tab CONNECT & TEST button ───────────────────────────────────────

def _test_connection(values: dict):
    """Runs in the settings tab's background thread → (ok, message)."""
    merged = {**_effective_cfg(), **{k: v for k, v in (values or {}).items() if v}}
    try:
        cloud, device_id = _cloud_from(merged)
    except RuntimeError as e:            # missing library
        return False, str(e)
    except ValueError:
        return False, "Enter your API key, secret and device id first."
    try:
        status = cloud.getstatus(device_id)
    except Exception as e:
        return False, f"Couldn't reach Tuya Cloud: {e}"
    if not status or status.get("success") is False:
        msg = (status or {}).get("msg", "check the keys, region and device id")
        return False, f"Connected to the cloud but the device failed: {msg}"
    dps = {i["code"]: i["value"] for i in status.get("result", []) if "code" in i}
    on = "on" if dps.get("switch_led") else "off"
    return True, f"Connected ✓ — the light is currently {on}."


# ── entry point ───────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = (parameters.get("action") or "").strip().lower()
    if not action:
        return "What should I do with the light — turn it on or off, set a colour, or a brightness?"

    # Fast, friendly guard before touching the network (uses defaults if the
    # token UI is blank; only complains if BOTH are unset / placeholders).
    eff = _effective_cfg()
    if any(_looks_unset(eff[k]) for k in ("api_key", "api_secret", "device_id")):
        return ("The smart light isn't set up yet. Open the plugin settings, add "
                "your Tuya API key, secret and device id, then press Connect & Test.")

    try:
        brightness = parameters.get("brightness")
        try:
            brightness = int(brightness) if brightness not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            brightness = None

        if action == "on":
            _send([{"code": "switch_led", "value": True}])
            result = "The light is on."
        elif action == "off":
            _send([{"code": "switch_led", "value": False}])
            result = "The light is off."
        elif action == "color":
            result = _set_color(parameters.get("color") or "", brightness)
        elif action == "brightness":
            if brightness is None:
                return "What brightness would you like, from 1 to 100 percent?"
            result = _set_brightness(brightness)
        elif action == "white":
            result = _set_white(parameters.get("warmth") or "warm")
        elif action == "status":
            result = _status_text()
        else:
            return f"I don't know the light action '{action}'."
    except RuntimeError as e:
        emsg = str(e).lower()
        if "tinytuya" in emsg:
            return "I need the Tuya library first. Run: pip install tinytuya"
        if any(w in emsg for w in ("token", "auth", "1010", "sign")):
            return ("Tuya authorization failed — double-check your API key, secret "
                    "and region in the plugin settings.")
        return f"The light command failed: {e}"
    except Exception as e:
        return f"Sorry, I couldn't control the light: {e}"

    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result
