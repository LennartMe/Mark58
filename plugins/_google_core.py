"""
plugins/_google_core.py — shared Google OAuth + API layer for the JARVIS
Google plugin suite (gmail_control, calendar_control, …).

The leading underscore matters: core/plugin_loader.py SKIPS files starting with
"_", so this is a shared helper, never loaded as a plugin. The real plugins
import from it:

    from plugins._google_core import build_settings, get_service, GoogleError

Design goals — this ships to a WHOLE COMMUNITY, so:
  • NOTHING is hard-coded. Every user connects THEIR OWN Google account with
    THEIR OWN OAuth "Desktop app" client id/secret, entered in the ⚙ plugin-
    settings tab (the token screen). No shared secret is baked into the code.
  • Credentials + the resulting token live ONLY in config/api_keys.json under
    plugin_config[<namespace>]  (via memory.config_manager). Never in code.
  • Cross-OS: the OAuth consent uses a loopback local-server flow, which works
    the same on Windows, macOS and Linux (it just opens the user's browser).
  • Language-neutral: this file returns English status/errors; the plugins hand
    English directives to JARVIS, which re-speaks them in the user's language.

Third-party deps (installed by requirements.txt, imported LAZILY so a missing
one can never stop the module importing or the plugin loading):
    google-auth-oauthlib   google-api-python-client   google-auth

How a user sets one up (put this in each plugin's field placeholders / docs):
  1. console.cloud.google.com → create a project → enable the Gmail / Calendar API
  2. OAuth consent screen → External → add yourself as a test user
  3. Credentials → Create → OAuth client ID → "Desktop app"
  4. Paste the Client ID + Client secret into the plugin settings, press AUTHORIZE
"""
from __future__ import annotations

import json

from memory.config_manager import get_plugin_config, save_plugin_config


# ── error taxonomy (plugins translate these into calm spoken guidance) ───────
class GoogleError(Exception):
    pass


class GoogleConfigError(GoogleError):
    """No client id/secret entered yet."""


class GoogleAuthError(GoogleError):
    """Not authorized yet, or the stored token is unusable."""


class GoogleDependencyError(GoogleError):
    """A required Google library is not installed."""


_TOKEN_KEY = "token"   # where the serialized OAuth credentials live, per namespace


# ── settings-schema builder (identical fields for every Google plugin) ───────
def build_settings(namespace: str, title: str, scopes: list[str]) -> dict:
    """Return a ready PLUGIN_SETTINGS dict: client id/secret fields + an
    AUTHORIZE button that runs the browser consent flow for `scopes` and stores
    the token under `namespace`. Each plugin just assigns the result to its
    module-level PLUGIN_SETTINGS constant."""
    def _authorize(values: dict):
        return authorize(namespace, scopes, values or {})

    return {
        "namespace": namespace,
        "title": title,
        "fields": [
            {"key": "client_id", "label": "OAuth Client ID", "type": "text",
             "placeholder": "…apps.googleusercontent.com  (Google Cloud → Desktop app)"},
            {"key": "client_secret", "label": "OAuth Client Secret", "type": "password",
             "placeholder": "GOCSPX-…  (from the same OAuth client)"},
        ],
        "action": {"label": "AUTHORIZE (opens your browser)", "run": _authorize},
    }


# ── OAuth: the AUTHORIZE button ──────────────────────────────────────────────
def authorize(namespace: str, scopes: list[str], values: dict):
    """Run Google's loopback consent flow and store the token. Returns
    (ok: bool, message: str) — shown verbatim in the settings panel."""
    cfg = {**get_plugin_config(namespace), **(values or {})}
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = (cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return False, "Enter your OAuth Client ID and Client Secret first."

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return False, ("Missing library. Run:  pip install google-auth-oauthlib "
                       "google-api-python-client")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        # port=0 → pick any free port; opens the browser, blocks until consent.
        creds = flow.run_local_server(port=0, prompt="consent",
                                      authorization_prompt_message="")
    except Exception as e:
        return False, f"Authorization failed or was cancelled: {e}"

    try:
        save_plugin_config(namespace, {_TOKEN_KEY: creds.to_json()})
    except Exception as e:
        return False, f"Authorized, but couldn't save the token: {e}"
    return True, "Authorized ✓ — this Google account is now connected."


# ── build an API client from the stored token (auto-refreshes) ───────────────
def get_service(namespace: str, api_name: str, api_version: str, scopes: list[str]):
    """Return an authenticated googleapiclient service, refreshing the token if
    needed. Raises GoogleConfigError / GoogleAuthError / GoogleDependencyError so
    the plugin can turn each into friendly spoken guidance."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleDependencyError(
            "google-api-python-client / google-auth not installed "
            "(pip install google-api-python-client google-auth-oauthlib)"
        ) from e

    cfg = get_plugin_config(namespace)
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = (cfg.get("client_secret") or "").strip()
    token_json = cfg.get(_TOKEN_KEY)

    if not client_id or not client_secret:
        raise GoogleConfigError("no OAuth client configured")
    if not token_json:
        raise GoogleAuthError("not authorized yet")

    try:
        info = json.loads(token_json) if isinstance(token_json, str) else dict(token_json)
    except Exception as e:
        raise GoogleAuthError(f"stored token is unreadable: {e}") from e

    # make sure the token blob carries everything Credentials needs
    info.setdefault("client_id", client_id)
    info.setdefault("client_secret", client_secret)
    info.setdefault("token_uri", "https://oauth2.googleapis.com/token")

    try:
        creds = Credentials.from_authorized_user_info(info, scopes)
    except Exception as e:
        raise GoogleAuthError(f"could not load credentials: {e}") from e

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_plugin_config(namespace, {_TOKEN_KEY: creds.to_json()})
            except Exception as e:
                raise GoogleAuthError(
                    f"session expired and couldn't be refreshed ({e}); "
                    "please AUTHORIZE again"
                ) from e
        else:
            raise GoogleAuthError("not authorized (no valid token); please AUTHORIZE")

    try:
        return build(api_name, api_version, credentials=creds, cache_discovery=False)
    except Exception as e:
        raise GoogleError(f"couldn't start the Google {api_name} client: {e}") from e


def is_configured(namespace: str) -> bool:
    cfg = get_plugin_config(namespace)
    return bool((cfg.get("client_id") or "").strip() and cfg.get(_TOKEN_KEY))
