# Music bot – GitHub + Railway ready

## 1. Put it on GitHub
Upload every file in this folder to a new repo (public or private both work – no secrets are inside).
`.env` and `cookies.txt` are in `.gitignore`, so they never get uploaded.

## 2. Deploy on Railway
1. Railway → **New Project → Deploy from GitHub repo** → pick the repo. Railway finds the `Dockerfile` by itself.
2. Open the service → **Variables → Raw Editor** and paste:

```
API_ID=...
API_HASH=...
BOT_TOKEN=...
SESSION_STRING=...
SAAVN_API=https://your-jiosaavn-api.example.com/api
COOKIES_URL=... (optional; auto-refreshed cookie export URL)
COOKIES_REFRESH=21600 (optional; seconds between COOKIES_URL refreshes)
COOKIES_FROM_BROWSER=auto (optional; imports an existing browser profile when available)
ENABLE_ALTERNATE_SOURCES=1 (optional; allows Audius/SoundCloud fallback)
MIN_AUDIO_KBPS=120 (optional; reject lower-bitrate formats)
VERIFY_YOUTUBE=0 (optional; keep 0, YouTube URLs are IP-bound)
AUDIO_QUALITY=high (optional; high = 48 kHz stereo, medium = 36 kHz mono fallback)
AUDIO_NORMALIZE=1 (optional; EBU R128 loudness normalization, 0 to disable)
```
3. Railway redeploys. Open **Deployments → View logs**; you should see `starting bot.py`.

YouTube works without cookies for ordinary songs. The resolver tries cookie-less high-quality
clients first (`default`, then `tv`/`web_safari` with the PO-token provider, then `tv_samsung`/
`visionos`/`tv_embedded`, and finally lower-bitrate clients). This means an expired cookie export
can no longer stop normal playback, so you do not have to replace `cookies.txt` every time it
expires. Cookies are only used for videos that genuinely require a signed-in account.

Cookie handling is resilient:
- `COOKIES_BASE64`, `COOKIES_TXT`, `COOKIES_URL`, `COOKIES_FILE`, or a local `cookies.txt` are all accepted.
- Netscape exports, JSON exports, and a raw `Cookie: a=b; c=d` header are understood.
- Rows for unrelated sites are dropped instead of rejecting the whole export, and
  `#HttpOnly_` Google/YouTube rows are preserved.
- `COOKIES_URL` is re-downloaded every `COOKIES_REFRESH` seconds, so update the hosted file once
  and the bot picks it up without a redeploy.
- `cookies.txt` (or `COOKIES_FILE`) is hot-reloaded when its modification time changes, so a new
  export on a Railway volume (`COOKIES_FILE=/data/cookies.txt`) is picked up without a restart.
- Each extraction gets a throwaway copy, so yt-dlp can never corrupt the shared cookie file.
- A bad or expired export keeps the last good cookie file instead of clearing it.

Audio quality and start speed:
- Playback uses 48 kHz stereo, which is what Telegram voice chats use. The old 96 kHz
  "studio" mode caused resampling underruns (crackling) on small containers.
- `AUDIO_NORMALIZE=1` (default) applies EBU R128 loudness normalization, so one song is not
  much quieter/louder than the next and peaks do not clip. Set `0` to disable.
- Search is typo-tolerant (for example, `believr` still finds `Believer`).
- Official uploads are detected from verified channels, known labels (T-Series, Sony Music,
  Zee Music, Tips, YRF, Saregama, etc.), Topic/VEVO, and the artist's own channel. A title that
  merely says "official" is not trusted, because re-uploads use that word too.
- Among official uploads, the popular version wins (view count), so a random re-upload cannot
  outrank the real song.
- Remixes, TikTok edits, slowed/reverb, sped-up, covers, live, 8D, karaoke and similar variants
  are skipped unless you type that keyword yourself. Lyrics uploads are used only when no
  official upload is available; arbitrary covers are never used as a fallback.
- Audio-only formats are preferred (`bestaudio`, sorted by bitrate/sample rate); YouTube's muxed
  360p format 18 is accepted only as a last resort.
- `MIN_AUDIO_KBPS` (default 120) rejects clearly low-bitrate streams before they are played.
- The last client strategy that produced a good stream is remembered and tried first, so the next
  song starts faster; repeated queries are cached for 30 minutes.
- `VERIFY_YOUTUBE=0` (default) skips the extra stream probe. YouTube URLs are bound to the IP that
  resolved them, so probing them separately can fail and force a slow, low-quality fallback. Set
  `VERIFY_YOUTUBE=1` only if you need dead-link detection before playback.
- The log line `youtube pick:` shows which title/channel/view count was actually selected.

It can also try importing cookies from an already logged-in browser profile
(`COOKIES_FROM_BROWSER=auto`, or e.g. `chrome:Default`). This only imports an existing session; it
cannot create a Google login or manufacture valid cookies. Railway containers normally have no
logged-in browser profile. Never commit or share cookie contents.

The Docker image installs the Rust bgutil PO-token provider and its matching yt-dlp plugin. `main.py`
starts the provider's HTTP server on `127.0.0.1:4416` in the same container, and yt-dlp requests
tokens from it. No second Railway service is required, and cookies remain optional. PO tokens may
help with some YouTube bot checks but do not guarantee access.

On Railway, YouTube sometimes blocks datacenter IPs even with valid cookies. If that happens:
- use `COOKIES_URL` so the bot can refresh cookies without a redeploy, and/or
- set `YT_PROXY` to a residential proxy (stream links are then fetched through the same proxy), and/or
- set `ENABLE_ALTERNATE_SOURCES=1` to let searches fall back to Audius and SoundCloud.

These services have different catalogs and may not have every track. You can also set
`ENABLE_YOUTUBE=0` to stop using YouTube entirely. There is still no way for the bot to create a
Google login by itself; `COOKIES_URL` refreshes a file you control, it does not generate cookies.

Variables set in Railway always win over a local `.env`.

## Run locally
Copy `.env.example` to `.env`, fill it in, then `python main.py`.

## How it stays alive
- `main.py` answers Railway's health check on `$PORT` and restarts `bot.py` forever if it exits.
- A watchdog restarts the bot if Telegram stops answering for ~3 minutes.
- A song that can't be played is skipped; a dead stream is retried from another source.
- yt-dlp is installed from `requirements.txt` when the environment is set up.

## Music sources
YouTube is the primary resolver, with optional alternate providers. Configured healthy providers are
searched concurrently; the first playable result starts playback. Providers on cooldown are only
tried after healthy providers fail. `SOURCE_ORDER` selects providers and breaks ties between results.
Spotify credentials can optionally improve canonical title/artist matching; Spotify does not provide
audio streams.

## Commands (Musicxdy-only names)

All group commands use the `/mx` prefix so they are less likely to collide with commands from other bots:

- `/mxstartplay <song or link>` — start playback (or add to queue)
- `/mxplay <song or link>` — alias for start/play
- `/mxstopplay` — stop playback and clear the queue
- `/mxplayforce <song or link>` — replace the current track and clear the queue after finding the song
- `/mxpause`, `/mxresume`, `/mxskip`, `/mxprevious`
- `/mxseek <seconds|MM:SS|HH:MM:SS>` — seek in the current song
- `/mxqueue`, `/mxshuffle`, `/mxloop`
- `/mxtestaudio` — test Telegram voice-chat audio

Commands are handled by the bot but aren't added to Telegram's command menu automatically; add them manually if you want them listed there. The old generic commands (`/play`, `/stop`, etc.) are not handled. Inline player buttons continue to work.

The bot can only play streams a provider makes publicly available to it. It cannot guarantee every
song or subscriber/premium quality, and it does not bypass login, DRM, or provider bot restrictions.
YouTube cookies do not guarantee access from Railway. For a complete licensed catalog, connect a
provider that grants your deployment playback rights.

## Files
bot.py · sources.py · main.py · config.py · Dockerfile · railway.json · Procfile · requirements.txt · .env.example
