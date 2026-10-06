"""
Play a video where the avatar normally is.

The HUD already had a surface for this — the live camera takes the centre of
the screen and gives it back — so a video is the same idea with a different
source, and it shares the same stack rather than inventing a second one. That
means the avatar, the camera and a video can never be on screen together.

WHAT IT ACCEPTS
    a local file        C:\\Users\\me\\clip.mp4, ~/Videos/holiday.mkv
    a direct media URL  https://example.com/trailer.mp4
    a YouTube link      https://youtube.com/watch?v=...
    a YouTube search    "play the new Dune trailer"

    The first two are handed straight to Qt. The last two have to be resolved
    to something playable first, which is what yt-dlp does; without it, this
    says so and offers to open the video in the browser instead of failing.

SOUND IS OFF UNTIL ASKED FOR
    A soundtrack talking over the assistant is the one way this feature could
    make JARVIS worse rather than better, and the microphone is open while it
    plays. So it starts muted, and the user turns sound on — from the button in
    the video header, or by saying so. Closing it works both ways too.
"""
from __future__ import annotations

import re
import threading
import webbrowser
from pathlib import Path

_YT = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# Which open request is the current one. Opening happens on a thread now, so
# "stop" can arrive while a video is still being resolved — and without this the
# stop would find nothing playing, say so, and then the video would appear
# anyway a second later. Bumping the number abandons whatever is in flight.
_open_lock = threading.Lock()
_open_seq = 0


def _begin_open() -> int:
    global _open_seq
    with _open_lock:
        _open_seq += 1
        return _open_seq


def _still_wanted(token: int) -> bool:
    with _open_lock:
        return token == _open_seq


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://", "file://"))


_TS_UNITS = {"h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
             "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
             "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


def _parse_timestamp(value) -> float | None:
    """'1:23', '01:02:03', '90', '90s', '2m30s', '2 minutes 30 seconds', '1h2m' -> seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", ".")
    if not text:
        return None
    if text.startswith("-"):
        inner = _parse_timestamp(text[1:])
        return -inner if inner is not None else None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\d{1,3}(?::\d{1,2}){1,2}(?:\.\d+)?", text):
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text):
        mult = _TS_UNITS.get(unit)
        if mult:
            total += float(num) * mult
            found = True
    return total if found else None


def _fmt_time(sec: float) -> str:
    sec = max(0, int(round(sec or 0)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _status(player) -> dict:
    fn = getattr(player, "video_status", None)
    if callable(fn):
        try:
            return dict(fn() or {})
        except Exception:
            pass
    return {"state": "playing" if player.video_is_playing() else "stopped"}


def _wait_for(player, test, timeout: float = 1.5) -> dict:
    import time as _t
    end = _t.monotonic() + timeout
    st = _status(player)
    while _t.monotonic() < end:
        if test(st):
            return st
        _t.sleep(0.1)
        st = _status(player)
    return st


def _system_media(action: str) -> str:
    """Fallback when nothing plays on the HUD: the OS media key reaches the
    browser or whatever player is active. Honest about being unverified."""
    try:
        from actions.smart_media_control import run as media_run
        return media_run({"action": action})
    except Exception as e:
        return f"status=failed action=video_player reason=no video on the HUD and the system media key failed ({e})"


def _transport(player, action: str, params: dict) -> str:
    on_hud = player.video_is_playing()
    if action in ("pause", "resume", "toggle", "play_pause"):
        if not on_hud:
            key = {"pause": "pause", "resume": "play"}.get(action, "play_pause")
            return _system_media(key) + " (no video is open on the HUD; sent the system media key)"
        st = _status(player)
        want = {"pause": "paused", "resume": "playing"}.get(action) or \
            ("paused" if st.get("state") == "playing" else "playing")
        player.video_control("pause" if want == "paused" else "play")
        st = _wait_for(player, lambda s: s.get("state") == want)
        pos = _fmt_time(st.get("position", 0))
        if st.get("state") == want:
            return f"status=done action=video_player state={want} position={pos}"
        return (f"status=unverified action=video_player requested={want} state={st.get('state')} "
                "-- NOT confirmed. Say you asked but cannot confirm.")

    if not on_hud:
        return "status=failed action=video_player reason=no video is open on the HUD"
    st = _status(player)
    duration = float(st.get("duration") or 0)
    position = float(st.get("position") or 0)

    if action == "status":
        return (f"status=done action=video_player state={st.get('state')} "
                f"position={_fmt_time(position)} duration={_fmt_time(duration) if duration else 'unknown'} "
                f"title={st.get('title') or ''} muted={st.get('muted')}")

    if not st.get("seekable", True):
        return "status=failed action=video_player reason=this stream cannot be seeked"

    if action == "restart":
        target = 0.0
    elif action == "seek":
        target = _parse_timestamp(params.get("timestamp") if params.get("timestamp") not in (None, "")
                                  else params.get("seconds"))
        if target is None:
            return "status=failed action=video_player reason=give a timestamp like 1:23 or 2m30s"
        if target < 0 and duration:
            target = duration + target      # "-30" = thirty seconds from the end
    else:  # forward / back
        step = _parse_timestamp(params.get("seconds") if params.get("seconds") not in (None, "")
                                else params.get("timestamp"))
        step = abs(step) if step else 10.0
        target = position + (step if action == "forward" else -step)
    if duration:
        target = min(max(0.0, target), max(0.0, duration - 0.5))
    target = max(0.0, target)
    player.video_control("seek", target)
    if action == "restart":
        player.video_control("play")
    st = _wait_for(player, lambda s: abs(float(s.get("position") or 0) - target) <= 3.0, 2.0)
    now = float(st.get("position") or 0)
    if abs(now - target) <= 3.0:
        return (f"status=done action=video_player position={_fmt_time(now)}"
                + (f" duration={_fmt_time(duration)}" if duration else ""))
    return (f"status=unverified action=video_player requested={_fmt_time(target)} "
            f"position={_fmt_time(now)} -- NOT confirmed; the stream may still be buffering.")


def _local_path(text: str) -> str:
    """The text as an existing file on disk, or ''."""
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    try:
        p = Path(raw).expanduser()
        if p.exists() and p.is_file():
            return str(p)
    except Exception:
        pass
    return ""


def _resolve_youtube(url_or_query: str) -> tuple[str, str, str, str]:
    """(video_url, audio_url, title, error). Needs yt-dlp; says so when absent.

    TWO urls, because YouTube no longer offers one.
    The first version of this asked for a format carrying both picture and
    sound and got "Requested format is not available". That was not a bad
    selector, it was a wrong assumption: checked against three videos —
    including the oldest upload on the site — every one offered ZERO combined
    formats. Picture and sound are separate streams now, and both are returned
    so the player can run them together.

    `audio_url` comes back empty when only a combined or video-only stream
    exists, which is the normal case for everything that is not YouTube.
    """
    try:
        import yt_dlp
    except Exception:
        return "", "", "", ("yt-dlp is not installed, so a YouTube video "
                            "cannot be played inside the HUD. "
                            "Run: pip install yt-dlp")

    query = url_or_query if _YT.search(url_or_query) else f"ytsearch1:{url_or_query}"
    # No "format" here on purpose: ask for everything and choose below, so a
    # video that lacks whatever was requested cannot fail outright.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if info and info.get("entries"):
            info = info["entries"][0]
        if not info:
            return "", "", "", "nothing was found for that."
    except Exception as e:
        return "", "", "", f"the video could not be resolved: {e}"

    formats = [f for f in (info.get("formats") or [])
               if str(f.get("protocol") or "").startswith("http") and f.get("url")]

    def has(f, key):
        return f.get(key) not in (None, "none")

    def best(candidates):
        """Highest quality — but the ORIGINAL language before quality.

        YouTube auto-dubs a lot of videos now, and it offers every dub as its
        own audio stream at the SAME bitrate as the original: measured on one
        video, English original at 129.483 and Arabic, Bangla, German, Spanish,
        French, Hindi and Indonesian all at 129.482. Choosing on bitrate alone
        therefore came down to a thousandth of a kilobit, and a video would play
        in Arabic one time and English the next for no reason the user could
        see.

        YouTube marks the original track with language_preference 10 and every
        dub with -1, so that decides it first and bitrate only breaks ties
        within the same track. Video formats carry no language, so this is the
        same ordering for them with the first term always equal.
        """
        if not candidates:
            return None
        return max(candidates,
                   key=lambda f: (f.get("language_preference") or 0,
                                  f.get("tbr") or 0))

    # Combined first — if a video still has one, one player is simpler and
    # cannot drift. Then picture and sound separately.
    combined = best([f for f in formats if has(f, "vcodec") and has(f, "acodec")])
    if combined:
        return combined["url"], "", info.get("title") or "", ""

    # 720p is the ceiling on purpose: the HUD panel is nowhere near 4K, and a
    # smaller stream starts sooner and drifts less.
    video = best([f for f in formats
                  if has(f, "vcodec") and not has(f, "acodec")
                  and f.get("ext") == "mp4" and (f.get("height") or 0) <= 720])
    if not video:
        video = best([f for f in formats
                      if has(f, "vcodec") and not has(f, "acodec")])
    audio = best([f for f in formats
                  if has(f, "acodec") and not has(f, "vcodec")
                  and f.get("ext") == "m4a"])
    if not audio:
        audio = best([f for f in formats
                      if has(f, "acodec") and not has(f, "vcodec")])

    if not video:
        return "", "", "", "no playable stream was offered for that video."
    if audio and audio.get("language"):
        print(f"[Video] audio track: {audio.get('language')} "
              f"({audio.get('format_note') or audio.get('format_id')})")
    return video["url"], (audio or {}).get("url", ""), info.get("title") or "", ""


def _search_page(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def _show_and_confirm(player, source: str, title: str, audio_source: str = "") -> tuple[bool, str]:
    """Queue playback on Qt and wait for a real PlayingState confirmation."""
    player.show_video(source, title, muted=True, audio_source=audio_source)
    waiter = getattr(player, "wait_for_video_start", None)
    if not callable(waiter):
        return True, ""
    ok, detail = waiter(25.0)
    if not ok:
        try:
            player.stop_video()
        except Exception:
            pass
    return bool(ok), str(detail or "")


def video_player(parameters: dict = None, response=None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").strip().lower()
    source = (params.get("source") or "").strip()

    if player is None:
        return "There is no display to play a video on."

    # ---- stop -------------------------------------------------------------
    if action in ("stop", "close", "hide"):
        # Cancel an open that has not finished yet, so a video the user has
        # already changed their mind about never reaches the screen.
        was_opening = not player.video_is_playing()
        _begin_open()
        player.stop_video()
        if was_opening:
            return "Stopped it before it opened."
        return "Closed the video."

    # ---- sound ------------------------------------------------------------
    if action in ("mute", "unmute", "sound_on", "sound_off"):
        if not player.video_is_playing():
            return "Nothing is playing."
        muted = action in ("mute", "sound_off")
        player.set_video_muted(muted)
        return "Muted the video." if muted else "Turned the video's sound on."

    # ---- transport ----------------------------------------------------------
    aliases = {"play_pause": "toggle", "unpause": "resume", "continue": "resume",
               "skip_forward": "forward", "fast_forward": "forward", "ff": "forward",
               "skip_back": "back", "backward": "back", "rewind": "back",
               "jump": "seek", "goto": "seek", "go_to": "seek", "position": "status",
               "from_start": "restart", "replay": "restart"}
    action = aliases.get(action, action)
    if action in ("pause", "resume", "toggle", "seek", "forward", "back", "restart", "status"):
        return _transport(player, action, params)

    # ---- play -------------------------------------------------------------
    if not source:
        # "play" with nothing named resumes a paused HUD video.
        if player.video_is_playing() and _status(player).get("state") == "paused":
            return _transport(player, "resume", params)
        return "What should I play?"

    local = _local_path(source)
    if local:
        ok, err = _show_and_confirm(player, local, Path(local).name)
        return (f"status=playing source={local}" if ok
                else f"status=failed reason={err}")

    # A direct media URL can go straight to the player; a YouTube page cannot.
    if _is_url(source) and not _YT.search(source):
        ok, err = _show_and_confirm(player, source, source.rsplit("/", 1)[-1][:44])
        return (f"status=playing source={source}" if ok
                else f"status=failed reason={err}")

    # YouTube/search resolution can take seconds. The tool is declared
    # NON_BLOCKING so Live may keep the conversation responsive, while this
    # handler retains ownership of the work and returns a terminal result.
    # The runtime owns non-blocking tool execution. Do the work in this handler
    # and return only a terminal status so the tool call cannot remain "opening".
    return _play_youtube(player, source, _begin_open())


def _play_youtube(player, source: str, token: int) -> str:
    """Resolve YouTube/search input, start playback, and return a terminal status."""
    try:
        url, audio_url, title, err = _resolve_youtube(source)
    except Exception as e:                                  # noqa: BLE001
        url = audio_url = title = ""
        err = str(e)

    if not _still_wanted(token):
        return "status=cancelled"

    if url:
        try:
            ok, detail = _show_and_confirm(player, url, title or source, audio_url)
            if ok:
                return f"status=playing title={title or source}"
            err = detail or "Qt did not start playback"
        except Exception as e:                              # noqa: BLE001
            err = str(e)

    page = source if _YT.search(source) else _search_page(source)
    opened = False
    try:
        opened = bool(webbrowser.open(page))
    except Exception:
        opened = False
    fallback = " browser_fallback=opened" if opened else ""
    _say(player, "Tell the user, in one short sentence in their own language: "
                 f"'{source}' could not be played on the display ({err}).")
    return f"status=failed reason={err}{fallback}"


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "video_player",
    "description": (
        "If user wants to open a video on YouTube, he will tell you that specifically"
        "If user wants to play a video, then he wants to call this feature"
        "Plays a VIDEO on the assistant's own display, where the avatar "
        "normally is — and controls it: stop, mute/unmute, pause, resume, seek to a "
        "timestamp ('jump to 1:23', 'go to 2 minutes 30'), skip forward/back ('skip 30 "
        "seconds', 'go back 10 seconds'), restart, and status (current position). "
        "Pause/resume also work on a video in the browser through the system media key "
        "when nothing is open on the HUD. Use whenever the "
        "user wants to WATCH something on screen: 'play the new Dune trailer', "
        "'play that video', 'play C:/clips/holiday.mp4', 'put that YouTube "
        "video on the screen', 'stop the video', 'videoyu kapat', 'turn the "
        "sound on', 'sesi aç', 'mute it'. Accepts a local file path, a direct "
        "video URL, a YouTube link, or a description to search YouTube for. "
        "Video always starts MUTED — only call action='unmute' when the user "
        "actually asks to hear it. "
        "This tool is NON_BLOCKING. Call it once; it returns status=playing only "
        "after Qt actually enters PlayingState, or status=failed with the reason. "
        "Do NOT use 'youtube_video' for this: that optional plugin opens videos "
        "in the web browser and summarises transcripts, it never plays anything "
        "on the assistant's display."
    ),
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Plays a video on the HUD - a YouTube link, a local file, a URL, or a description to search for."
    ),
    "behavior": "NON_BLOCKING",
    "scheduling": "SILENT",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("'play' (default), 'stop' to close it, 'unmute' "
                                "to turn the sound on, 'mute' to silence it, 'pause', "
                                "'resume', 'toggle', 'seek' (to 'timestamp'), 'forward' / "
                                "'back' (by 'seconds', default 10), 'restart', 'status'."),
                "enum": ["play", "stop", "mute", "unmute", "pause", "resume", "toggle",
                         "seek", "forward", "back", "restart", "status"],
            },
            "timestamp": {
                "type": "STRING",
                "description": "For seek: where to jump, e.g. '1:23', '01:02:03', '2m30s', '90'.",
            },
            "seconds": {
                "type": "NUMBER",
                "description": "For forward/back: how far to skip (default 10).",
            },
            "source": {
                "type": "STRING",
                "description": (
                    "For action='play': a local file path, a direct video URL, "
                    "a YouTube link, or what to search YouTube for. Pass what "
                    "the user actually named, in their own words."
                ),
            },
        },
        "required": ["action"],
    },
    "handler": video_player,
}
