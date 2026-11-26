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
COOKIES_BASE64=...
```
3. Railway redeploys. Open **Deployments → View logs**; you should see `starting bot.py`.

`COOKIES_BASE64` is optional (YouTube cookies). Create it from your cookies.txt with:
`base64 -w0 cookies.txt` (Linux) or `base64 -i cookies.txt` (Mac).

Variables set in Railway always win over a local `.env`.

## Run locally
Copy `.env.example` to `.env`, fill it in, then `python main.py`.

## How it stays alive
- `main.py` answers Railway's health check on `$PORT` and restarts `bot.py` forever if it exits.
- A watchdog restarts the bot if Telegram stops answering for ~3 minutes.
- A song that can't be played is skipped; a dead stream is retried from another source.
- yt-dlp is updated on every start (YouTube changes often).

## Music sources
SoundCloud, Audius, optional Saavn/Jamendo, and YouTube are searched concurrently; the first playable
result starts playback. Providers on cooldown are only tried after healthy providers fail. Change the
source set with `SOURCE_ORDER`; its order breaks ties between simultaneous results. Audius tries
multiple discovery hosts and matching streamable results before giving up. Piped was removed because
its instances are unreliable.

Use `/mxplayforce <song>` to replace the current track and clear the queue after the requested song is
found.

The bot can only play streams a provider makes publicly available to it. It cannot guarantee every
song or subscriber/premium quality, and it does not bypass login, DRM, or provider bot restrictions.
YouTube cookies do not guarantee access from Railway. For a complete licensed catalog, connect a
provider that grants your deployment playback rights.

## Files
bot.py · sources.py · main.py · config.py · Dockerfile · railway.json · Procfile · requirements.txt · .env.example
