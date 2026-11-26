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
COOKIES_BASE64=... (optional; only if YouTube asks for login)
COOKIES_FROM_BROWSER=auto (optional; imports an existing browser profile when available)
```
3. Railway redeploys. Open **Deployments → View logs**; you should see `starting bot.py`.

YouTube authentication is deployment-dependent. The resolver reads `COOKIES_BASE64`, `COOKIES_TXT`, or a local `cookies.txt`. It can also try importing cookies from an already logged-in browser profile (`COOKIES_FROM_BROWSER=auto`, or e.g. `chrome:Default`). This only imports an existing session; it cannot create a Google login or manufacture valid cookies. Railway containers normally have no logged-in browser profile, so provide a fresh Netscape-format cookie export through Railway Variables when YouTube requires sign-in. Never commit or share cookie contents.

Variables set in Railway always win over a local `.env`.

## Run locally
Copy `.env.example` to `.env`, fill it in, then `python main.py`.

## How it stays alive
- `main.py` answers Railway's health check on `$PORT` and restarts `bot.py` forever if it exits.
- A watchdog restarts the bot if Telegram stops answering for ~3 minutes.
- A song that can't be played is skipped; a dead stream is retried from another source.
- yt-dlp is updated on every start (YouTube changes often).

## Music sources (tried in this order)
SoundCloud → Audius → optional Saavn/Jamendo → YouTube (last resort).
Audius tries multiple discovery hosts and matching streamable results before giving up. Piped was
removed because its instances are unreliable. Change the order with `SOURCE_ORDER`.

## Commands (Musicxdy-only names)

All group commands use the `/mx` prefix so they are less likely to collide with commands from other bots:

- `/mxstartplay <song or link>` — start playback (or add to queue)
- `/mxplay <song or link>` — alias for start/play
- `/mxstopplay` — stop playback and clear the queue
- `/mxpause`, `/mxresume`, `/mxskip`, `/mxprevious`
- `/mxqueue`, `/mxshuffle`, `/mxloop`
- `/mxtestaudio` — test Telegram voice-chat audio

The old generic commands (`/play`, `/stop`, etc.) are no longer registered. Inline player buttons continue to work.

The bot can only play streams a provider makes publicly available to it. It cannot guarantee every
song or subscriber/premium quality, and it does not bypass login, DRM, or provider bot restrictions.
YouTube cookies do not guarantee access from Railway. For a complete licensed catalog, connect a
provider that grants your deployment playback rights.

## Files
bot.py · sources.py · main.py · config.py · Dockerfile · railway.json · Procfile · requirements.txt · .env.example
