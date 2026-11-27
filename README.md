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
AUDIO_QUALITY=high (optional; high = 48 kHz stereo, medium = 36 kHz mono fallback)
AUDIO_NORMALIZE=1 (optional; EBU R128 loudness normalization, 0 to disable)
```
3. Railway redeploys. Open **Deployments → View logs**; you should see `starting bot.py`.

> **Only one service may run the bot.** If this repo is connected to more than one Railway
> project/service, two bot copies join the same voice chat and replace each other's stream. The
> song then starts, cuts after a few seconds, and restarts. `main.py` defaults to
> `BOT_SERVICE_NAME=web`; any other service stays up only for the health check. Set
> `BOT_SERVICE_NAME` to the service that should run the bot, or delete/stop the duplicate service.

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
  Zee Music, Tips, YRF, Saregama, SVF, Surinder, Sarthak, Tarang, Speed Records, Humble Music,
  Fueled by Ramen, Big Machine, etc.), Topic/VEVO, and the artist's own channel. A title that
  merely says "official" is not trusted, because re-uploads use that word too.
- Among official uploads, the popular version wins (view count), so a random re-upload cannot
  outrank the real song.
- Language hints ("odia", "bengali", "punjabi", ...) are removed from title matching but bias
  ranking towards that language's labels and script. A Latin query can also match a native-script
  official title from a known label near the top of the results.
- Remixes, TikTok edits, slowed/reverb, sped-up, covers, live, 8D, karaoke and similar variants
  are skipped unless you type that keyword yourself. Native-script variants (কভার, ਕਵਰ, କଭର,
  கவர், कवर, etc.) are filtered too. Lyrics uploads are used only when no official upload is
  available; arbitrary covers are never used as a fallback.
- Audio-only formats are preferred (`bestaudio`, sorted by bitrate/sample rate); YouTube's muxed
  360p format 18 is accepted only as a last resort.
- The start watchdog is off by default. It used to replace a stream after a few seconds when
  PyTgCalls briefly reported IDLE, which made songs restart from the beginning. Set
  `START_WATCHDOG=1` only if you want automatic retries.
- The last client strategy that produced a good stream is remembered and tried first, so the next
  song starts faster; repeated queries are cached for 30 minutes.
- YouTube stream URLs are always probed before playback. A URL that resolves but returns 403 to
  ffmpeg is rejected and the next source is tried, instead of starting a dead stream.
- The log line `youtube pick:` shows which title/channel/view count was selected and whether it
  was official.

Now-playing card:
- The official upload's HD artwork (`maxresdefault`, then `maxres1/2/3`, then sd/hq fallbacks)
  is sent as a photo. The caption is the player, and the 2×3 buttons sit directly below it in the
  same bubble.
- Caption layout: `🎵 title`, blank line, player state, blank line, `👤 requester`. There is no
  separate channel/artist line, so the card stays compact.
- Playing shows the progress bar; paused, playback-ended and completed show a status line instead.
- Buttons are colour-coded by row: green for Prev/Pause, blue for Skip/Shuffle, red for Loop/Stop.
- After playback ends, completes or is stopped, the buttons are removed and only the photo plus the
  final status text remain.
- Every player button and control command also posts a short English attribution message with a
  clickable display-name mention, for example `👤 @name skipped the song.` or
  `👤 @name turned loop on.` If the bot receives a sparse user object, it resolves the name through
  the assistant session before posting.
- Title and requester are clipped to one line each, so long names cannot push the card out of shape.
- If a lyrics upload is the only fallback, the card still uses the official song's artwork.
- If Telegram cannot fetch a photo, the bot falls back to the same compact text card.
- Skip/force replace the stream without leaving the voice chat. A short grace period ignores the
  old stream's end event, and a start watchdog retries if the call stays idle, so replacing a song
  does not cut the new song's audio.

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
- set `YT_PROXY` to a comma-separated residential proxy list. The bot rotates through the list;
  a proxy that returns 403/407/timeout is skipped for `YT_PROXY_COOLDOWN` seconds (default 300)
  and the next one is tried. Playback uses the same proxy that resolved the stream. When every
  proxy is cooling down the bot logs `all youtube proxies are cooling down`, tries a direct
  connection, and then falls back to alternate sources if they are enabled. Replace the list in
  Railway when the proxies expire; no code change is needed.
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
