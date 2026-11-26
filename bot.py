"""Clean voice-chat music bot. Best-quality audio, plain text, no thumbnails, no effects."""
import asyncio

# One event loop for everything: pyrogram binds to the loop that exists when it is created.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

import html
import logging
import json
import os
import random
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
from pyrogram.errors import FloodWait
from pyrogram.raw import functions
from pyrogram.raw.types import InputStickerSetShortName
from pyrogram.types import (
    CallbackQuery,
    Message,
)
from pytgcalls import PyTgCalls, filters as call_filters
from pytgcalls.types import AudioQuality, MediaStream, StreamEnded

# FFmpeg DEBUG output floods the logs (HLS reconnect/playlist lines many times a second).
logging.getLogger('ffmpeg').setLevel(logging.WARNING)

import sources
from seek import parse_seek_position

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION = os.environ["SESSION_STRING"]  # assistant account that joins the voice chat

# Every fresh bot login (auth.ImportBotAuthorization) counts toward Telegram's flood limit, and each
# Railway deploy used to start with a brand-new in-memory session. Keep the bot session on disk
# when a persistent directory exists (mount a Railway volume at /data, or set BOT_SESSION_DIR) so
# redeploys and restarts reuse it instead of logging in again.
_sess_dir = os.environ.get("BOT_SESSION_DIR") or ("/data" if os.path.isdir("/data") else "")
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


queues: dict[int, list[Track]] = {}
history: dict[int, list[Track]] = {}
loop: dict[int, bool] = {}


def fmt(sec: int) -> str:
    m, s = divmod(int(sec or 0), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


STREAM_MAX_AGE = 3000  # seconds; YouTube stream links expire after a few hours


async def reresolve(t: Track):
    """Find the song again (skipping sources that already failed for it) and update the track."""
    info = await asyncio.to_thread(sources.resolve, t.query or t.title, t.bad)
    t.url, t.headers, t.source = info["url"], info.get("headers") or {}, info["source"]
    t.page = info.get("page") or t.page
    t.duration = info.get("duration") or t.duration
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
    "now": ("🎧", ["🎧", "🎵", "🎶"]),
    "pause": ("⏸", ["⏸", "⏯"]),
    "play": ("▶️", ["▶️", "▶", "🎶"]),
    "user": ("👤", ["👤", "🙋", "👑"]),
    "prev": ("⏮", ["⏮", "◀️", "⬅️"]),
    "skip": ("⏭", ["⏭", "⏩", "➡️"]),
    "shuffle": ("🔀", ["🔀", "🔄"]),
    "loop": ("🔁", ["🔁", "🔂", "♻️"]),
    "stop": ("⏹", ["⏹", "🛑", "❌"]),
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


def pe(role: str) -> str:
    ch, cid = EMO.get(role, (ROLES[role][0], ""))
    return f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>' if cid else ch


LINE = "━━━━━━━━━━━━━━━━━━"


def elapsed(t: Track) -> int:
    now = t.paused_at or time.time()
    return max(0, min(int(now - t.started), t.duration or 10**9))


def card(t: Track) -> str:
    e, d = elapsed(t), t.duration
    filled = round(12 * e / d) if d else 0
    bar = "▰" * filled + "▱" * (12 - filled)
    icon = pe("pause") if t.paused_at else pe("play")
    head = f"{pe('pause')} <b>PAUSED</b>" if t.paused_at else f"{pe('now')} <b>NOW PLAYING</b>"
    artist = f"\n{html.escape(t.artist)}" if t.artist else ""
    return (
        f"{head}\n\n<b>{html.escape(t.title.upper())}</b>{artist}\n\n{LINE}\n"
        f"{icon}  {bar}  <code>{fmt(e)} / {fmt(d)}</code>\n\n"
        f"{pe('user')} Requested by: {html.escape(t.by)}\n{LINE}"
    )


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
        return json.load(e)


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
    return {"inline_keyboard": [
        [B("prev", "Previous", "prev", "primary"), pause, B("skip", "Skip", "skip", "primary")],
        [B("shuffle", "Shuffle", "shuffle", "primary"),
         B("loop", "Loop ✓" if on else "Loop", "loop", "success" if on else "primary"),
         B("stop", "Stop", "stop", "danger")],
    ]}


async def refresh(chat_id: int, t: Track):
    await api("editMessageText", {
        "chat_id": chat_id, "message_id": t.msg, "text": card(t), "parse_mode": "HTML",
        "reply_markup": kb(chat_id, t), "link_preview_options": {"is_disabled": True},
    }, kb(chat_id, t, plain=True))


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
    ffmpeg_parameters = t.ffmpeg_parameters
    if seek_to is not None:
        ffmpeg_parameters = f"{ffmpeg_parameters or ''} --audio --start -ss {seek_to}".strip()
    kw = dict(
        audio_parameters=AudioQuality.STUDIO,  # 48 kHz stereo, top quality
        video_flags=MediaStream.Flags.IGNORE,
        ffmpeg_parameters=ffmpeg_parameters,
    )
    # Preserve provider headers; silently dropping Referer/User-Agent can turn a valid
    # stream into an HTTP error or an apparently silent playback.
    stream = MediaStream(t.url, headers=t.headers, **kw)
    try:
        await calls.play(chat_id, stream)
    except Exception as e:
        print("calls.play failed:", repr(e), flush=True)
        raise
    t.started = time.time() - (seek_to or 0)
    t.paused_at = 0.0


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


async def start_with_fallback(chat_id: int, t: Track):
    """Start this exact requested track; on media failure, retry other providers only."""
    attempts = max(1, len(sources.SOURCES))
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
            if t.source:
                t.bad.add(t.source)
            if attempt + 1 >= attempts:
                break
            try:
                await reresolve(t)
            except Exception as resolve_error:
                last_error = resolve_error
                break
    raise RuntimeError(f"all available audio sources failed: {last_error}") from last_error


async def announce(chat_id: int, t: Track):
    res = await api("sendMessage", {
        "chat_id": chat_id, "text": card(t), "parse_mode": "HTML",
        "reply_markup": kb(chat_id, t), "link_preview_options": {"is_disabled": True},
    }, kb(chat_id, t, plain=True))
    t.chat_id = chat_id
    t.msg = res.get("result", {}).get("message_id", 0)
    t.task = asyncio.create_task(updater(t))


def finish(t: Track):
    if t.task:
        t.task.cancel()


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


async def play_next(chat_id: int, force: bool = False):
    """Advance the queue. A track that can't be played is skipped - the queue never gets stuck."""
    q = queues.get(chat_id, [])
    if q and loop.get(chat_id) and not force:
        try:
            await start_with_fallback(chat_id, q[0])  # replay same track, same card
            return
        except Exception as e:
            print("loop replay failed:", repr(e)[:200], flush=True)
    while True:
        if q:
            done = q.pop(0)
            finish(done)
            if done.started:
                history.setdefault(chat_id, []).append(done)
                del history[chat_id][:-20]
        if not q:
            await leave(chat_id)
            return
        try:
            await start_with_fallback(chat_id, q[0])
            await announce(chat_id, q[0])
            return
        except Exception as e:
            print("could not play, skipping:", q[0].title, repr(e)[:200], flush=True)
            await notify(chat_id, f"Couldn't play \"{q[0].title}\", skipping it.")


async def handle_end(chat_id: int):
    q = queues.get(chat_id)
    if not q:
        return
    t = q[0]
    # Stream "ended" almost instantly although the song is long = the link was dead. Try another source.
    quick = bool(t.started) and not t.paused_at and (time.time() - t.started) < 4 and t.duration > 20
    if quick and t.retries < 3:
        t.retries += 1
        t.bad.add(t.source)
        print(f"stream died right away ({t.source}), trying another source (try {t.retries})", flush=True)
        try:
            await start_with_fallback(chat_id, t)
            return
        except Exception as e:
            print("no other source worked:", repr(e)[:200], flush=True)
    await play_next(chat_id, force=quick)


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
        await play_next(chat_id, force=True)
        return "⏭ Skipped"
    if action == "prev":
        h = history.get(chat_id)
        if not h:
            return "No previous song."
        finish(cur)
        q.insert(0, h.pop())
        await start_with_fallback(chat_id, q[0])
        await announce(chat_id, q[0])
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
        for x in queues.pop(chat_id, []):
            finish(x)
        loop.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        await api("editMessageText", {"chat_id": chat_id, "message_id": cur.msg,
                                      "text": f"{pe('stop')} <b>STOPPED</b>", "parse_mode": "HTML",
                                      "reply_markup": {"inline_keyboard": []}})
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

    # The assistant must be a group member before it can join that group's voice chat.
    if not await ensure_assistant_in_group(m.chat.id):
        return await m.reply_text(
            "I couldn't add the assistant account to this group. "
            "Please make the music bot an admin with permission to invite users, "
            "then try /mxplay again."
        )

    status = await m.reply_text("Searching…")
    try:
        info = await asyncio.to_thread(sources.resolve, query)
    except Exception as e:
        print("search failed:", repr(e)[:400], flush=True)
        return await status.edit_text("Couldn't find a playable version of that song on any source. "
                                      "Try another name, or try again in a minute.")

    t = Track(
        title=info.get("title", "Unknown"),
        url=info["url"],
        page=info.get("page", ""),
        duration=info.get("duration") or 0,
        by=m.from_user.first_name if m.from_user else "Unknown",
        artist=info.get("artist") or "",
        headers=info.get("headers") or {},
        source=info.get("source", ""),
        query=query,
        resolved_at=time.time(),
    )
    if force:
        previous = queues.pop(m.chat.id, [])
        for old in previous:
            finish(old)
        if previous:
            try:
                await calls.leave_call(m.chat.id)
            except Exception as e:
                print("forced stop failed:", repr(e)[:200], flush=True)
        q = queues.setdefault(m.chat.id, [])
        q.append(t)
    else:
        q = queues.setdefault(m.chat.id, [])
        q.append(t)

    if force or len(q) == 1:
        try:
            await start_with_fallback(m.chat.id, t)
        except Exception as e:
            q.clear()
            return await status.edit_text(f"Couldn't start playback. Make sure a voice chat is open and the assistant is in the group.\n{e}")
        await status.delete()
        await announce(m.chat.id, t)
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
        ffmpeg_parameters="--audio --start -f lavfi",
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


for _n, _a in (("mxpause", "pause"), ("mxresume", "resume"), ("mxskip", "skip"),
               ("mxstopplay", "stop"), ("mxprevious", "prev"), ("mxshuffle", "shuffle"),
               ("mxloop", "loop")):
    _cmd(_n, _a)


@bot.on_callback_query(filters.regex(r"^ctl:"))
async def buttons(_, cq: CallbackQuery):
    try:
        text = await act(cq.message.chat.id, cq.data.split(":", 1)[1])
    except Exception:
        text = "Something went wrong."
    await cq.answer(text)


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
