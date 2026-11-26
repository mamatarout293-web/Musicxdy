"""Shared settings loader. Railway/host variables always win over the .env file."""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
REQUIRED = ("API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING")


def load_env(path: str | None = None) -> None:
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            # setdefault: a value set in Railway's Variables tab is never overridden by .env
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def missing() -> list[str]:
    return [k for k in REQUIRED if not os.environ.get(k, "").strip()]
