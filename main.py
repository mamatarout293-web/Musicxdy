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
from urllib.error import URLError
from urllib.request import urlopen

import config

ROOT = config.ROOT
os.chdir(ROOT)
config.load_env()

state = {
    "since": time.time(),
    "restarts": 0,
    "bot": "starting",
    "last_exit": None,
    "service": os.environ.get("RAILWAY_SERVICE_NAME", ""),
    "project": os.environ.get("RAILWAY_PROJECT_NAME", ""),
}
child: subprocess.Popen | None = None
pot_provider: subprocess.Popen | None = None
stopping = False
POT_PROVIDER_PATH = "/usr/local/bin/bgutil-pot"
POT_PROVIDER_URL = "http://127.0.0.1:4416"


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


# ── YouTube PO-token provider ─────────────────────────────────
def pot_provider_ready() -> bool:
    try:
        with urlopen(f"{POT_PROVIDER_URL}/ping", timeout=1) as response:
            body = json.loads(response.read())
    except (OSError, URLError, json.JSONDecodeError):
        return False
    return isinstance(body, dict) and bool(body.get("version"))


def start_pot_provider():
    global pot_provider
    if pot_provider_ready():
        log("bgutil POT provider already listening")
        return
    if not os.path.isfile(POT_PROVIDER_PATH) or not os.access(POT_PROVIDER_PATH, os.X_OK):
        log(f"bgutil POT provider binary unavailable at {POT_PROVIDER_PATH}; YouTube PO tokens disabled")
        return

    log("starting bgutil POT provider on 127.0.0.1:4416")
    pot_provider = subprocess.Popen(
        [POT_PROVIDER_PATH, "server", "--host", "127.0.0.1", "--port", "4416"],
        cwd=ROOT,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if pot_provider_ready():
            log("bgutil POT provider ready")
            return
        if pot_provider.poll() is not None:
            log(f"bgutil POT provider exited during startup with code {pot_provider.returncode}")
            pot_provider = None
            return
        time.sleep(0.25)
    log("bgutil POT provider did not become ready within 20s")
    stop_pot_provider()


def stop_pot_provider():
    global pot_provider
    if not pot_provider or pot_provider.poll() is not None:
        pot_provider = None
        return
    pot_provider.terminate()
    try:
        pot_provider.wait(8)
    except subprocess.TimeoutExpired:
        pot_provider.kill()
        pot_provider.wait()
    pot_provider = None


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
    stop_pot_provider()
    os._exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main():
    global child
    service = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
    project = os.environ.get("RAILWAY_PROJECT_NAME", "").strip()
    log(f"service={service or '?'} project={project or '?'}")
    threading.Thread(target=serve_health, daemon=True).start()

    # Two copies of the bot in the same voice chat replace each other's stream,
    # which looks like the song restarting every few seconds. Keep only the named
    # service running the bot; other services stay up for Railway's health check.
    allowed_service = os.environ.get("BOT_SERVICE_NAME", "web").strip()
    if service and allowed_service and service != allowed_service:
        state["bot"] = f"health-only (bot runs on {allowed_service})"
        log(f"BOT_SERVICE_NAME={allowed_service}; this service ({service}) will only answer health checks")
        while not stopping:
            time.sleep(1)
        return

    start_pot_provider()

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
