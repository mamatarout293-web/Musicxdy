"""Multi-source song resolver.

YouTube is the primary search/playback resolver, followed by Audius and SoundCloud.
Configured healthy sources are searched concurrently and the first playable result is returned.
Each candidate is checked for relevance and unwanted alternate versions before playback. Sources
that fail are temporarily cooled down and tried after healthy sources.

Env options (all optional):
  SOURCE_ORDER      e.g. "youtube,audius,soundcloud"   (subset / tie-break preference)
  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET  optional Spotify catalog lookup credentials
  JAMENDO_CLIENT_ID free Jamendo key (enables the "jamendo" source)
  COOKIES_URL       URL of a Netscape/JSON cookie export; re-downloaded every COOKIES_REFRESH seconds
  COOKIES_REFRESH   seconds between COOKIES_URL refreshes (default 21600 = 6 hours)
  COOKIES_FILE      path to a Netscape/JSON cookie file (default: cookies.txt next to this file)
  COOKIES_TXT / COOKIES_BASE64   optional YouTube cookies (default: cookies.txt next to this file)
  COOKIES_FROM_BROWSER           auto-detect an existing logged-in browser profile, or set chrome[:profile],firefox,etc.
  ENABLE_YOUTUBE                  set to 0 to disable YouTube (enabled by default)
  RESOLVE_TIMEOUT   max seconds spent on one search (default 55)
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from difflib import SequenceMatcher

from yt_dlp import YoutubeDL

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 8
DEADLINE = int(os.environ.get("RESOLVE_TIMEOUT", "55"))
URL_RE = re.compile(r"^https?://", re.I)
# Optional HTTP proxy (ideally residential) used ONLY for YouTube. YouTube stream
# links are locked to the IP that resolved them, so playback must use it too.
YT_PROXY = os.environ.get("YT_PROXY", "").strip() or None
# Verifying a YouTube stream URL costs an extra round-trip and can fail when the
# signed URL is bound to the proxy/IP that resolved it. Playback fails fast on a
# dead link, so verification is opt-in for YouTube (VERIFY_YOUTUBE=1).
VERIFY_YOUTUBE = os.environ.get("VERIFY_YOUTUBE", "0").strip().lower() in {"1", "true", "yes", "on"}
try:
    MIN_AUDIO_KBPS = float(os.environ.get("MIN_AUDIO_KBPS", "120") or 120)
except ValueError:
    MIN_AUDIO_KBPS = 120.0


class NotFound(Exception):
    """Source works, but has no (relevant) result. Does not penalise the source."""


class Unavailable(Exception):
    """No source could provide a playable stream."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors) or "no source available")
        self.errors = errors


def log(*a):
    print("[sources]", *a, flush=True)


# ───────────────────────── helpers ─────────────────────────
def _open(url: str, headers: dict | None = None, timeout: float = HTTP_TIMEOUT,
          proxy: str | None = None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url: str, timeout: float = HTTP_TIMEOUT):
    with _open(url, {"Accept": "application/json"}, timeout) as r:
        return json.loads(r.read(3_000_000).decode("utf-8", "replace"))


def playable(url: str, headers: dict | None = None, proxy: str | None = None) -> bool:
    """Verify that a URL returns readable media bytes, not just an HTTP response."""
    h = dict(headers or {})
    h.pop("Accept-Encoding", None)
    h["Range"] = "bytes=0-4095"
    try:
        with _open(url, h, proxy=proxy) as r:
            # A range request may be honoured (206) or ignored (200). Other statuses
            # are not evidence of a usable stream; in particular, 416 is not playable.
            if r.status not in (200, 206):
                return False
            content_type = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type in {"text/html", "application/json", "text/json", "application/xml"}:
                return False
            chunk = r.read(4096)
            if not chunk:
                return False
            probe = chunk.lstrip().lower()
            if probe.startswith((b"<!doctype html", b"<html", b"{", b"[{")):
                return False
            # Some providers label HLS manifests as text/plain; accept them only
            # when the body is actually an HLS playlist.
            if content_type == "text/plain" and not probe.startswith(b"#extm3u"):
                return False
            return True
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _finish(info: dict, allow_low_quality: bool = False) -> dict:
    if not info.get("url"):
        raise RuntimeError("no stream url")
    source = info.get("source") or ""
    abr = asr = 0
    try:
        abr = float(info.get("abr") or 0)
        asr = int(info.get("asr") or 0)
    except (TypeError, ValueError):
        pass
    vcodec = str(info.get("vcodec") or "none").lower()
    muxed = vcodec not in {"none", ""}
    if not allow_low_quality:
        if abr and abr < MIN_AUDIO_KBPS:
            raise RuntimeError(f"{source}: audio bitrate below {MIN_AUDIO_KBPS:.0f} kbps ({abr:.0f} kbps)")
        if asr and asr < 32000:
            raise RuntimeError(f"{source}: sample rate too low ({asr} Hz)")
        # Audio-only is preferred. A muxed video with no audio bitrate info is
        # usually YouTube's low-quality 360p format 18.
        if muxed and not abr:
            raise RuntimeError(f"{source}: only a muxed video format is available")
    verify = source != "youtube" or VERIFY_YOUTUBE
    if verify and not playable(info["url"], info.get("headers") or {},
                               YT_PROXY if source == "youtube" else None):
        raise RuntimeError(f"{source}: stream not reachable")
    return info


_STOP = {"official", "video", "audio", "lyrics", "lyric", "song", "full", "new", "hd",
         "the", "a", "of", "ft", "feat", "and", "by", "original", "version", "music",
         # Language/industry hints are not part of the song name.
         "hindi", "english", "bengali", "bangla", "odia", "oriya", "punjabi",
         "tamil", "telugu", "kannada", "malayalam", "marathi", "gujarati",
         "bhojpuri", "assamese", "urdu", "sanskrit", "sambalpuri", "haryanvi",
         "rajasthani", "konkani", "kashmiri", "nepali", "sinhala",
         "indian", "bollywood", "tollywood", "kollywood", "sandalwood"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\w+", (s or "").lower()) if t not in _STOP}


def _channel_tokens(s: str) -> set[str]:
    # "ImagineDragonsVEVO" -> {"imagine", "dragons", "vevo"}
    return _tokens(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or ""))


def _token_match(qt: str, ct: str) -> float:
    if qt == ct:
        return 1.0
    if abs(len(qt) - len(ct)) > max(2, len(qt) // 3):
        return 0.0
    if len(qt) < 4 or len(ct) < 4:
        if abs(len(qt) - len(ct)) > 1 or qt[:1] != ct[:1]:
            return 0.0
    return SequenceMatcher(None, qt, ct).ratio()


def _coverage(query_tokens: set[str], have_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    total = 0.0
    for qt in query_tokens:
        best = max((_token_match(qt, ct) for ct in have_tokens), default=0.0)
        if best >= 0.8:
            total += best
    return total / len(query_tokens)


def _relevant(query: str, title: str, artist: str = "") -> bool:
    """Fail closed on weak matches, but tolerate small spelling mistakes."""
    q = _tokens(query)
    if not q:
        return False
    return _coverage(q, _tokens(f"{title} {artist}")) >= 0.7


# Hard variants are never played unless the user typed the keyword themselves.
_VARIANT_HARD = re.compile(
    r"\b(?:mix(?:es)?|remix(?:es)?|mashup|medley|tiktok|tik\s*tok|"
    r"reprised?|female\s+version|male\s+version|"
    r"acoustic(?:\s+version)?|piano\s+version|"
    r"lo[ -]?fi|fan[ -]?made|bootleg|rework|flip|"
    r"sped\s*up|speed\s*up|slowed(?:\s*\+\s*reverb)?|reverb|"
    r"nightcore|8d(?:\s+audio)?|karaoke|cover|live|instrumental|"
    r"bass\s*boost(?:ed)?|extended|dj\s*set|"
    r"edit(?:s|ed)?|status|whatsapp|shorts?|"
    r"reaction|parody|amv|anime|"
    r"ringtone|teaser|trailer|behind\s+the\s+scenes|"
    r"1\s*hour|hour\s+loop|loop|"
    r"ai\s*(?:cover|version)|"
    r"video\s*edit|fan\s*edit|"
    r"concert|festival|performance|unplugged|remake|"
    r"interview|podcast)\b|"
    r"कवर|रीमिक्स|रिमिक्स|स्लोड|रिवर्ब|8डी|"
    r"কভার|রিমিক্স|রিমিক্স|স্লোড|রিভার্ব|৮ডি|"
    r"ਕਵਰ|ਰੀਮਿਕਸ|ਸਲੋ|ਰੀਵਰਬ|8ਡੀ|"
    r"କଭର|ରିମିକ୍ସ|ସ୍ଲୋ|ରିଭର୍ବ|8ଡି|"
    r"கவர்|ரீமிக்ஸ்|ஸ்லோ|ரிவர்ப்|8டி|"
    r"కవర్|రీమిక్స్|స్లో|రివర్బ్|8డి", re.I
)
# Lyrics videos are a softer fallback: used only when nothing official exists.
_VARIANT_SOFT = re.compile(
    r"\b(?:lyrics?|lyric\s*video)\b|"
    r"लिरिक्स|লিরিক্স|ਲਿਰਿਕਸ|ଲିରିକ୍ସ|பாடல்\s*வரிகள்|సాహిత్యం", re.I
)
_OFFICIAL_TITLE = re.compile(
    r"\b(?:official\s*(?:music\s*)?(?:video|audio|visuali[sz]er)|"
    r"official\s+(?:lyric|lyrics)\s*video|official)\b", re.I
)
# Verified is the strongest signal, but it is not always present in flat search
# results. These are the channels that publish the real masters.
_LABEL_CHANNEL = re.compile(
    r"\b(?:t[- ]?series|sony music|zee music|tips|yrf|yash raj|saregama|"
    r"ishtar|speed records|white hill|desi music|saga music|times music|"
    r"ultra|wave music|srg|venus|eros|shemaroo|rajshri|aditya music|"
    r"lahari|think music|junglee|warner music|universal music|atlantic|"
    r"columbia|rca|capitol|interscope|republic|island|def jam|empire|"
    r"300 entertainment|vevo|topic|"
    # Bengali
    r"svf|eskay|grassroot|surinder|jeetz|amara muzik|asha audio|cozmik|"
    r"dhrubo|mayur|saregama bangla|sony music east|zee music bangla|"
    r"times music bangla|t[- ]?series bangla|rdc bangla|angel digital|"
    r"orion multimedia|pinky audio|soundtek|shemaroo bangla|rtv music|"
    r"anupam movie songs|"
    # Odia
    r"sarthak|dhoom music|tarang music|tarang tv|tarang cine|kanak music|"
    r"odiaone|sidharth music|astar music|suriya music|global music junction|"
    r"saregama odia|zee music odia|sony music odia|"
    r"t[- ]?series odia|aditya music odia|jagannath music|laxmi music|"
    r"bhabani music|rudra music|maa tarini music|"
    # Punjabi
    r"humble music|t[- ]?series apna punjab|rehaan records|moviebox|"
    r"vip records|finetouch|jass records|gold media|5911 records|"
    r"single track studios|burfi music|navrattan|ptc records|planet recordz|"
    r"lokdhun|gem tunes|saregama punjabi|zee music punjabi|"
    r"sony music punjabi|times music punjabi|ishtar punjabi|brown studios|"
    r"crown records|"
    # English / international
    r"fueled by ramen|big machine|young money|ovo|xo|good music|top dawg|"
    r"aftermath|shady|quality control|alamo|sub pop|xl recordings|domino|"
    r"88rising|monstercat|spinnin|armada|anjunabeats|defected|"
    r"ministry of sound|because music)\b", re.I
)
_LATIN_TEXT = re.compile(r"[A-Za-z]")
_NON_LATIN_TEXT = re.compile(r"[^\x00-\x7F]")

# Language hints are removed from coverage (so "jhumka odia" still matches "Jhumka")
# but they bias ranking towards that language's labels and script.
_LANG_HINTS = {
    "hindi": (["hindi", "हिंदी", "हिन्दी"],
              ["t-series", "sony music india", "zee music", "tips", "yrf",
               "saregama", "wave music", "srg", "aditya music"]),
    "english": (["english"],
                ["fueled by ramen", "big machine", "atlantic", "columbia", "rca",
                 "capitol", "interscope", "republic", "island", "universal music",
                 "warner music"]),
    "bengali": (["bengali", "bangla", "বাংলা"],
                ["svf", "eskay", "surinder", "grassroot", "amara muzik",
                 "saregama bangla", "zee music bangla", "sony music east",
                 "times music bangla", "rtv music", "anupam movie songs"]),
    "odia": (["odia", "oriya", "sambalpuri", "ଓଡ଼ିଆ", "ଓଡିଆ"],
             ["tarang", "sarthak", "dhoom music", "kanak music", "sidharth music",
              "amara muzik", "odiaone", "astar music", "suriya music",
              "global music junction"]),
    "punjabi": (["punjabi", "ਪੰਜਾਬੀ"],
                ["speed records", "white hill", "humble music", "saga music",
                 "ishtar punjabi", "rehaan", "moviebox", "t-series apna punjab",
                 "ptc records", "planet recordz"]),
    "tamil": (["tamil", "தமிழ்"],
              ["think music", "sony music south", "lahari", "aditya music",
               "divo", "saregama tamil"]),
    "telugu": (["telugu", "తెలుగు"],
               ["aditya music", "lahari", "sony music south", "zee music south",
                "saregama telugu", "mango music", "madhura audio"]),
}
_LANG_SCRIPTS = {
    "hindi": "\u0900-\u097F",
    "bengali": "\u0980-\u09FF",
    "punjabi": "\u0A00-\u0A7F",
    "odia": "\u0B00-\u0B7F",
    "tamil": "\u0B80-\u0BFF",
    "telugu": "\u0C00-\u0C7F",
}


def _language_bonus(query: str, title: str, channel: str) -> float:
    q = (query or "").lower()
    text = f"{title} {channel}".lower()
    bonus = 0.0
    for lang, (words, labels) in _LANG_HINTS.items():
        if not any(word in q for word in words):
            continue
        script = _LANG_SCRIPTS.get(lang)
        if (any(word in text for word in words)
                or any(label in text for label in labels)
                or (script and re.search(f"[{script}]", text))):
            bonus += 3.0
    return bonus


def _script_mismatch(query: str, text: str) -> bool:
    """Latin query with no token overlap against a title that contains native script."""
    if not _LATIN_TEXT.search(query or "") or not _NON_LATIN_TEXT.search(text or ""):
        return False
    return _coverage(_tokens(query), _tokens(text)) == 0


def _variant_unwanted(query: str, title: str, artist: str = "",
                      allow_soft: bool = False) -> bool:
    """Reject alternate edits named in either the title or the publishing channel."""
    text = f"{title} {artist}"
    if _VARIANT_HARD.search(text) and not _VARIANT_HARD.search(query):
        return True
    if _VARIANT_SOFT.search(text):
        # An official lyric video is still an official upload.
        return not (allow_soft or _VARIANT_SOFT.search(query)
                    or _OFFICIAL_TITLE.search(title or ""))
    return False


def _is_official(title: str, channel: str, query: str = "",
                 verified: bool = False, views: int = 0) -> bool:
    """Trust only platform-verified channels, known labels, Topic/VEVO, or the artist's own channel.

    A title that merely says "official" is not enough: re-uploads use that word too.
    A verified channel is not enough either when the query names an artist: verified
    cover artists upload the same title. Known labels, the artist's own channel, or
    a clearly popular upload are accepted instead.
    """
    ch = (channel or "").lower()
    q = _tokens(query)
    channel_tokens = _channel_tokens(channel)
    artist_match = False
    if len(q) >= 2 and 2 <= len(channel_tokens) <= 4:
        matched = sum(
            1 for ct in channel_tokens
            if any(_token_match(qt, ct) >= 0.8 for qt in q)
        )
        artist_match = matched == len(channel_tokens)
    try:
        views = int(views or 0)
    except (TypeError, ValueError):
        views = 0
    if verified:
        return (artist_match or len(q) < 3
                or bool(_LABEL_CHANNEL.search(ch)) or views >= 10_000_000)
    if _LABEL_CHANNEL.search(ch):
        return True
    return artist_match


def _match_score(query: str, title: str, artist: str = "",
                 duration: float = 0, verified: bool = False,
                 views: int = 0) -> float:
    q = _tokens(query)
    t = _tokens(title)
    a = _tokens(artist)
    if not q:
        return 0.0
    try:
        duration = float(duration or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        views = int(views or 0)
    except (TypeError, ValueError):
        views = 0
    overlap = _coverage(q, t | a)
    title_overlap = _coverage(q, t)
    score = overlap * 5 + title_overlap * 2
    if verified:
        score += 6
    if _is_official(title, artist, query, verified, views):
        score += 4
    if views:
        # Popular uploads are usually the real song; cap so it cannot hide official signals.
        score += min(14.0, math.log10(views) * 1.8)
    score += _language_bonus(query, title, artist)
    if duration:
        if duration < 90 or duration > 900:
            score -= 4
        elif 120 <= duration <= 600:
            score += 1
    if _variant_unwanted(query, title, artist):
        score -= 20
    return score


def _candidate_basic_ok(query: str, entry: dict, allow_soft: bool = False) -> bool:
    """Variant, live, view-count and duration checks; no title/query coverage."""
    title = entry.get("title") or ""
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
    if _variant_unwanted(query, title, artist, allow_soft=allow_soft):
        return False
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return False
    try:
        views = int(entry.get("view_count") or 0)
    except (TypeError, ValueError):
        views = 0
    # A handful of views is almost always a fresh re-upload, not the real song.
    if 0 < views < 1000:
        return False
    try:
        duration = float(entry.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    # Short clips are usually TikTok/Shorts edits; very long ones are loops.
    if duration and (duration < 45 or duration > 1200):
        return False
    return True


def _candidate_ok(query: str, entry: dict, min_coverage: float = 0.7,
                  allow_soft: bool = False) -> bool:
    if not _candidate_basic_ok(query, entry, allow_soft):
        return False
    title = entry.get("title") or ""
    artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
    return _coverage(_tokens(query), _tokens(f"{title} {artist}")) >= min_coverage


# ───────────────────────── cookies ─────────────────────────
# YouTube cookies are optional. They are only needed for videos that require a
# signed-in account (age/region restricted). Normal songs resolve without them,
# which is why the resolver tries cookie-less clients first: an expired export
# must never be able to block playback.
_COOKIE_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "google.com", "googlevideo.com", "googleapis.com",
    "gstatic.com", "ytimg.com", "googleusercontent.com",
)
_COOKIE_PATH = os.path.join(tempfile.gettempdir(), f"yt_cookies_{os.getpid()}.txt")
try:
    _COOKIE_REFRESH = float(os.environ.get("COOKIES_REFRESH", "21600") or 21600)
except ValueError:
    _COOKIE_REFRESH = 21600.0
_COOKIE_LOCK = threading.Lock()
_COOKIE_STATE: dict = {
    "signature": None, "count": 0, "file_sig": None, "remote_at": 0.0, "source": "",
}
COOKIES: str | None = None


def _is_google_cookie_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    if not d:
        return False
    if any(d == suffix or d.endswith("." + suffix) for suffix in _COOKIE_DOMAINS):
        return True
    # Country domains such as google.co.in / accounts.google.co.uk
    return bool(re.search(r"(?:^|\.)google\.[a-z]{2,3}(?:\.[a-z]{2})?$", d))


def _netscape_cookie_rows(text: str) -> list[str]:
    """Keep only Google/YouTube rows; a browser export also contains unrelated sites."""
    rows = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.lstrip("\ufeff").strip()
        if not line:
            continue
        prefix = ""
        if line.startswith("#HttpOnly_"):
            prefix, line = "#HttpOnly_", line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or not _is_google_cookie_domain(fields[0]):
            continue
        rows.append(prefix + "\t".join(fields))
    return rows


def _json_cookie_rows(text: str) -> list[str]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies") or data.get("Cookies") or []
    if not isinstance(data, list):
        raise ValueError("cookie JSON must be a list of cookie objects")
    rows = []
    for cookie in data:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None or not _is_google_cookie_domain(domain):
            continue
        try:
            expires = int(float(cookie.get("expirationDate") or cookie.get("expires")
                                or cookie.get("expiry") or 0))
        except (TypeError, ValueError):
            expires = 0
        prefix = "#HttpOnly_" if cookie.get("httpOnly") or cookie.get("httponly") else ""
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        rows.append(f"{prefix}{domain}\t{include_sub}\t{cookie.get('path') or '/'}\t"
                    f"{secure}\t{expires}\t{name}\t{value}")
    return rows


def _header_cookie_rows(text: str) -> list[str]:
    """Accept a raw `Cookie: a=b; c=d` header pasted into COOKIES_TXT."""
    rows = []
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            rows.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    return rows


def _cookie_row_key(row: str) -> tuple:
    plain = row[len("#HttpOnly_"):] if row.startswith("#HttpOnly_") else row
    fields = plain.split("\t")
    return (fields[0].lower(), fields[2], fields[5]) if len(fields) == 7 else (row,)


def _normalize_cookie_text(text: str) -> list[str]:
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        raise ValueError("empty cookie data")
    if text[0] in "[{":
        rows = _json_cookie_rows(text)
    elif "\t" in text:
        rows = _netscape_cookie_rows(text)
    else:
        rows = _header_cookie_rows(text)
    unique: dict[tuple, str] = {}
    for row in rows:
        unique[_cookie_row_key(row)] = row
    if not unique:
        raise ValueError("no Google/YouTube cookies found")
    return list(unique.values())


def _decode_cookie_base64(value: str) -> str:
    cleaned = re.sub(r"\s+", "", value)
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        raw = base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    return raw.decode("utf-8-sig")


def _store_cookie_text(text: str, source: str) -> str:
    global COOKIES
    rows = _normalize_cookie_text(text)
    content = "# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n"
    signature = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if signature != _COOKIE_STATE["signature"]:
        tmp = f"{_COOKIE_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, _COOKIE_PATH)
        _COOKIE_STATE.update(signature=signature, count=len(rows), source=source)
        log(f"loaded {len(rows)} validated cookie entries from {source}")
    COOKIES = _COOKIE_PATH
    return COOKIES


def _remote_cookie_text(url: str) -> str:
    with _open(url, {"Accept": "text/plain,*/*"}, timeout=10) as response:
        return response.read(2_000_000).decode("utf-8-sig", "replace")


def refresh_cookies(force: bool = False) -> str | None:
    """Load cookies from env/URL/file and hot-reload when they change.

    Sources, in priority order: COOKIES_BASE64, COOKIES_TXT, COOKIES_URL
    (re-downloaded every COOKIES_REFRESH seconds), then ./cookies.txt.
    A bad or expired export never clears the last good cookie file.
    """
    global COOKIES
    with _COOKIE_LOCK:
        try:
            b64 = os.environ.get("COOKIES_BASE64", "").strip()
            if b64:
                return _store_cookie_text(_decode_cookie_base64(b64), "COOKIES_BASE64")

            txt = os.environ.get("COOKIES_TXT", "")
            if txt.strip():
                return _store_cookie_text(txt.replace("\\n", "\n").replace("\\t", "\t"),
                                          "COOKIES_TXT")

            url = os.environ.get("COOKIES_URL", "").strip()
            if url:
                if force or time.time() - _COOKIE_STATE["remote_at"] >= _COOKIE_REFRESH:
                    _COOKIE_STATE["remote_at"] = time.time()
                    try:
                        return _store_cookie_text(_remote_cookie_text(url), "COOKIES_URL")
                    except Exception as e:
                        log("COOKIES_URL refresh failed (keeping last good cookies):", str(e)[:140])
                if COOKIES:
                    # Remote is the source of truth once loaded; a stale local
                    # cookies.txt must not replace it.
                    return COOKIES

            custom_file = os.environ.get("COOKIES_FILE", "").strip()
            src = custom_file or os.path.join(ROOT, "cookies.txt")
            try:
                stat = os.stat(src)
            except OSError:
                return COOKIES
            file_sig = (stat.st_mtime_ns, stat.st_size)
            if force or file_sig != _COOKIE_STATE["file_sig"]:
                _COOKIE_STATE["file_sig"] = file_sig
                with open(src, "r", encoding="utf-8-sig") as handle:
                    return _store_cookie_text(handle.read(), "COOKIES_FILE" if custom_file else "cookies.txt")
        except Exception as e:
            log("cookies not loaded:", str(e)[:160])
        return COOKIES


COOKIES = refresh_cookies(force=True)


def _browser_cookie_specs() -> list[tuple[str, str | None, None, None]]:
    """Find existing logged-in browser profiles; this reads sessions, it cannot create logins."""
    setting = os.environ.get("COOKIES_FROM_BROWSER", "auto").strip()
    known = {
        "chrome": os.path.expanduser("~/.config/google-chrome"),
        "chromium": os.path.expanduser("~/.config/chromium"),
        "firefox": os.path.expanduser("~/.mozilla/firefox"),
        "edge": os.path.expanduser("~/.config/microsoft-edge"),
        "brave": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
        "opera": os.path.expanduser("~/.config/opera"),
        "vivaldi": os.path.expanduser("~/.config/vivaldi"),
    }
    specs = []
    if not setting or setting.lower() in {"0", "false", "off", "none"}:
        return specs
    entries = list(known) if setting.lower() == "auto" else [x.strip() for x in setting.split(",") if x.strip()]
    for entry in entries:
        browser, sep, profile = entry.partition(":")
        browser = browser.lower()
        if browser not in known:
            log("ignoring unsupported COOKIES_FROM_BROWSER entry:", browser)
            continue
        if setting.lower() == "auto" and not os.path.isdir(known[browser]):
            continue
        specs.append((browser, profile or None, None, None))
    return specs


def _ydl_opts(cookies: bool = True, clients: list[str] | None = None,
              browser: tuple[str, str | None, None, None] | None = None,
              proxy: str | None = None) -> dict:
    o = {
        "format": "bestaudio[acodec!=none]/best[acodec!=none]/best",
        "format_sort": ["abr", "asr"],
        "js_runtimes": {"deno": {}, "node": {}},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 8,
        "retries": 1,
        "extractor_retries": 1,
        "extractor_args": {
            "youtube": {"player_client": ["tv_embedded", "web_safari", "default"]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        },
    }
    if cookies and COOKIES:
        o["cookiefile"] = COOKIES
    elif browser:
        o["cookiesfrombrowser"] = browser
    if proxy:
        o["proxy"] = proxy
    if clients:
        o["extractor_args"]["youtube"] = {"player_client": clients}
    return o


@contextmanager
def _cookie_copy(path: str):
    """Give yt-dlp a throwaway cookie jar.

    yt-dlp rewrites the file it is given. Sharing one path between concurrent
    searches can corrupt it, and writing back can also drop cookies YouTube
    refreshed. Each extraction therefore gets its own copy.
    """
    fd, tmp = tempfile.mkstemp(prefix="ytcookies-", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        yield tmp
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _extract_entries(opts: dict, target: str) -> list[dict]:
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise NotFound("no results")
    entries = [e for e in (info.get("entries") or [info]) if e]
    if not entries:
        raise NotFound("no results")
    return entries


def _ydl_entries(opts: dict, target: str) -> list[dict]:
    cookiefile = opts.get("cookiefile")
    if cookiefile and os.path.exists(cookiefile):
        with _cookie_copy(cookiefile) as tmp:
            return _extract_entries({**opts, "cookiefile": tmp}, target)
    return _extract_entries(opts, target)


def _thumb_candidates(info: dict, source: str = "") -> list[str]:
    """Highest-resolution artwork first; maxres/maxres1-3 are 1280x720 HD stills."""
    urls: list[str] = []
    if source == "youtube" and info.get("id"):
        # maxresdefault is missing on some videos; maxres1/2/3 are still 1280x720.
        for name in ("maxresdefault", "maxres1", "maxres2", "maxres3",
                     "sddefault", "hqdefault"):
            urls.append(f"https://i.ytimg.com/vi/{info['id']}/{name}.jpg")
    for thumb in sorted(info.get("thumbnails") or [],
                        key=lambda x: x.get("width") or 0, reverse=True):
        if thumb.get("url"):
            urls.append(thumb["url"])
    if info.get("thumbnail"):
        urls.append(info["thumbnail"])
    return list(dict.fromkeys(url for url in urls if url))


def _norm(info: dict, source: str) -> dict:
    return {
        "title": info.get("title") or "Unknown",
        "url": info.get("url"),
        "page": info.get("webpage_url") or info.get("original_url") or "",
        "duration": int(info.get("duration") or 0),
        "artist": info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "",
        "headers": info.get("http_headers") or {},
        "abr": info.get("abr"),
        "asr": info.get("asr"),
        "acodec": info.get("acodec"),
        "vcodec": info.get("vcodec"),
        "format_id": info.get("format_id"),
        "height": info.get("height"),
        "thumbnails": _thumb_candidates(info, source),
        "official": bool(info.get("official")),
        "source": source,
    }


# ───────────────────────── sources ─────────────────────────
# Tried in order until YouTube returns a playable audio format. Cookie-less
# clients come first: an expired export must never block ordinary songs.
YT_ATTEMPTS = [
    {"clients": ["default"], "cookies": False},                              # best audio, no cookies
    {"clients": ["default"], "cookies": True},                               # signed-in (age/region)
    {"clients": ["tv", "web_safari", "mweb", "android_vr"], "cookies": False},  # PO-token provider
    {"clients": ["tv", "web_safari", "mweb", "android_vr"], "cookies": True},   # PO token + cookies
    {"clients": ["tv_embedded", "tv_samsung", "visionos"], "cookies": False},  # explicit high-quality clients
    {"clients": ["tv_embedded", "tv_samsung", "visionos"], "cookies": True},
    {"clients": ["tv_simply", "mweb", "android_vr"], "cookies": False,       # last resort (muxed, lower bitrate)
     "low_quality": True},
]

# Remember the last client strategy that produced a high-quality stream so the
# next song starts immediately instead of re-walking failed attempts.
_LAST_GOOD: dict | None = None


_YT_CACHE: dict[str, tuple[float, dict]] = {}
_YT_CACHE_TTL = 1800  # stream URLs stay valid for hours; 30 min keeps repeats instant


def _is_bot_check(e: Exception) -> bool:
    m = str(e).lower()
    return "not a bot" in m or "sign in to confirm" in m


def _is_cookie_error(e: Exception) -> bool:
    """Errors that usually disappear when the cookie file is not sent at all."""
    m = str(e).lower()
    return any(token in m for token in (
        "cookie", "login required", "requires login", "please sign in",
        "page needs to be reloaded", "account cookies", "precondition check failed",
    ))


def _is_unavailable(e: Exception) -> bool:
    """Permanent per-video errors: another client cannot fix these."""
    m = str(e).lower()
    return any(token in m for token in (
        "video unavailable", "this video is not available", "private video",
        "video has been removed", "removed by the uploader", "no longer available",
    ))


def _yt_attempts() -> list[dict]:
    # Cookie attempts are pointless without a loaded cookie file; skipping them
    # keeps a stale export from costing extra round-trips.
    attempts = [a for a in YT_ATTEMPTS if not a.get("cookies") or COOKIES]
    # If ordinary extraction is challenged, try importing an already-authenticated
    # browser profile. This cannot create a Google login or bypass verification.
    for browser_spec in _browser_cookie_specs():
        attempts.append({"browser": browser_spec, "cookies": False})
    if _LAST_GOOD is not None:
        for i, a in enumerate(attempts):
            if a == _LAST_GOOD:
                attempts.insert(0, attempts.pop(i))
                break
    return attempts


def _note_quality(attempt: dict, info: dict) -> None:
    global _LAST_GOOD
    if not attempt.get("low_quality"):
        _LAST_GOOD = attempt
    try:
        rate = f"{float(info.get('abr')):.0f} kbps" if info.get("abr") else "bitrate unknown"
    except (TypeError, ValueError):
        rate = "bitrate unknown"
    log(f"youtube stream: {info.get('format_id') or '?'} {info.get('acodec') or '?'} "
        f"{rate} {info.get('asr') or '?'} Hz")


def _yt_extract(url: str) -> dict:
    """Resolve ONE video url to a stream, trying each client strategy in turn."""
    refresh_cookies()
    last: Exception | None = None
    for a in _yt_attempts():
        allow_low = bool(a.get("low_quality"))
        used_cookies = bool(a.get("cookies", True) and COOKIES)
        try:
            entries = _ydl_entries(
                _ydl_opts(a.get("cookies", True), a.get("clients"), a.get("browser"), YT_PROXY), url
            )
            info = _finish(_norm(entries[0], "youtube"), allow_low_quality=allow_low)
            _note_quality(a, info)
            return info
        except NotFound as e:
            last = e
        except Exception as e:
            last = e
            log("youtube attempt failed:", str(e)[:160])
            if _is_unavailable(e):
                break
            if used_cookies and (_is_bot_check(e) or _is_cookie_error(e)):
                # Same client, but without the possibly expired cookies.
                try:
                    entries = _ydl_entries(
                        _ydl_opts(cookies=False, clients=a.get("clients"), proxy=YT_PROXY), url
                    )
                    info = _finish(_norm(entries[0], "youtube"), allow_low_quality=allow_low)
                    _note_quality(a, info)
                    return info
                except Exception as retry_error:
                    last = retry_error
                    log("youtube retry without cookies failed:", str(retry_error)[:160])
                    if _is_unavailable(retry_error):
                        break
    raise last or RuntimeError("youtube failed")


def _search_entries(target: str) -> list[dict]:
    """Flat search first (fast, no player request), then once with cookies if blocked."""
    opts = _ydl_opts(cookies=False, proxy=YT_PROXY)
    opts["extract_flat"] = True
    try:
        return _ydl_entries(opts, target)
    except Exception as e:
        if not COOKIES:
            raise
        log("youtube search failed without cookies, retrying with cookies:", str(e)[:120])
        retry = _ydl_opts(cookies=True, proxy=YT_PROXY)
        retry["extract_flat"] = True
        return _ydl_entries(retry, target)


_OEMBED_CACHE: dict[str, str] = {}
_OEMBED_LOCK = threading.Lock()


def _oembed_author(vid: str) -> str:
    """Channel name for a video id, without a player request."""
    if not vid:
        return ""
    with _OEMBED_LOCK:
        if vid in _OEMBED_CACHE:
            return _OEMBED_CACHE[vid]
    author = ""
    url = ("https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v="
           f"{urllib.parse.quote(vid)}&format=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            author = (data.get("author_name") or "").strip()
    except Exception:
        author = ""
    with _OEMBED_LOCK:
        _OEMBED_CACHE[vid] = author
    return author


def _enrich_channels(entries: list[dict]) -> None:
    """Flat search sometimes drops the channel; fill it so official detection still works."""
    missing = [
        e for e in entries
        if e.get("id") and not (e.get("channel") or e.get("uploader") or e.get("artist"))
    ][:20]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        authors = list(pool.map(_oembed_author, [e["id"] for e in missing]))
    for e, author in zip(missing, authors):
        if author:
            e["channel"] = author


def _youtube(target: str) -> dict:
    cached = _YT_CACHE.get(target)
    if cached and time.time() - cached[0] < _YT_CACHE_TTL:
        return cached[1]
    refresh_cookies()
    if not COOKIES and not _browser_cookie_specs():
        log("no YouTube cookies loaded; using cookie-less clients "
            "(set COOKIES_URL/COOKIES_BASE64/COOKIES_TXT only for age-restricted videos)")
    m = re.match(r"ytsearch\d*:(.*)", target, re.I)
    if not m:
        # Direct user-provided YouTube links are resolved as requested.
        info = _yt_extract(target)
        _YT_CACHE[target] = (time.time(), info)
        return info

    # Search is a cheap flat listing: it needs no player request, so it is fast.
    # The official-biased search runs in parallel with the plain one, so it does not
    # add latency. Only the chosen candidates go through full stream extraction.
    search_query = m.group(1).strip()

    def channel_of(e):
        return e.get("channel") or e.get("uploader") or e.get("artist") or ""

    with ThreadPoolExecutor(max_workers=2) as pool:
        plain_future = pool.submit(_search_entries, target)
        official_future = None
        if "official" not in search_query.lower():
            official_future = pool.submit(
                _search_entries, f"ytsearch10:{search_query} official audio"
            )
        entries = plain_future.result()
        if official_future is not None:
            try:
                extra = official_future.result()
            except Exception as e:
                log("official search failed:", str(e)[:100])
            else:
                seen = {e.get("id") for e in entries if e.get("id")}
                entries += [e for e in extra if e.get("id") not in seen]

    _enrich_channels(entries)
    for i, e in enumerate(entries):
        e["_rank"] = i

    def score(e):
        return _match_score(
            search_query, e.get("title", ""), channel_of(e),
            e.get("duration") or 0, bool(e.get("channel_is_verified")),
            e.get("view_count") or 0,
        ) + max(0.0, 3.0 - (e.get("_rank") or 0) * 0.2)

    def is_official(e):
        return _is_official(
            e.get("title", ""), channel_of(e), search_query,
            bool(e.get("channel_is_verified")), e.get("view_count") or 0,
        )

    ranked = sorted(entries, key=score, reverse=True)
    official_candidates = []
    for e in ranked:
        if not is_official(e):
            continue
        if _candidate_ok(search_query, e, 0.7):
            official_candidates.append(e)
            continue
        # A Latin query ("tumi robe nirobe") cannot match a Bengali title. Trust
        # YouTube relevance for a known label only near the top of the results.
        if (_candidate_basic_ok(search_query, e)
                and _script_mismatch(search_query, e.get("title", ""))
                and _LABEL_CHANNEL.search(channel_of(e))
                and (e.get("_rank") or 99) < 5):
            official_candidates.append(e)
    # Lyrics uploads usually reuse the official audio; never fall back to arbitrary covers.
    lyrics_candidates = [
        e for e in ranked
        if _VARIANT_SOFT.search(e.get("title", "") or "")
        and _candidate_ok(search_query, e, 0.5, allow_soft=True)
    ]
    candidates: list[dict] = []
    for e in official_candidates[:4] + lyrics_candidates[:2]:
        if e not in candidates:
            candidates.append(e)
    if not candidates:
        raise NotFound("youtube: no official upload found")
    if not official_candidates:
        log("youtube: no official upload found, trying lyrics uploads")
    last: Exception | None = None
    for e in candidates:
        vid = e.get("id")
        url = e.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url"))
        try:
            info = _yt_extract(url)
        except Exception as err:
            last = err
            if _is_bot_check(err):
                raise  # same block applies to every candidate; fail fast
            continue
        chosen_official = is_official(e)
        info["official"] = chosen_official
        if chosen_official:
            info["thumbnails"] = _thumb_candidates(e, "youtube")
        elif official_candidates:
            # Lyrics fallback: keep the official song's artwork, not the lyrics channel's.
            info["thumbnails"] = _thumb_candidates(official_candidates[0], "youtube")
        else:
            info["thumbnails"] = []
        log("youtube pick:", f"{e.get('title', '')[:70]} | {channel_of(e)} | {e.get('view_count') or 0} views | official={chosen_official}")
        _YT_CACHE[target] = (time.time(), info)
        return info
    raise last or NotFound("youtube: no playable result")


def _soundcloud(q: str) -> dict:
    last: Exception | None = None
    entries = _ydl_entries(_ydl_opts(cookies=False), f"scsearch10:{q}")
    candidates = sorted(entries, key=lambda e: _match_score(
        q, e.get("title", ""), e.get("artist") or e.get("uploader", "")), reverse=True)
    for e in candidates:
        if "preview" in f"{e.get('format_note', '')} {e.get('format_id', '')}".lower():
            continue
        info = _norm(e, "soundcloud")
        if not _relevant(q, info["title"], info["artist"]) or _variant_unwanted(q, info["title"], info["artist"]):
            continue
        try:
            return _finish(info)
        except Exception as ex:
            last = ex
    if last:
        raise last
    raise NotFound("soundcloud: no relevant result")


_audius_cache = {"hosts": [], "t": 0.0}


def _audius_hosts() -> list[str]:
    if _audius_cache["hosts"] and time.time() - _audius_cache["t"] < 3600:
        return list(_audius_cache["hosts"])
    hosts: list[str] = []
    try:
        hosts = [h.rstrip("/") for h in get_json("https://api.audius.co").get("data", [])]
        random.shuffle(hosts)
    except Exception as e:
        log("audius host list unavailable:", str(e)[:100])
    hosts = hosts or ["https://discoveryprovider.audius.co"]
    _audius_cache.update(hosts=hosts, t=time.time())
    return list(hosts)


def _audius(q: str) -> dict:
    last: Exception | None = None
    queries = [q]
    normalized = " ".join(t for t in re.findall(r"\w+", q) if t.lower() not in _STOP)
    if normalized and normalized.casefold() != q.casefold():
        queries.append(normalized)
    seen: set[tuple[str, str]] = set()
    for host in _audius_hosts()[:3]:
        for search in queries:
            try:
                d = get_json(
                    f"{host}/v1/tracks/search?query={urllib.parse.quote(search, safe='')}"
                    "&limit=10&app_name=musicbot"
                )
            except Exception as e:
                last = e
                continue
            for t in d.get("data", []):
                track_id = str(t.get("id") or "")
                track_key = (host, track_id)
                artist = (t.get("user") or {}).get("name", "")
                if (not track_id or track_key in seen or t.get("is_streamable") is False
                        or not _relevant(q, t.get("title", ""), artist)
                        or _variant_unwanted(q, t.get("title", ""), artist)):
                    continue
                seen.add(track_key)
                try:
                    return _finish({
                        "title": t.get("title") or "Unknown",
                        "url": f"{host}/v1/tracks/{track_id}/stream?app_name=musicbot",
                        "page": "https://audius.co" + (t.get("permalink") or ""),
                        "duration": int(t.get("duration") or 0),
                        "artist": artist,
                        "headers": {},
                        "source": "audius",
                    })
                except Exception as e:
                    last = e
    raise last or NotFound("audius: no relevant result")


def _jamendo(q: str) -> dict:
    cid = os.environ["JAMENDO_CLIENT_ID"]
    d = get_json("https://api.jamendo.com/v3.0/tracks/?client_id=%s&format=json&limit=5&audioformat=mp32&search=%s"
                 % (cid, urllib.parse.quote(q)))
    for t in d.get("results", []):
        if t.get("audio") and _relevant(q, t.get("name", ""), t.get("artist_name", "")):
            return _finish({"title": t.get("name") or "Unknown", "url": t["audio"], "page": t.get("shareurl", ""),
                            "duration": int(t.get("duration") or 0), "artist": t.get("artist_name", ""),
                            "headers": {}, "source": "jamendo"})
    raise NotFound("jamendo: no relevant result")


# ───────────────────────── Spotify catalogue lookup ─────────────────────────
_spotify_token_cache = {"token": "", "expires": 0.0}


def _spotify_catalog_query(q: str) -> str:
    """Use Spotify Web API for canonical title/artist matching only, never for audio streams."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return q

    try:
        cached = _spotify_token_cache
        if cached["token"] and time.time() < cached["expires"] - 30:
            token = cached["token"]
        else:
            credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            request = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": UA,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                auth = json.loads(response.read(100_000).decode("utf-8", "replace"))
            token = auth["access_token"]
            _spotify_token_cache.update(token=token, expires=time.time() + int(auth.get("expires_in", 3600)))

        params = urllib.parse.urlencode({"q": q, "type": "track", "market": "IN", "limit": 10})
        request = urllib.request.Request(
            f"https://api.spotify.com/v1/search?{params}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8", "replace"))

        tracks = (data.get("tracks") or {}).get("items") or []
        candidates = []
        for track in tracks:
            name = track.get("name") or ""
            artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
            if (not name or not _relevant(q, name, artists)
                    or _variant_unwanted(q, name, artists)):
                continue
            candidates.append((_match_score(q, name, artists), name, artists))
        if not candidates:
            return q
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, name, artists = candidates[0]
        canonical = f"{name} {artists}".strip()
        log(f"Spotify catalogue match: {canonical}")
        return canonical
    except Exception as e:
        log("Spotify catalogue lookup unavailable; using original query:", str(e)[:140])
        return q


# ───────────────────────── registry ─────────────────────────
class Source:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn
        self.fails = 0
        self.dead_until = 0.0

    @property
    def alive(self) -> bool:
        return time.time() >= self.dead_until

    def good(self):
        self.fails, self.dead_until = 0, 0.0

    def bad(self):
        self.fails += 1
        self.dead_until = time.time() + min(30 * 2 ** (self.fails - 1), 600)  # 30s, 60s, ... max 10 min


def _build() -> list[Source]:
    # YouTube is the primary playback resolver by default. Set ENABLE_YOUTUBE=0 to disable it.
    s = []
    # Spotify is used only to identify the canonical track; it does not provide audio streams.
    youtube_enabled = os.environ.get("ENABLE_YOUTUBE", "1").strip().lower() in {"1", "true", "yes", "on"}
    if youtube_enabled:
        s.append(Source("youtube", lambda q: _youtube(f"ytsearch15:{q}")))
    # Alternate user uploads are disabled by default: they often return covers,
    # edits, or low-quality copies. Enable only as an explicit last-resort choice.
    if os.environ.get("ENABLE_ALTERNATE_SOURCES", "").strip().lower() in {"1", "true", "yes", "on"}:
        s.extend([Source("audius", _audius), Source("soundcloud", _soundcloud)])
        if os.environ.get("JAMENDO_CLIENT_ID"):
            s.append(Source("jamendo", _jamendo))
    if not s:
        raise RuntimeError("No music sources enabled")
    order = [x.strip() for x in os.environ.get("SOURCE_ORDER", "").split(",") if x.strip()]
    if order:
        by = {x.name: x for x in s}
        s = [by[n] for n in order if n in by] or s
    return s


SOURCES = _build()
_RESOLVER_POOL = ThreadPoolExecutor(max_workers=max(1, len(SOURCES)))


def _try_source(source: Source, query: str):
    try:
        info = source.fn(query)
        source.good()
        return info, None
    except NotFound as e:
        return None, e
    except Exception as e:
        source.bad()
        log(f"{source.name} failed (cooldown {int(source.dead_until - time.time())}s):",
            str(e)[:160])
        return None, e


# ───────────────────────── URLs ─────────────────────────
def _url_kind(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "soundcloud.com" in host:
        return "soundcloud"
    if "spotify.com" in host:
        return "spotify"
    return "direct"


def _title_hint(url: str) -> str | None:
    """Name of the song behind a link, so another source can search for it."""
    try:
        kind = _url_kind(url)
        if kind == "youtube":
            m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", url)
            if m:
                d = get_json("https://www.youtube.com/oembed?format=json&url="
                             + urllib.parse.quote(f"https://www.youtube.com/watch?v={m.group(1)}", safe=""))
                return d.get("title")
        elif kind == "spotify":
            with _open(url) as r:
                page = r.read(400_000).decode("utf-8", "replace")
            m = re.search(r"<title>(.*?)</title>", page, re.S)
            if m:
                t = re.sub(r"\s*\|\s*Spotify.*$", "", m.group(1).strip())
                t = re.sub(r"\s*-\s*(song|single|album)?\s*(and lyrics)?\s*by\s*", " ", t, flags=re.I)
                return t.strip() or None
    except Exception as e:
        log("title lookup failed:", str(e)[:100])
    return None


def _direct(url: str, kind: str) -> dict:
    if kind == "youtube":
        return _youtube(url)
    entries = _ydl_entries(_ydl_opts(cookies=False), url)
    return _finish(_norm(entries[0], kind), allow_low_quality=True)


# ───────────────────────── main entry ─────────────────────────
def resolve(query: str, skip=()) -> dict:
    """Returns {title,url,page,duration,artist,headers,source}. `skip` = source names to avoid."""
    skip = set(skip)
    query = query.strip()
    t0 = time.time()
    errors: list[str] = []

    if URL_RE.match(query):
        kind = _url_kind(query)
        if kind not in skip and kind != "spotify":
            try:
                return _direct(query, kind)
            except Exception as e:
                errors.append(f"{kind}: {str(e)[:100]}")
                log("direct link failed:", str(e)[:160])
        hint = _title_hint(query)
        if not hint:
            raise Unavailable(errors or ["could not read that link"])
        log("link failed/unsupported, searching other sources for:", hint)
        query = hint

    query = _spotify_catalog_query(query)

    ordered = [s for s in SOURCES if s.name not in skip]
    healthy = [s for s in ordered if s.alive]
    cooling = [s for s in ordered if not s.alive]

    def run_group(group: list[Source]) -> dict | None:
        futures: dict[Future, Source] = {
            _RESOLVER_POOL.submit(_try_source, s, query): s for s in group
        }
        source_order = {s: i for i, s in enumerate(group)}
        while futures:
            remaining = DEADLINE - (time.time() - t0)
            if remaining <= 0:
                errors.append("timeout")
                break
            done, _ = wait(futures, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                errors.append("timeout")
                break
            for future in sorted(done, key=lambda f: source_order[futures[f]]):
                source = futures.pop(future)
                info, error = future.result()
                if error is None:
                    log(f"'{query[:50]}' -> {source.name} "
                        f"({info.get('abr') or '?'} kbps, {info.get('asr') or '?'} Hz)")
                    for pending in futures:
                        pending.cancel()
                    return info
                if isinstance(error, NotFound):
                    errors.append(str(error))
                else:
                    errors.append(f"{source.name}: {str(error)[:100]}")
        for future in futures:
            future.cancel()
        return None

    info = run_group(healthy)
    if info:
        return info
    if time.time() - t0 <= DEADLINE:
        info = run_group(cooling)
        if info:
            return info
    raise Unavailable(errors)
