"""Multi-source song resolver.

YouTube is the primary search/playback resolver, followed by Audius and SoundCloud.
Configured healthy sources are searched concurrently and the first playable result is returned.
Each candidate is checked for relevance and unwanted alternate versions before playback. Sources
that fail are temporarily cooled down and tried after healthy sources.

Env options (all optional):
  SOURCE_ORDER      e.g. "youtube,audius,soundcloud"   (subset / tie-break preference)
  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET  optional Spotify catalog lookup credentials
  JAMENDO_CLIENT_ID free Jamendo key (enables the "jamendo" source)
  COOKIES_TXT / COOKIES_BASE64   optional YouTube cookies (default: cookies.txt next to this file)\n  COOKIES_FROM_BROWSER           auto-detect an existing logged-in browser profile, or set chrome[:profile],firefox,etc.
  ENABLE_YOUTUBE                  set to 0 to disable YouTube (enabled by default)
  RESOLVE_TIMEOUT   max seconds spent on one search (default 55)
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from yt_dlp import YoutubeDL

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 8
DEADLINE = int(os.environ.get("RESOLVE_TIMEOUT", "55"))
URL_RE = re.compile(r"^https?://", re.I)


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
def _open(url: str, headers: dict | None = None, timeout: float = HTTP_TIMEOUT):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def get_json(url: str, timeout: float = HTTP_TIMEOUT):
    with _open(url, {"Accept": "application/json"}, timeout) as r:
        return json.loads(r.read(3_000_000).decode("utf-8", "replace"))


def playable(url: str, headers: dict | None = None) -> bool:
    """Verify that a URL returns readable media bytes, not just an HTTP response."""
    h = dict(headers or {})
    h.pop("Accept-Encoding", None)
    h["Range"] = "bytes=0-4095"
    try:
        with _open(url, h) as r:
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


def _finish(info: dict) -> dict:
    if not info.get("url"):
        raise RuntimeError("no stream url")
    # Reject formats explicitly identified as low quality. Unknown metadata is
    # allowed because some providers do not publish bitrate/sample-rate fields.
    try:
        abr = float(info.get("abr") or 0)
        if abr and abr < 128:
            raise RuntimeError(f"{info.get('source')}: audio bitrate below 128 kbps ({abr:.0f} kbps)")
        asr = int(info.get("asr") or 0)
        if asr and asr < 32000:
            raise RuntimeError(f"{info.get('source')}: sample rate too low ({asr} Hz)")
    except (TypeError, ValueError):
        pass
    headers = info.get("headers") or {}
    if not playable(info["url"], headers):
        raise RuntimeError(f"{info.get('source')}: stream not reachable")
    # MediaStream expects a mapping of individual HTTP header names to values.
    # Keep them separate: folding Referer/Accept into User-Agent creates one invalid header.
    return info


_STOP = {"official", "video", "audio", "lyrics", "lyric", "song", "full", "new", "hd", "the", "a", "of", "ft", "feat", "and", "by", "original", "version"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\w+", (s or "").lower()) if t not in _STOP}


def _relevant(query: str, title: str, artist: str = "") -> bool:
    """Fail closed on weak matches: title + artist must identify the requested recording."""
    q = _tokens(query)
    if not q:
        return False
    have = _tokens(f"{title} {artist}")
    return len(q & have) / len(q) >= 0.75


_VARIANT = re.compile(
    r"\b(?:mix(?:es)?|remix(?:es)?|mashup|medley|tiktok|reprised?|"
    r"female\s+version|male\s+version|acoustic(?:\s+version)?|piano\s+version|"
    r"lo[ -]?fi|fan[ -]?made|bootleg|rework|flip|sped\s*up|slowed(?:\s*\+\s*reverb)?|"
    r"nightcore|8d|karaoke|cover|live|instrumental|bass\s*boosted|extended|dj\s*set|"
    r"lyrics?\s*video)\b", re.I
)


def _variant_unwanted(query: str, title: str, artist: str = "") -> bool:
    """Reject alternate edits named in either the title or the publishing channel."""
    if _VARIANT.search(query):
        return False
    return bool(_VARIANT.search(f"{title} {artist}"))


def _match_score(query: str, title: str, artist: str = "") -> float:
    q = _tokens(query)
    t = _tokens(title)
    a = _tokens(artist)
    if not q:
        return 0.0
    overlap = len(q & (t | a)) / len(q)
    title_overlap = len(q & t) / len(q)
    score = overlap * 5 + title_overlap * 2
    if q == t:
        score += 5
    if _variant_unwanted(query, title, artist):
        score -= 20
    return score


def _validate_cookie_domains(data: str) -> int:
    """Validate Netscape-format cookies and reject unrelated service credentials."""
    allowed = {
        "google.com", "www.google.com", "accounts.google.com", "ogs.google.com",
        "youtube.com", "www.youtube.com", "m.youtube.com",
    }
    found = 0
    for line_no, raw in enumerate(data.splitlines(), 1):
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        # Netscape cookie files are tab-delimited. Splitting on the literal
        # characters backslash+t rejects every otherwise-valid exported row.
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"invalid Netscape cookie row at line {line_no}")
        domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if domain not in allowed:
            raise ValueError(f"cookie export contains an unrelated domain at line {line_no}")
        found += 1
    if not found:
        raise ValueError("cookie export contains no supported Google/YouTube cookies")
    return found


def _prepare_cookies() -> str | None:
    """yt-dlp rewrites its cookie file, so work on a writable copy."""
    dst = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    try:
        b64 = os.environ.get("COOKIES_BASE64", "").strip()
        txt = os.environ.get("COOKIES_TXT", "")
        if b64:
            # Reject malformed Base64 instead of silently accepting a truncated export.
            data = base64.b64decode(b64, validate=True).decode("utf-8")
        elif txt.strip():
            data = txt.replace("\\n", "\n")
        else:
            src = os.path.join(ROOT, "cookies.txt")
            if not os.path.exists(src):
                return None
            with open(src, "r", encoding="utf-8") as f:
                data = f.read()
        cookie_count = _validate_cookie_domains(data)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(data)
        source = "COOKIES_BASE64" if b64 else ("COOKIES_TXT" if txt.strip() else "cookies.txt")
        log(f"loaded {cookie_count} validated cookie entries from {source}")
        return dst
    except Exception as e:
        log("cookies not loaded:", e)
        return None


COOKIES = _prepare_cookies()


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
              browser: tuple[str, str | None, None, None] | None = None) -> dict:
    o = {
        "format": "bestaudio[acodec!=none]/best[acodec!=none]/best",
        "js_runtimes": {"deno": {}, "node": {}},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
    }
    if cookies and COOKIES:
        o["cookiefile"] = COOKIES
    elif browser:
        o["cookiesfrombrowser"] = browser
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    return o


def _ydl_entries(opts: dict, target: str) -> list[dict]:
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        raise NotFound("no results")
    entries = [e for e in (info.get("entries") or [info]) if e]
    if not entries:
        raise NotFound("no results")
    return entries


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
        "source": source,
    }


# ───────────────────────── sources ─────────────────────────
# Tried in order until YouTube returns a playable audio format
YT_ATTEMPTS = [
    {},                                                 # default clients + cookies
    {"clients": ["tv", "web_safari"]},                  # with cookies
    {"clients": ["android_vr"], "cookies": False},      # no cookies, no JS needed
    {"clients": ["mweb"], "cookies": False},
]


_YT_CACHE: dict[str, tuple[float, dict]] = {}
_YT_CACHE_TTL = 1800  # stream URLs stay valid for hours; 30 min keeps repeats instant


def _is_bot_check(e: Exception) -> bool:
    m = str(e).lower()
    return "not a bot" in m or "sign in to confirm" in m


def _yt_attempts() -> list[dict]:
    attempts = list(YT_ATTEMPTS)
    # If ordinary extraction is challenged, try importing an already-authenticated
    # browser profile. This cannot create a Google login or bypass verification.
    for browser_spec in _browser_cookie_specs():
        attempts.append({"browser": browser_spec, "cookies": False})
    return attempts


def _yt_extract(url: str) -> dict:
    """Resolve ONE video url to a stream, trying each client strategy in turn."""
    last: Exception | None = None
    for a in _yt_attempts():
        try:
            entries = _ydl_entries(
                _ydl_opts(a.get("cookies", True), a.get("clients"), a.get("browser")), url
            )
            return _finish(_norm(entries[0], "youtube"))
        except NotFound as e:
            last = e
        except Exception as e:
            last = e
            log("youtube attempt failed:", str(e)[:160])
    raise last or RuntimeError("youtube failed")


def _youtube(target: str) -> dict:
    cached = _YT_CACHE.get(target)
    if cached and time.time() - cached[0] < _YT_CACHE_TTL:
        return cached[1]
    if not COOKIES and not _browser_cookie_specs():
        log("no YouTube cookie file or logged-in browser profile found; "
            "set COOKIES_BASE64/COOKIES_TXT or provide an authenticated browser profile")
    m = re.match(r"ytsearch\d*:(.*)", target, re.I)
    if not m:
        # Direct user-provided YouTube links are resolved as requested.
        info = _yt_extract(target)
        _YT_CACHE[target] = (time.time(), info)
        return info

    # Search is a cheap flat listing: it needs no player request, so it is fast
    # and is not hit by the "confirm you're not a bot" check. Only the chosen
    # candidates go through full stream extraction (previously all 10 did).
    search_query = m.group(1).strip()
    opts = _ydl_opts(cookies=False)
    opts["extract_flat"] = True
    entries = _ydl_entries(opts, target)

    def artist_of(e):
        return e.get("artist") or e.get("uploader") or e.get("channel") or ""

    ranked = sorted(entries, key=lambda e: _match_score(search_query, e.get("title", ""), artist_of(e)),
                    reverse=True)
    candidates = [
        e for e in ranked
        if _relevant(search_query, e.get("title", ""), artist_of(e))
        and not _variant_unwanted(search_query, e.get("title", ""), artist_of(e))
        and _match_score(search_query, e.get("title", ""), artist_of(e)) >= 5.0
    ][:3]
    if not candidates:
        raise NotFound("youtube: no acceptable search result")
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
        s.append(Source("youtube", lambda q: _youtube(f"ytsearch10:{q}")))
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
    return _finish(_norm(entries[0], kind))


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
                    log(f"'{query[:50]}' -> {source.name}")
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
