# -*- coding: utf-8 -*-
import asyncio
import json
import os
import sys
from urllib.parse import unquote, urlparse

from telethon import TelegramClient
import socks


APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "config.json"),
    os.path.join(APP_DIR, "config.json"),
    os.path.join(os.path.dirname(APP_DIR), "config.json"),
]
DEFAULT_PROXY = ""


def load_config() -> dict:
    for path in CONFIG_CANDIDATES:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
    return {}


def make_telethon_proxy(proxy_url: str):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    proxy_type = {
        "http": socks.HTTP,
        "https": socks.HTTP,
        "socks4": socks.SOCKS4,
        "socks5": socks.SOCKS5,
    }.get(scheme)
    if proxy_type is None or not parsed.hostname or not parsed.port:
        return None
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return (proxy_type, parsed.hostname, parsed.port, True, username, password)


def resolve_session_file() -> str:
    if os.path.exists(os.path.join(os.getcwd(), "config.json")):
        base_dir = os.getcwd()
    elif os.path.basename(APP_DIR).lower() == "dist":
        base_dir = os.path.dirname(APP_DIR)
    else:
        base_dir = APP_DIR
    session_dir = os.path.join(base_dir, "logs")
    os.makedirs(session_dir, exist_ok=True)
    return os.path.join(session_dir, "telegram_user")


async def main():
    config = load_config()
    api_id = str(config.get("telegram_api_id") or os.getenv("TELEGRAM_API_ID") or "").strip()
    api_hash = str(config.get("telegram_api_hash") or os.getenv("TELEGRAM_API_HASH") or "").strip()
    phone = str(config.get("telegram_phone") or os.getenv("TELEGRAM_PHONE") or "").strip()
    proxy_url = str(
        config.get("telegram_proxy")
        or os.getenv("TELEGRAM_PROXY")
        or os.getenv("YA_LYRICS_PROXY")
        or os.getenv("HTTP_PROXY")
        or DEFAULT_PROXY
    ).strip()

    if not api_id:
        api_id = input("telegram_api_id: ").strip()
    if not api_hash:
        api_hash = input("telegram_api_hash: ").strip()
    if not phone:
        phone = input("phone (+79990000000): ").strip()

    session_file = resolve_session_file()
    proxy = make_telethon_proxy(proxy_url)

    print("Session:", session_file + ".session")
    print("Proxy:", proxy_url if proxy else "disabled")
    client = TelegramClient(session_file, int(api_id), api_hash, proxy=proxy)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"Authorized as: {me.first_name or ''} @{me.username or ''}".strip())
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
