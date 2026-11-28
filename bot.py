"""Clean voice-chat music bot. Best-quality audio, official HD artwork card, no effects."""
import asyncio

# One event loop for everything: pyrogram binds to the loop that exists when it is created.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import html
import logging
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import config

config.load_env()
_missing = config.missing()
if _missing:
    print("MISSING SETTINGS: " + ", ".join(_missing) +
          "  -> add them in Railway > Variables (or in the .env file).", flush=True)
    sys.exit(2)

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.raw import functions
from pyrogram.raw.types import InputStickerSetShortName
from pyrogram.types import (
    CallbackQuery,
    Message,
)
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import AudioQuality, Call, MediaStream, StreamEnded

# FFmpeg DEBUG output floods the logs (HLS reconnect/playlist lines many times a second).
logging.getLogger('ffmpeg').setLevel(logging.WARNING)

import sources
from seek import parse_seek_position

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION = os.environ["SESSION_STRING"]  # assistant account that joins the voice chat

# 48 kHz stereo is what Telegram voice chats use. "medium" (36 kHz mono) is an
# escape hatch for very small containers where encoding still crackles.
AUDIO_QUALITY = {
    "high": AudioQuality.HIGH,
    "medium": AudioQuality.MEDIUM,
    "low": AudioQuality.LOW,
}.get(os.environ.get("AUDIO_QUALITY", "high").strip().lower(), AudioQuality.HIGH)

# Every fresh bot login (auth.ImportBotAuthorization) counts toward Telegram's flood limit, and each
# Railway deploy used to start with a brand-new in-memory session. Keep the bot session on disk
# when a persistent directory exists (mount a Railway volume at /data, or set BOT_SESSION_DIR) so
# redeploys and restarts reuse it instead of logging in again.
_sess_dir = (
    os.environ.get("BOT_SESSION_DIR")
    or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
)
if _sess_dir and not os.access(_sess_dir, os.W_OK):
    _sess_dir = ""
if _sess_dir:
    bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=_sess_dir, sleep_threshold=60)
else:
    print("No persistent session dir (/data or BOT_SESSION_DIR): bot logs in fresh on every start.", flush=True)
    bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True, sleep_threshold=60)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION,
                   in_memory=True, sleep_threshold=60)
calls = PyTgCalls(assistant)

@dataclass
class Track:
    title: str
    url: str       # direct audio stream
    page: str      # youtube link
    duration: int
    by: str
    artist: str = ""
    headers: dict = field(default_factory=dict)
    ffmpeg_parameters: str | None = None
    started: float = 0.0
    paused_at: float = 0.0
    msg: int = 0
    chat_id: int = 0
    task: object = None
    source: str = ""        # which source produced the stream (youtube, soundcloud, ...)
    query: str = ""         # what the user asked for, used to find the song again elsewhere
    resolved_at: float = 0.0
    retries: int = 0
    bad: set = field(default_factory=set)  # sources that already failed for this track
    thumbs: list = field(default_factory=list)  # official HD artwork URLs, best first
    official: bool = False
    has_photo: bool = False
    end_state: str = ""  # "", "ended", "completed", "stopped"
    final_elapsed: int = 0
    end_seen: bool = False
    ignore_end_until: float = 0.0
    start_token: int = 0
    last_start_at: float = 0.0
    last_start_url: str = ""
    proxy: str = ""


queues: dict[int, list[Track]] = {}
history: dict[int, list[Track]] = {}
loop: dict[int, bool] = {}
advance_locks: dict[int, asyncio.Lock] = {}
background_tasks: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def fmt(sec: int) -> str:
    m, s = divmod(int(sec or 0), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


STREAM_MAX_AGE = 3000  # seconds; YouTube stream links expire after a few hours


async def reresolve(t: Track):
    """Find the song again (skipping sources that already failed for it) and update the track."""
    info = await asyncio.to_thread(sources.resolve, t.query or t.title, t.bad)
    t.url, t.headers, t.source = info["url"], info.get("headers") or {}, info["source"]
    t.proxy = info.get("proxy") or ""
    t.page = info.get("page") or t.page
    t.duration = info.get("duration") or t.duration
    t.thumbs = info.get("thumbnails") or t.thumbs
    t.official = bool(info.get("official", t.official))
    t.resolved_at = time.time()


async def ensure_fresh(t: Track):
    if t.resolved_at and time.time() - t.resolved_at > STREAM_MAX_AGE:
        print("stream link is old, refreshing:", t.title, flush=True)
        await reresolve(t)


# ── premium emojis (resolved from the packs below; falls back to normal emoji) ──
PACKS = [p.strip() for p in os.environ.get(
    "EMOJI_PACKS", "sexypiyush69_79e604_by_SexyPiyush_BOT,NewsEmoji,RestrictedEmoji").split(",") if p.strip()]

# role -> (default emoji, alternatives to look for in the packs)
ROLES = {
    "song": ("🎵", ["🎵", "🎶", "🎧"]),
    "now": ("🎧", ["🎧", "🎵", "🎶"]),
    "pause": ("⏸️", ["⏸️", "⏸", "⏯️"]),
    "play": ("▶️", ["▶️", "▶", "🎶"]),
    "user": ("👤", ["👤", "🙋", "👑"]),
    "prev": ("⏮", ["⏮", "◀️", "⬅️"]),
    "skip": ("⏭", ["⏭", "⏩", "➡️"]),
    "shuffle": ("🔀", ["🔀", "🔄"]),
    "loop": ("🔁", ["🔁", "🔂", "♻️"]),
    "stop": ("⏹️", ["⏹️", "⏹", "🛑", "❌"]),
    "queue": ("📥", ["📥", "📋", "📝"]),
}
EMO: dict[str, tuple[str, str]] = {}  # role -> (char, custom_emoji_id or "")


async def load_emojis():
    found: dict[str, str] = {}
    for name in PACKS:
        for client in (bot, assistant):
            try:
                res = await client.invoke(functions.messages.GetStickerSet(
                    stickerset=InputStickerSetShortName(short_name=name), hash=0))
            except Exception:
                continue
            docs = {d.id for d in res.documents}
            for p in res.packs:
                ch = (p.emoticon or "")
                for d in p.documents:
                    if d in docs:
                        found.setdefault(ch, str(d))
                        found.setdefault(ch.replace("\ufe0f", ""), str(d))
                        break
            break
    for role, (default, alts) in ROLES.items():
        for ch in alts:
            cid = found.get(ch) or found.get(ch.replace("\ufe0f", ""))
            if cid:
                EMO[role] = (ch, cid)
                break
        else:
            EMO[role] = (default, "")
    print("Premium emojis:", {r: bool(v[1]) for r, v in EMO.items()})


def pe(role: str, plain: bool = False) -> str:
    ch, cid = EMO.get(role, (ROLES[role][0], ""))
    return ch if plain or not cid else f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>'


def elapsed(t: Track) -> int:
    if not t.started:
        return 0
    now = t.paused_at or time.time()
    return max(0, min(int(now - t.started), t.duration or 10**9))


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:max(0, limit - 1)].rstrip() + "…"


_TITLE_BRACKETS = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*")
_TITLE_TAIL = re.compile(
    r"\s+(?:official(?:\s+(?:music\s+)?(?:video|audio))?|"
    r"full\s+(?:video|song)|video\s+song|lyrics?|lyrical|audio|"
    r"8k|4k|hd|hq)\s*$", re.I)


def _display_title(title: str) -> str:
    raw = (title or "").strip()
    text = raw.split("|", 1)[0].strip()
    text = _TITLE_BRACKETS.sub(" ", text)
    for _ in range(2):
        text = _TITLE_TAIL.sub("", text).strip()
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—:•")
    return text or raw or "Unknown"


def card(t: Track, plain: bool = False) -> str:
    d = t.duration
    e = t.final_elapsed if t.end_state else elapsed(t)
    filled = round(10 * e / d) if d else 0
    bar = "▰" * filled + "▱" * (10 - filled)
    title = html.escape(_clip(_display_title(t.title).upper(), 34))
    by = html.escape(_clip(t.by, 20))
    if t.end_state == "completed":
        status = f"{pe('stop', plain)} COMPLETED • {fmt(d)} / {fmt(d)}"
    elif t.end_state == "ended":
        status = f"{pe('stop', plain)} PLAYBACK ENDED • {fmt(e)} / {fmt(d)}"
    elif t.end_state == "stopped":
        status = f"{pe('stop', plain)} STOPPED • {fmt(e)} / {fmt(d)}"
    elif t.paused_at:
        status = f"{pe('pause', plain)} PAUSED • {fmt(e)} / {fmt(d)}"
    else:
        status = f"{pe('play', plain)}  {bar}  {fmt(e)} / {fmt(d)}"
    return f"{pe('song', plain)} {title}\n\n{status}\n\n{pe('user', plain)} {by}"


def _call(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        # A Telegram timeout must not abort playback or skip the current track.
        return {"ok": False, "description": f"{type(e).__name__}: {e}"[:200]}


async def api(method: str, payload: dict, fallback_markup: dict | None = None) -> dict:
    """Bot API call; if colours/premium icons are rejected, retries with plain buttons."""
    res = await asyncio.to_thread(_call, method, payload)
    if not res.get("ok") and fallback_markup is not None \
            and "not modified" not in str(res.get("description", "")):
        res = await asyncio.to_thread(_call, method, {**payload, "reply_markup": fallback_markup})
    return res


def kb(chat_id: int, t: Track, plain: bool = False) -> dict:
    """Unified colours: green = play/pause, blue = navigation, red = stop.
    Premium emoji icons are used when available; plain=True is the safe fallback."""
    def B(role, label, data, style):
        ch, cid = EMO.get(role, (ROLES[role][0], ""))
        if plain or not cid:
            return {"text": f"{ch} {label}" if plain or not cid else label,
                    "callback_data": f"ctl:{data}", **({} if plain else {"style": style})}
        return {"text": label, "callback_data": f"ctl:{data}", "style": style, "icon_custom_emoji_id": cid}

    pause = B("play", "Resume", "resume", "success") if t.paused_at else B("pause", "Pause", "pause", "success")
    on = loop.get(chat_id)
    # Two buttons per row: labels never overflow the photo bubble on a narrow phone.
    return {"inline_keyboard": [
        [B("prev", "Prev", "prev", "success"), pause],
        [B("skip", "Skip", "skip", "primary"), B("shuffle", "Shuffle", "shuffle", "primary")],
        [B("loop", "Loop ✓" if on else "Loop", "loop", "danger"),
         B("stop", "Stop", "stop", "danger")],
    ]}


async def refresh(chat_id: int, t: Track):
    final = bool(t.end_state)
    markup = {"inline_keyboard": []} if final else kb(chat_id, t)
    plain_markup = {"inline_keyboard": []} if final else kb(chat_id, t, plain=True)
    if t.has_photo:
        res = await api("editMessageCaption", {
            "chat_id": chat_id, "message_id": t.msg,
            "caption": card(t), "parse_mode": "HTML",
            "reply_markup": markup,
        }, plain_markup)
        if not res.get("ok") and "not modified" not in str(res.get("description", "")):
            await api("editMessageCaption", {
                "chat_id": chat_id, "message_id": t.msg,
                "caption": card(t, plain=True), "parse_mode": "HTML",
                "reply_markup": plain_markup,
            })
        return
    res = await api("editMessageText", {
        "chat_id": chat_id, "message_id": t.msg, "text": card(t),
        "parse_mode": "HTML", "reply_markup": markup,
        "link_preview_options": {"is_disabled": True},
    }, plain_markup)
    if not res.get("ok") and "not modified" not in str(res.get("description", "")):
        await api("editMessageText", {
            "chat_id": chat_id, "message_id": t.msg, "text": card(t, plain=True),
            "parse_mode": "HTML", "reply_markup": plain_markup,
            "link_preview_options": {"is_disabled": True},
        })


async def updater(t: Track):
    last = ""
    try:
        while True:
            await asyncio.sleep(10)
            try:
                txt = card(t)
                if txt != last:
                    await refresh(t.chat_id, t)
                    last = txt
            except Exception as e:  # a network hiccup must not kill the progress bar
                print("card update failed:", repr(e)[:120], flush=True)
    except asyncio.CancelledError:
        pass


async def start_stream(chat_id: int, t: Track, seek_to: int | None = None):
    now = time.time()
    if (seek_to is None and t.started and t.last_start_at
            and t.url == t.last_start_url
            and now - t.last_start_at < 8.0):
        # A watchdog/source retry must not rewind a stream that already started.
        print("ignoring duplicate start for:", t.title, flush=True)
        return
    t.last_start_at = now
    t.last_start_url = t.url
    # 48 kHz stereo is exactly what Telegram voice chats use. AudioQuality.STUDIO
    # is 96 kHz and forces an extra resample; on a small Railway container that
    # causes underruns (crackling) without any audible benefit.
    start = ["--audio", "---start"]
    proxy = t.proxy
    if t.source == "youtube" and not proxy and sources.PROXY_POOL:
        # Old queued tracks have no stored proxy; use the next available one.
        proxy = sources.PROXY_POOL.candidates()[0] or ""
    if proxy:
        # The stream link is bound to the proxy's IP, so ffmpeg must fetch it through it too.
        start += ["-http_proxy", proxy]
    if seek_to is not None:
        start += ["-ss", str(seek_to)]
    if t.ffmpeg_parameters:
        start.append(t.ffmpeg_parameters)
    ffmpeg_parameters = " ".join(start)
    if os.environ.get("AUDIO_NORMALIZE", "1").strip().lower() in {"1", "true", "yes", "on"}:
        # Single-pass EBU R128 evens out loud/quiet uploads and prevents clipping.
        # ffprobe does not support -af; pytgcalls strips unsupported flags for its probe.
        ffmpeg_parameters += " ---end -af loudnorm=I=-16:TP=-1.5:LRA=11"
    kw = dict(
        audio_parameters=AUDIO_QUALITY,  # default 48 kHz stereo, matches Telegram
        video_flags=MediaStream.Flags.IGNORE,
        ffmpeg_parameters=ffmpeg_parameters,
    )
    # Preserve provider headers; silently dropping Referer/User-Agent can turn a valid
    # stream into an HTTP error or an apparently silent playback.
    stream = MediaStream(t.url, headers=t.headers, **kw)
    t.end_seen = False
    t.end_state = ""
    t.final_elapsed = 0
    # Suppress the previous stream's end event while this start/replacement is in flight.
    t.ignore_end_until = time.time() + 8.0
    t.start_token += 1
    token = t.start_token
    try:
        # A previous track may have been paused. Resume before replacing it, otherwise
        # some clients keep the new stream silent until the user presses Resume.
        await calls.resume(chat_id)
    except Exception:
        pass
    try:
        await calls.play(chat_id, stream)
    except Exception as e:
        print("calls.play failed:", repr(e), flush=True)
        if _is_call_setup_error(e):
            try:
                await ensure_assistant_in_group(chat_id)
            except Exception as join_error:
                print("assistant rejoin before retry failed:", repr(join_error)[:140], flush=True)
            try:
                # Clear a stale voice-chat state ("not a participant") before rejoining.
                await calls.leave_call(chat_id)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            t.ignore_end_until = time.time() + 8.0
            try:
                await calls.play(chat_id, stream)
            except Exception as retry_error:
                print("calls.play retry failed:", repr(retry_error), flush=True)
                raise
        else:
            raise
    t.started = time.time() - (seek_to or 0)
    t.paused_at = 0.0
    # Replacing a stream makes the old stream emit StreamEnded. Ignore that stale
    # event briefly, otherwise it advances the brand-new track and cuts the sound.
    t.ignore_end_until = time.time() + 8.0
    if os.environ.get("START_WATCHDOG", "0").strip().lower() in {"1", "true", "yes", "on"}:
        spawn(watch_start(chat_id, t, token))


def _is_call_setup_error(error: Exception) -> bool:
    """Do not switch audio providers for Telegram voice-chat permission/state errors."""
    name = type(error).__name__.lower()
    message = str(error).lower()
    known_types = (
        "groupcall", "chatadminrequired", "usernotparticipant",
        "peeridinvalid", "channelprivate", "userbannedinchannel",
    )
    known_messages = (
        "voice chat is not active", "group call is not active",
        "not a participant", "chat admin required", "peer id invalid",
    )
    return any(token in name for token in known_types) or any(
        token in message for token in known_messages
    )


async def _playback_state(chat_id: int):
    try:
        states = await asyncio.wait_for(calls.calls, timeout=3)
    except Exception:
        return None
    return states.get(chat_id) if hasattr(states, "get") else None


async def watch_start(chat_id: int, t: Track, token: int):
    """Catch a genuinely silent start without killing a stream that is still connecting."""
    await asyncio.sleep(8.0)
    lock = advance_locks.setdefault(chat_id, asyncio.Lock())
    retry = False
    async with lock:
        if t.start_token != token:
            return
        q = queues.get(chat_id)
        if not q or q[0] is not t or t.end_seen or t.end_state or t.paused_at:
            return
        state = await _playback_state(chat_id)
        if state is None:
            # PyTgCalls may not have registered the call yet. Treat this as unknown,
            # not as silence; retrying here used to replace a healthy starting stream.
            print("call state not registered yet; not retrying", flush=True)
            return
        if state.playback != Call.Status.IDLE:
            return
        # Confirm IDLE once more before replacing the stream. A single early read
        # can happen while ffmpeg is still opening the source.
        await asyncio.sleep(2.0)
        if t.start_token != token:
            return
        q = queues.get(chat_id)
        if not q or q[0] is not t or t.end_seen or t.end_state or t.paused_at:
            return
        state = await _playback_state(chat_id)
        if state is None or state.playback != Call.Status.IDLE:
            return
        print("no active playback after start; retrying:", t.title, flush=True)
        if t.retries >= 3:
            retry = True
        else:
            t.retries += 1
            t.end_seen = True
            if t.source:
                t.bad.add(t.source)
            try:
                await start_with_fallback(chat_id, t)
                return
            except Exception as e:
                print("retry after idle failed:", repr(e)[:160], flush=True)
                retry = True
    if retry:
        try:
            await play_next(chat_id, force=True, expected=t)
        except Exception as e:
            print("advance after idle failed:", repr(e)[:160], flush=True)


async def start_with_fallback(chat_id: int, t: Track):
    """Start this exact requested track; retry YouTube with another proxy, then other providers."""
    attempts = max(1, len(sources.SOURCES) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            # A caller may have marked the just-ended provider bad before entering here.
            if t.source and t.source in t.bad:
                await reresolve(t)
            else:
                await ensure_fresh(t)
            await start_stream(chat_id, t)
            return
        except Exception as e:
            if _is_call_setup_error(e):
                raise
            last_error = e
            retry_with_new_proxy = False
            if t.source == "youtube" and t.proxy:
                sources.PROXY_POOL.mark_bad(t.proxy, str(e))
                if not sources.PROXY_POOL.all_bad():
                    retry_with_new_proxy = True
                    t.bad.discard("youtube")
            if not retry_with_new_proxy and t.source:
                t.bad.add(t.source)
            if attempt + 1 >= attempts:
                break
            try:
                await reresolve(t)
            except Exception as resolve_error:
                last_error = resolve_error
                break
    raise RuntimeError(f"all available audio sources failed: {last_error}") from last_error


async def announce(chat_id: int, t: Track, message_id: int | None = None) -> int:
    if t.task:
        t.task.cancel()
    if message_id:
        # The old text status cannot become a photo; remove it and send a fresh card.
        await api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    t.chat_id = chat_id
    t.has_photo = False
    res: dict = {}
    # Official HD artwork first (maxres, maxres1-3, then sd/hq fallbacks). Telegram
    # fetches the URL itself, so no image bytes pass through this container.
    for plain in (False, True):
        for photo in (t.thumbs or [])[:8]:
            res = await api("sendPhoto", {
                "chat_id": chat_id, "photo": photo,
                "caption": card(t, plain=plain),
                "parse_mode": "HTML",
                "reply_markup": kb(chat_id, t, plain=plain),
            })
            if res.get("ok"):
                t.msg = res.get("result", {}).get("message_id", 0)
                t.has_photo = True
                break
        if t.has_photo:
            break
    if not t.has_photo:
        if t.thumbs:
            kind = "official" if t.official else "fallback"
            print(f"photo card failed ({kind}), using text card:",
                  (t.thumbs or [""])[0][:100], flush=True)
        payload = {
            "chat_id": chat_id, "text": card(t),
            "parse_mode": "HTML", "reply_markup": kb(chat_id, t),
            "link_preview_options": {"is_disabled": True},
        }
        res = await api("sendMessage", payload, kb(chat_id, t, plain=True))
        if not res.get("ok"):
            payload["text"] = card(t, plain=True)
            res = await api("sendMessage", payload, kb(chat_id, t, plain=True))
        t.msg = res.get("result", {}).get("message_id", 0)
    t.task = asyncio.create_task(updater(t))
    return t.msg


def finish(t: Track):
    if t.task:
        t.task.cancel()
        t.task = None


async def finalize_track(t: Track, state: str = ""):
    """Freeze the card (and remove its buttons) when a track ends, completes or stops."""
    token = t.start_token
    q = queues.get(t.chat_id)
    if q and q[0] is t and not t.end_state:
        return  # this track was replayed before the background cleanup ran
    if t.start_token != token:
        return
    t.end_seen = True
    if state and not t.end_state:
        t.end_state = state
    if not t.end_state:
        t.end_state = "ended"
    if not t.final_elapsed:
        t.final_elapsed = t.duration if t.end_state == "completed" else elapsed(t)
    if t.start_token != token:
        return
    finish(t)
    if not t.msg:
        return
    try:
        await refresh(t.chat_id, t)
    except Exception as e:
        print("final card update failed:", repr(e)[:140], flush=True)


async def notify(chat_id: int, text: str):
    try:
        await api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception:
        pass


async def leave(chat_id: int):
    queues.pop(chat_id, None)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass


async def play_next(chat_id: int, force: bool = False, expected: Track | None = None):
    """Advance the queue. A track that can't be played is skipped - the queue never gets stuck."""
    lock = advance_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        q = queues.get(chat_id, [])
        if expected is not None and (not q or q[0] is not expected):
            return
        if q and loop.get(chat_id) and not force:
            t = q[0]
            t.end_state = ""
            t.final_elapsed = 0
            t.end_seen = False
            t.ignore_end_until = 0.0
            try:
                await asyncio.sleep(0.15)
                await start_with_fallback(chat_id, t)  # replay same track, same card
            except Exception as e:
                print("loop replay failed:", repr(e)[:200], flush=True)
            else:
                try:
                    await refresh(chat_id, t)
                except Exception as e:
                    print("loop card refresh failed:", repr(e)[:140], flush=True)
                return
        while True:
            done = None
            if q:
                done = q.pop(0)
                done.end_seen = True
                if q:
                    # Protect the next track from the old stream's end event while it starts.
                    q[0].end_seen = False
                    q[0].ignore_end_until = time.time() + 8.0
                if done.started:
                    history.setdefault(chat_id, []).append(done)
                    del history[chat_id][:-20]
            if not q:
                await leave(chat_id)
                if done:
                    spawn(finalize_track(done))
                return
            try:
                await asyncio.sleep(0.15)
                await start_with_fallback(chat_id, q[0])
            except Exception as e:
                if done:
                    await finalize_track(done)
                print("could not play, skipping:", q[0].title, repr(e)[:200], flush=True)
                await notify(chat_id, f"Couldn't play \"{q[0].title}\", skipping it.")
                continue
            if done:
                # Card cleanup is cosmetic; do it in the background so it cannot delay
                # the next track's audio.
                spawn(finalize_track(done))
            try:
                await announce(chat_id, q[0])
            except Exception as e:
                # The card is cosmetic; never skip a track that is already playing.
                print("card send failed, playback continues:", repr(e)[:160], flush=True)
            return


async def handle_end(chat_id: int):
    q = queues.get(chat_id)
    if not q:
        return
    t = q[0]
    if t.ignore_end_until and time.time() < t.ignore_end_until:
        print("ignoring stale stream-end event", flush=True)
        return
    if t.end_seen:
        return
    t.end_seen = True
    now = elapsed(t)
    if t.duration and now >= t.duration - 2:
        t.end_state = "completed"
        t.final_elapsed = t.duration
    else:
        t.end_state = "ended"
        t.final_elapsed = now
    # Stream "ended" almost instantly although the song is long = the link was dead. Try another source.
    quick = bool(t.started) and not t.paused_at and (time.time() - t.started) < 4 and t.duration > 20
    if quick and t.retries < 3:
        t.retries += 1
        if t.source:
            t.bad.add(t.source)
        print(f"stream died right away ({t.source}), trying another source (try {t.retries})", flush=True)
        t.end_state = ""
        t.final_elapsed = 0
        try:
            await asyncio.sleep(0.2)
            q = queues.get(chat_id)
            if not q or q[0] is not t or t.end_state:
                return
            await start_with_fallback(chat_id, t)
            return
        except Exception as e:
            print("no other source worked:", repr(e)[:200], flush=True)
    await play_next(chat_id, force=quick, expected=t)


async def act(chat_id: int, action: str) -> str:
    q = queues.get(chat_id)
    if not q:
        return "Nothing is playing."
    cur = q[0]
    if action == "pause":
        if cur.paused_at:
            return "Already paused."
        await calls.pause(chat_id)
        cur.paused_at = time.time()
        await refresh(chat_id, cur)
        return "⏸ Paused"
    if action == "resume":
        if not cur.paused_at:
            return "Already playing."
        await calls.resume(chat_id)
        cur.started += time.time() - cur.paused_at
        cur.paused_at = 0.0
        await refresh(chat_id, cur)
        return "▶️ Resumed"
    if action == "skip":
        cur.end_seen = True
        cur.end_state = "ended"
        cur.final_elapsed = elapsed(cur)
        await play_next(chat_id, force=True)
        return "⏭ Skipped"
    if action == "prev":
        lock = advance_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            h = history.get(chat_id)
            if not h:
                return "No previous song."
            prev = h.pop()
            cur.end_seen = True
            cur.end_state = "ended"
            cur.final_elapsed = elapsed(cur)
            spawn(finalize_track(cur))
            q[0] = prev
            prev.end_state = ""
            prev.final_elapsed = 0
            prev.end_seen = False
            prev.ignore_end_until = time.time() + 8.0
            prev.retries = 0
            prev.bad.clear()
            await asyncio.sleep(0.15)
            await start_with_fallback(chat_id, prev)
            try:
                await announce(chat_id, prev)
            except Exception as e:
                print("previous card send failed, playback continues:", repr(e)[:160], flush=True)
        return "⏮ Previous"
    if action == "shuffle":
        if len(q) < 3:
            return "Not enough songs in the queue."
        rest = q[1:]
        random.shuffle(rest)
        q[1:] = rest
        return "🔀 Queue shuffled"
    if action == "loop":
        loop[chat_id] = not loop.get(chat_id)
        await refresh(chat_id, cur)
        return "🔁 Loop on" if loop[chat_id] else "🔁 Loop off"
    if action == "stop":
        lock = advance_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            for x in queues.pop(chat_id, []):
                finish(x)
            loop.pop(chat_id, None)
            try:
                await calls.leave_call(chat_id)
            except Exception:
                pass
            cur.end_state = "stopped"
            cur.final_elapsed = elapsed(cur)
            cur.end_seen = True
            await finalize_track(cur)
        return "⏹ Stopped"
    return ""


@bot.on_message(filters.command("mxseek") & filters.group)
async def seek_track(_, m: Message):
    if len(m.command) != 2:
        return await m.reply_text("Usage: /mxseek <seconds|MM:SS|HH:MM:SS>")
    try:
        target = parse_seek_position(m.command[1])
    except ValueError:
        return await m.reply_text("Usage: /mxseek <seconds|MM:SS|HH:MM:SS>")

    q = queues.get(m.chat.id)
    if not q:
        return await m.reply_text("Nothing is playing.")
    cur = q[0]

    try:
        await ensure_fresh(cur)
    except Exception as e:
        print("/mxseek could not refresh the stream:", repr(e)[:200], flush=True)
        return await m.reply_text("Couldn't refresh the current stream to seek.")

    if cur.duration and target >= cur.duration:
        return await m.reply_text(f"Choose a position before {fmt(cur.duration)}.")

    was_paused = bool(cur.paused_at)
    try:
        await start_stream(m.chat.id, cur, seek_to=target)
        if was_paused:
            await calls.pause(m.chat.id)
            cur.paused_at = time.time()
        await refresh(m.chat.id, cur)
    except Exception as e:
        print("/mxseek failed:", repr(e)[:200], flush=True)
        return await m.reply_text("Couldn't seek this stream. It may not support seeking.")

    await m.reply_text(f"⏩ Seeked to {fmt(target)}.")


@calls.on_update(call_filters.stream_end(StreamEnded.Type.AUDIO))
async def on_end(_, update: StreamEnded):
    try:
        print(f"audio stream ended in {update.chat_id}; advancing queue", flush=True)
        await handle_end(update.chat_id)
    except Exception as e:
        print("on_end failed:", repr(e)[:200], flush=True)


async def play_query(m: Message, force: bool = False):
    query = " ".join(m.command[1:]).strip()
    if not query and m.reply_to_message:
        query = (m.reply_to_message.text or "").strip()
    if not query:
        command = "/mxplayforce" if force else "/mxstartplay"
        return await m.reply_text(f"Usage: {command} song name or link")

    status = await m.reply_text("Searching…")
    # Joining the group and resolving the song are independent: overlap them so
    # the first play is not delayed by the membership check.
    assistant_task = asyncio.create_task(ensure_assistant_in_group(m.chat.id))
    try:
        info = await asyncio.to_thread(sources.resolve, query)
    except Exception as e:
        await assistant_task
        print("search failed:", repr(e)[:400], flush=True)
        return await status.edit_text("Couldn't find a playable version of that song on any source. "
                                      "Try another name, or try again in a minute.")
    if not await assistant_task:
        return await status.edit_text(
            "I couldn't add the assistant account to this group. "
            "Please make the music bot an admin with permission to invite users, "
            "then try /mxplay again."
        )

    t = Track(
        title=info.get("title", "Unknown"),
        url=info["url"],
        page=info.get("page", ""),
        duration=info.get("duration") or 0,
        by=m.from_user.first_name if m.from_user else "Unknown",
        artist=info.get("artist") or "",
        headers=info.get("headers") or {},
        source=info.get("source", ""),
        proxy=info.get("proxy") or "",
        query=query,
        resolved_at=time.time(),
        thumbs=info.get("thumbnails") or [],
        official=bool(info.get("official")),
    )
    if force:
        previous = queues.pop(m.chat.id, [])
        for old in previous:
            old.end_seen = True
            # Final cards are updated in the background so /mxplayforce starts immediately.
            spawn(finalize_track(old, "stopped"))
        q = queues.setdefault(m.chat.id, [])
        q.append(t)
        t.end_seen = False
        t.ignore_end_until = time.time() + 8.0
        # Let the replaced stream stop before asking PyTgCalls to play the new one.
        await asyncio.sleep(0.2)
    else:
        q = queues.setdefault(m.chat.id, [])
        q.append(t)

    if force or len(q) == 1:
        try:
            await start_with_fallback(m.chat.id, t)
        except Exception as e:
            q.clear()
            return await status.edit_text(f"Couldn't start playback. Make sure a voice chat is open and the assistant is in the group.\n{e}")
        try:
            used = await announce(m.chat.id, t, message_id=status.id)
        except Exception as e:
            print("card send failed, playback continues:", repr(e)[:160], flush=True)
            used = 0
        if used != status.id:
            try:
                await status.delete()
            except Exception:
                pass
    else:
        await status.delete()
        await api("sendMessage", {"chat_id": m.chat.id, "parse_mode": "HTML",
                                  "text": f"{pe('queue')} <b>QUEUED #{len(q) - 1}</b>\n\n<b>{html.escape(t.title)}</b>\n{pe('user')} {html.escape(t.by)}"})


@bot.on_message(filters.command(["mxplay", "mxstartplay"]) & filters.group)
async def play(_, m: Message):
    await play_query(m)


@bot.on_message(filters.command("mxplayforce") & filters.group)
async def mxplayforce(_, m: Message):
    await play_query(m, force=True)


@bot.on_message(filters.command("mxtestaudio") & filters.group)
async def test_audio(_, m: Message):
    if queues.get(m.chat.id):
        return await m.reply_text("Stop current playback with /mxstopplay, then run /mxtestaudio.")
    status = await m.reply_text("Starting an 8-second audio test…")
    t = Track(
        title="440 Hz audio test",
        url="sine=frequency=440:duration=8",
        page="",
        duration=8,
        by=m.from_user.first_name if m.from_user else "Unknown",
        source="test tone",
        ffmpeg_parameters="-f lavfi",
        resolved_at=time.time(),
    )
    q = queues.setdefault(m.chat.id, [])
    q.append(t)
    try:
        await start_stream(m.chat.id, t)
        await status.delete()
        await announce(m.chat.id, t)
    except Exception as e:
        q.clear()
        print("audio test failed:", repr(e)[:200], flush=True)
        await status.edit_text("The audio test could not start. Check the voice chat and Railway logs.")


def _cmd(name: str, action: str):
    @bot.on_message(filters.command(name) & filters.group)
    async def handler(_, m: Message):
        try:
            text = await act(m.chat.id, action)
        except Exception as e:
            print(f"/{name} failed:", repr(e)[:200], flush=True)
            text = "Something went wrong."
        await m.reply_text(text)
        if m.from_user:
            await action_notice(m.chat.id, m.from_user, action, text, getattr(m, "message_thread_id", None))


for _n, _a in (("mxpause", "pause"), ("mxresume", "resume"), ("mxskip", "skip"),
               ("mxstopplay", "stop"), ("mxprevious", "prev"), ("mxshuffle", "shuffle"),
               ("mxloop", "loop")):
    _cmd(_n, _a)


_ACTION_DONE = {
    "pause": "paused the song",
    "resume": "resumed the song",
    "skip": "skipped the song",
    "prev": "went back to the previous song",
    "shuffle": "shuffled the queue",
    "stop": "stopped the playback",
}
_ACTION_TRIED = {
    "pause": "tried to pause the song",
    "resume": "tried to resume the song",
    "skip": "tried to skip the song",
    "prev": "tried to go back to the previous song",
    "shuffle": "tried to shuffle the queue",
    "stop": "tried to stop the playback",
}
_NOOP_RESULTS = {
    "Nothing is playing.",
    "Already paused.",
    "Already playing.",
    "No previous song.",
    "Not enough songs in the queue.",
    "Something went wrong.",
}


def _mention_html(name: str, user_id, username: str | None = None) -> str:
    """Fallback HTML mention: username mention first, then clickable name link."""
    if username:
        return f"@{username}"
    if user_id:
        return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
    return html.escape(name)


def _plain_mention(name: str, username: str | None = None) -> str:
    return f"@{username}" if username else name


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _display_name(obj) -> str:
    if not obj:
        return ""
    first = (getattr(obj, "first_name", "") or "").strip()
    last = (getattr(obj, "last_name", "") or "").strip()
    return " ".join(part for part in (first, last) if part) or (getattr(obj, "title", "") or "").strip()


async def _actor_name(user) -> tuple[str, str | None, int | None]:
    """Get a full name even when a callback only carries a user id."""
    if not user:
        return "Someone", None, None
    uid = getattr(user, "id", None)
    name = _display_name(user)
    username = (getattr(user, "username", "") or "").strip() or None
    if uid and not name:
        # The assistant user session shares the group and can resolve users even
        # when the bot client only received a sparse callback user object.
        for client_name, client in (("assistant", assistant), ("bot", bot)):
            try:
                full = await asyncio.wait_for(client.get_users(uid), timeout=3)
                name = _display_name(full)
                username = (getattr(full, "username", "") or "").strip() or username
                if name:
                    print(f"resolved button user via {client_name}: {uid}", flush=True)
                    break
            except Exception as e:
                print(f"{client_name} could not resolve button user {uid}:", repr(e)[:120], flush=True)
    if not name:
        name = username or (f"User {uid}" if uid else "Someone")
    name = " ".join(name.split())
    return name, username, uid


def _action_phrase(action: str, result: str) -> str:
    if action == "loop":
        if "Loop on" in result:
            return "turned loop on"
        if "Loop off" in result:
            return "turned loop off"
        return "tried to toggle loop"
    done = _ACTION_DONE.get(action)
    if not done:
        return "used the player controls"
    if result in _NOOP_RESULTS:
        return _ACTION_TRIED.get(action, done)
    return done


async def action_notice(chat_id: int, user, action: str, result: str, thread_id: int | None = None):
    """Post a separate English attribution line under the card for every button press."""
    phrase = _action_phrase(action, result)
    name, username, uid = await _actor_name(user)
    prefix = f"{pe('user', True)} "
    text = f"{prefix}{name} {phrase}."

    if uid:
        actor = {"id": uid, "is_bot": bool(getattr(user, "is_bot", False)), "first_name": name}
        if username:
            actor["username"] = username
        entity = {
            "type": "text_mention",
            "offset": _utf16_len(prefix),
            "length": _utf16_len(name),
            "user": actor,
        }
        payload = {"chat_id": chat_id, "text": text, "entities": [entity]}
        if thread_id:
            payload["message_thread_id"] = thread_id
        res = await api("sendMessage", payload)
        if res.get("ok"):
            return
        print("action notice text_mention failed:", res.get("description"), flush=True)

    html_text = f"{prefix}{_mention_html(name, uid, username)} {phrase}."
    payload = {"chat_id": chat_id, "text": html_text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    res = await api("sendMessage", payload)
    if res.get("ok"):
        return

    print("action notice failed:", res.get("description"), flush=True)
    try:
        kwargs = {"parse_mode": ParseMode.HTML}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        await assistant.send_message(chat_id, html_text, **kwargs)
        return
    except Exception as e:
        print("action notice assistant fallback failed:", repr(e)[:160], flush=True)

    plain = f"👤 {_plain_mention(name, username)} {phrase}."
    fallback = {"chat_id": chat_id, "text": plain}
    if thread_id:
        fallback["message_thread_id"] = thread_id
    res = await api("sendMessage", fallback)
    if res.get("ok"):
        return

    print("action notice plain fallback failed:", res.get("description"), flush=True)
    try:
        kwargs = {"message_thread_id": thread_id} if thread_id else {}
        await bot.send_message(chat_id, plain, **kwargs)
    except Exception as e:
        print("action notice pyrogram fallback failed:", repr(e)[:160], flush=True)


@bot.on_callback_query(filters.regex(r"^ctl:"))
async def buttons(_, cq: CallbackQuery):
    action = cq.data.split(":", 1)[1]
    try:
        text = await act(cq.message.chat.id, action)
    except Exception:
        text = "Something went wrong."
    try:
        await cq.answer(text)
    except Exception:
        pass
    await action_notice(cq.message.chat.id, cq.from_user, action, text,
                        getattr(cq.message, "message_thread_id", None))


@bot.on_message(filters.command("mxqueue") & filters.group)
async def show_queue(_, m: Message):
    q = queues.get(m.chat.id)
    if not q:
        return await m.reply_text("Queue is empty.")
    lines = [f"{pe('now')} <b>NOW PLAYING</b>\n{html.escape(q[0].title)} · {fmt(q[0].duration)}"]
    if len(q) > 1:
        lines.append(f"\n{pe('queue')} <b>UP NEXT</b>")
        lines += [f"{i}. {html.escape(x.title)} · {fmt(x.duration)}" for i, x in enumerate(q[1:11], 1)]
    await api("sendMessage", {"chat_id": m.chat.id, "parse_mode": "HTML", "text": "\n".join(lines)})


async def watchdog():
    """If Telegram stops answering for ~3 minutes, exit so the supervisor / Railway restarts everything."""
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(asyncio.gather(bot.get_me(), assistant.get_me()), 30)
            fails = 0
        except Exception as e:
            fails += 1
            print(f"watchdog: Telegram check failed ({fails}/3): {repr(e)[:120]}", flush=True)
            if fails >= 3:
                print("watchdog: restarting the bot", flush=True)
                os._exit(1)


async def ensure_assistant_in_group(chat_id: int) -> bool:
    """Join the assistant user account using a temporary invite link created by the bot."""
    try:
        await assistant.get_chat(chat_id)
        return True
    except Exception as lookup_error:
        print(f"assistant is not in group {chat_id}: {type(lookup_error).__name__}", flush=True)

    invite = None
    try:
        invite = await bot.create_chat_invite_link(
            chat_id, name="Music assistant auto-join"
        )
        await assistant.join_chat(invite.invite_link)
        # Confirm membership; join-request-only groups must not be treated as joined.
        await assistant.get_chat(chat_id)
        print(f"assistant joined group {chat_id}", flush=True)
        return True
    except Exception as e:
        print(f"assistant could not join group {chat_id}: {repr(e)[:240]}", flush=True)
        return False
    finally:
        if invite is not None:
            try:
                await bot.revoke_chat_invite_link(chat_id, invite.invite_link)
            except Exception as revoke_error:
                print(f"temporary invite cleanup failed: {repr(revoke_error)[:120]}", flush=True)


@bot.on_chat_member_updated()
async def on_bot_group_membership(_, update):
    """When this bot is added to a group, invite the assistant account into it first."""
    try:
        me = await bot.get_me()
        member = getattr(update, "new_chat_member", None)
        user = getattr(member, "user", None)
        if not user or user.id != me.id:
            return
        status = getattr(member, "status", "")
        status = getattr(status, "value", status)
        if str(status).lower() not in ("member", "administrator"):
            return
        chat = update.chat
        chat_type = getattr(chat, "type", "")
        chat_type = getattr(chat_type, "value", chat_type)
        if str(chat_type).lower() not in ("group", "supergroup"):
            return
        if not await ensure_assistant_in_group(chat.id):
            print(
                f"assistant auto-join needs the bot to be an admin in group {chat.id} "
                "so it can create an invite link",
                flush=True,
            )
    except Exception as e:
        print("automatic assistant group join failed:", repr(e)[:240], flush=True)


async def start_with_floodwait(client, name):
    """Start a client, sleeping out Telegram's FloodWait instead of crashing (restarts extend the ban)."""
    while True:
        try:
            await client.start()
            return
        except FloodWait as e:
            wait_seconds = max(int(getattr(e, "value", 0)), 1) + 5
            print(f"Telegram FloodWait starting {name}; waiting {wait_seconds}s.", flush=True)
            await asyncio.sleep(wait_seconds)


async def main():
    await start_with_floodwait(bot, "bot")
    # Telegram may temporarily rate-limit assistant-session authorization.
    # Keep the bot client online while respecting
    # Telegram's exact FloodWait duration instead of crashing into Railway restarts.
    while True:
        try:
            await assistant.start()
            break
        except FloodWait as e:
            wait_seconds = max(int(getattr(e, "value", 0)), 1) + 5
            print(
                f"Telegram FloodWait during assistant authorization; "
                f"waiting {wait_seconds}s before retrying.",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)
    await calls.start()
    try:
        await load_emojis()
    except Exception as e:
        print("emoji packs not loaded (plain emojis are used):", repr(e)[:120], flush=True)
    asyncio.create_task(watchdog())
    me = await bot.get_me()
    print(f"Bot is running as @{me.username}", flush=True)
    await idle()
    print("Shutting down...", flush=True)
    for c in (bot, assistant):
        try:
            await asyncio.wait_for(c.stop(), 5)
        except Exception:
            pass


if __name__ == "__main__":
    LOOP.run_until_complete(main())
