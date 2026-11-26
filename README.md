# Music bot

## Deploy on Railway
1. Push the project files (including `Dockerfile` and `railway.json`) at the root of a GitHub repository.
2. In Railway, create a project with **Deploy from GitHub repo** and select that repository.
3. Add these required variables in the Railway service's **Variables** tab:
   - `API_ID` and `API_HASH` from my.telegram.org
   - `BOT_TOKEN` from BotFather
   - `SESSION_STRING` for the assistant Telegram account that joins voice chats
4. Deploy. Railway builds the Dockerfile and checks `/` on the port provided by `$PORT`.

Do not commit `.env`, `cookies.txt`, or any other credentials. `.env.example` is a template;
Railway variables are the deployment configuration. If credentials were committed or shared,
revoke/rotate them before deploying.

## How it stays alive
- `main.py` restarts `bot.py` forever if it exits, and Railway restarts the container if that ever dies.
- A watchdog restarts the bot if Telegram stops answering for ~3 minutes.
- A song that can't be played is skipped; a stream that dies instantly is retried from another source.
- yt-dlp is updated on every start (YouTube changes often).

## Music sources (tried in this order, automatically)
YouTube (4 client methods) → SoundCloud → Audius → Piped (YouTube mirrors).
Audius tries multiple discovery hosts and matching streamable results before giving up. Optional
extra sources can be enabled with `SAAVN_API` or `JAMENDO_CLIENT_ID`. Change order with `SOURCE_ORDER`.
A source that keeps failing is paused for 30s–10min.

The bot can only play streams a provider makes publicly available to it. It cannot guarantee every
song or subscriber/premium quality, and it does not bypass login, DRM, or provider bot restrictions.
For a complete licensed catalog, connect a provider that grants your deployment playback rights.

## Files
bot.py (bot) · sources.py (all music sources) · main.py (supervisor + health check) · config.py ·
Dockerfile · railway.json · requirements.txt · `.env.example`
