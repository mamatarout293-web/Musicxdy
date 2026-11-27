"""Entry point (Railway / Procfile / Docker).

* answers Railway's health check on $PORT right away,
* runs bot.py and restarts it forever if it ever stops (with a growing delay, so a crash loop can't spam).
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import config

ROOT = config.ROOT
os.chdir(ROOT)
config.load_env()

state = {"since": time.time(), "restarts": 0, "bot": "starting", "last_exit": None}
child: subprocess.Popen | None = None
stopping = False


def log(*a):
    print("[main]", *a, flush=True)


# ── health check ──────────────────────────────────────────────
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", **state, "uptime": int(time.time() - state["since"])}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def serve_health():
    port = int(os.environ.get("PORT", "8080"))
    try:
        HTTPServer(("0.0.0.0", port), Health).serve_forever()
    except Exception as e:
        log("health server not started:", e)


# ── shutdown ──────────────────────────────────────────────────
def shutdown(*_):
    """Railway sends SIGTERM on redeploy: stop the bot at once so two copies never share one session."""
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(8)
        except subprocess.TimeoutExpired:
            child.kill()
    os._exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main():
    global child
    threading.Thread(target=serve_health, daemon=True).start()

    # On a redeploy the old container may still be alive for a few seconds. Using the same Telegram
    # session from two places at once makes Telegram invalidate it, so wait before connecting.
    delay = int(os.environ.get("START_DELAY", "10"))
    if delay:
        log(f"waiting {delay}s before start (lets the previous deployment shut down)")
        time.sleep(delay)

    backoff = 5
    while not stopping:
        missing = config.missing()
        if missing:
            log("MISSING SETTINGS: " + ", ".join(missing) + " -> add them in Railway > Variables (or in .env)")
            state["bot"] = "missing settings"
            time.sleep(30)
            config.load_env()
            continue

        started = time.time()
        state["bot"] = "running"
        log("starting bot.py")
        child = subprocess.Popen([sys.executable, "bot.py"], cwd=ROOT)
        code = child.wait()
        ran = time.time() - started
        state.update(bot="restarting", last_exit=code, restarts=state["restarts"] + 1)
        # healthy for 2+ minutes -> restart quickly; crashing right away -> wait longer (max 2 min)
        backoff = 5 if ran > 120 else min(backoff * 2, 120)
        log(f"bot.py exited with code {code} after {int(ran)}s; restarting in {backoff}s")
        time.sleep(backoff)


if __name__ == "__main__":
    main()
