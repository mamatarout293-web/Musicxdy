import asyncio
import re
from pathlib import Path

from pyrogram import Client

ENV = Path(__file__).with_name(".env")
SESSION_RE = re.compile(r"^\s*SESSION_STRING\s*=", re.IGNORECASE)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_session(session: str) -> None:
    lines = ENV.read_text(encoding="utf-8-sig", errors="ignore").splitlines() if ENV.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if SESSION_RE.match(line):
            if not replaced:
                out.append(f"SESSION_STRING={session}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"SESSION_STRING={session}")
    ENV.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


async def main() -> None:
    env = read_env()
    api_id_text = env.get("API_ID", "").strip()
    api_hash = env.get("API_HASH", "").strip()
    if not api_id_text.isdigit():
        api_id_text = input("API_ID: ").strip()
    if not api_hash:
        api_hash = input("API_HASH: ").strip()

    app = Client("session_gen", api_id=int(api_id_text), api_hash=api_hash, in_memory=True)
    async with app:
        session = await app.export_session_string()

    save_session(session)
    print("\nSESSION_STRING .env me save ho gaya.")
    print("Ab is window ko band karo aur start_windows.bat chalao.")
    print(f"Saved in: {ENV}")


if __name__ == "__main__":
    asyncio.run(main())
