# -*- coding: utf-8 -*-
import asyncio
import hashlib
import json
import logging
import msvcrt
import os
import re
import sys
import time
from io import BytesIO
from typing import Optional
from urllib.parse import unquote, urlparse

import aiohttp
from pypresence import ActivityType, Presence
import winrt.windows.media.control as wmc

try:
    from telethon import TelegramClient, functions, types
    from telethon.errors import FloodWaitError
    import socks
except Exception:
    TelegramClient = None
    functions = None
    types = None
    FloodWaitError = None
    socks = None

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "896771305108553788")
DEFAULT_PROXY = os.getenv("YA_LYRICS_PROXY", "")
MAX_STATUS_LEN = 128
TIME_OFFSET = 0.0
LYRIC_LOOKAHEAD = 0.35
POLL_INTERVAL = 0.25
MIN_RPC_INTERVAL = 0.0
SEEK_THRESHOLD = 1.5
SEEK_RAW_THRESHOLD = 1.0
TELEGRAM_DELETE_BATCH_SIZE = 20
TELEGRAM_QUEUE_MAX = 100
TELEGRAM_PLAYER_DEFAULT_UPDATE_INTERVAL = 3.0
TELEGRAM_PLAYER_DEFAULT_BAR_WIDTH = 10
CACHE_MISS_DEFAULT_TTL = 86400.0
MIN_LYRIC_GAP = 0.2
SESSION_LOG_INTERVAL = 30.0
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(APP_DIR, "logs")
CACHE_DIR = os.path.join(LOG_DIR, "cache")
TRACK_CACHE_DIR = os.path.join(CACHE_DIR, "tracks")
COVER_CACHE_DIR = os.path.join(CACHE_DIR, "covers")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
INSTANCE_LOCK_FILE = os.path.join(LOG_DIR, "app.lock")
CONFIG_CANDIDATES = [
    os.path.join(os.getcwd(), "config.json"),
    os.path.join(APP_DIR, "config.json"),
    os.path.join(os.path.dirname(APP_DIR), "config.json"),
]

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TRACK_CACHE_DIR, exist_ok=True)
os.makedirs(COVER_CACHE_DIR, exist_ok=True)
log_handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stderr is not None:
    log_handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=log_handlers,
)
log = logging.getLogger("ym-discord")


def load_config() -> dict:
    for path in CONFIG_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as file:
                config = json.load(file)
            log.info(f"Loaded config: {path}")
            return config
        except Exception as e:
            log.warning(f"Config {path}: {e}")
    return {}


CONFIG = load_config()
DISCORD_ENABLED = bool(CONFIG.get("discord_enabled", True))
TELEGRAM_MODE = (os.getenv("TELEGRAM_MODE") or CONFIG.get("telegram_mode", "bot")).strip().lower()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or CONFIG.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or CONFIG.get("telegram_chat_id", "")
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID") or CONFIG.get("telegram_api_id", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH") or CONFIG.get("telegram_api_hash", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE") or CONFIG.get("telegram_phone", "")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL") or CONFIG.get("telegram_channel", TELEGRAM_CHAT_ID)
TELEGRAM_PERSONAL_CHANNEL = bool(CONFIG.get("telegram_personal_channel", True))
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or CONFIG.get("telegram_proxy", "") or DEFAULT_PROXY
LYRICS_USE_PROXY = bool(CONFIG.get("lyrics_use_proxy", True))
TELEGRAM_PLAYER_UPDATE_INTERVAL = float(CONFIG.get("telegram_player_update_interval", TELEGRAM_PLAYER_DEFAULT_UPDATE_INTERVAL))
TELEGRAM_PLAYER_BAR_WIDTH = int(CONFIG.get("telegram_player_bar_width", TELEGRAM_PLAYER_DEFAULT_BAR_WIDTH))
CACHE_MISS_TTL = float(CONFIG.get("cache_miss_ttl", CACHE_MISS_DEFAULT_TTL))

if TELEGRAM_PROXY:
    os.environ.setdefault("HTTP_PROXY", TELEGRAM_PROXY)
    os.environ.setdefault("HTTPS_PROXY", TELEGRAM_PROXY)

lyrics_cache: dict[str, list[tuple[float, str]]] = {}
cover_cache: dict[str, Optional[str]] = {}
_last_session_log = 0.0
_instance_lock_handle = None


def make_telethon_proxy(proxy_url: str):
    if not proxy_url or socks is None:
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


def telegram_session_bases() -> list[str]:
    candidates = [
        os.path.join(os.getcwd(), "logs", "telegram_user"),
        os.path.join(APP_DIR, "logs", "telegram_user"),
        os.path.join(os.path.dirname(APP_DIR), "logs", "telegram_user"),
    ]
    result = []
    seen = set()
    for path in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized not in seen:
            seen.add(normalized)
            result.append(os.path.abspath(path))
    return result


def cache_hash(value: str) -> str:
    return hashlib.sha1(value.strip().lower().encode("utf-8", errors="ignore")).hexdigest()


def track_cache_path(track_key: str) -> str:
    return os.path.join(TRACK_CACHE_DIR, cache_hash(track_key) + ".json")


def cover_cache_path(cover_url: str) -> str:
    return os.path.join(COVER_CACHE_DIR, cache_hash(cover_url) + ".jpg")


def read_track_cache(track_key: str) -> dict:
    path = track_cache_path(track_key)
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"Track cache read: {e}")
        return {}


def write_track_cache(track_key: str, updates: dict):
    path = track_cache_path(track_key)
    data = read_track_cache(track_key)
    data.update(updates)
    data.setdefault("track_key", track_key)
    data["updated_at"] = time.time()
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Track cache write: {e}")


def cache_entry_fresh(timestamp: float) -> bool:
    return timestamp > 0 and time.time() - timestamp < CACHE_MISS_TTL


async def get_cover_bytes(cover_url: str) -> Optional[bytes]:
    if not cover_url:
        return None
    path = cover_cache_path(cover_url)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as file:
                data = file.read()
            log.info(f"Cover file cache hit: {path}")
            return data
    except Exception as e:
        log.warning(f"Cover file cache read: {e}")

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(cover_url, timeout=aiohttp.ClientTimeout(total=15), ssl=False) as response:
                if response.status != 200:
                    log.warning(f"Cover download: {response.status}")
                    return None
                data = await response.read()
    except Exception as e:
        log.warning(f"Cover download: {e}")
        return None

    try:
        with open(path, "wb") as file:
            file.write(data)
        log.info(f"Cover file cached: {path}")
    except Exception as e:
        log.warning(f"Cover file cache write: {e}")
    return data


def acquire_single_instance():
    global _instance_lock_handle
    _instance_lock_handle = open(INSTANCE_LOCK_FILE, "a+b")
    try:
        _instance_lock_handle.seek(0)
        msvcrt.locking(_instance_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log.info("Another instance is already running; exiting this copy.")
        sys.exit(0)


async def log_available_smtc_sessions(sessions):
    global _last_session_log
    now = time.monotonic()
    if now - _last_session_log < SESSION_LOG_INTERVAL:
        return
    _last_session_log = now

    if not sessions:
        log.info("No SMTC sessions visible. Start a desktop music app and play a track.")
        return

    seen = []
    for session in sessions:
        app_id = session.source_app_user_model_id or ""
        try:
            info = await session.try_get_media_properties_async()
            status = int(session.get_playback_info().playback_status)
            seen.append(f"{app_id} | status={status} | {info.artist or ''} - {info.title or ''}")
        except Exception as e:
            seen.append(f"{app_id} | error={e}")

    log.info("Visible SMTC sessions: " + " ; ".join(seen))


APP_DISPLAY_NAMES = {
    "yandex": "Яндекс Музыку",
    "spotify": "Spotify",
    "applemusic": "Apple Music",
    "apple music": "Apple Music",
    "itunes": "Apple Music",
    "vk": "VK Музыку",
    "boom": "VK Музыку",
    "youtube": "YouTube Music",
    "tidal": "Tidal",
    "deezer": "Deezer",
}

MUSIC_APP_PRIORITY = ["yandex", "spotify", "applemusic", "itunes", "vk", "boom", "youtube", "tidal", "deezer"]


def get_app_display_name(app_id: str) -> str:
    normalized = app_id.lower().replace(".", " ").replace("_", " ").replace("-", " ")
    compact = normalized.replace(" ", "")
    for key, name in APP_DISPLAY_NAMES.items():
        if key in normalized or key in compact:
            return name
    return "Музыку"


async def get_smtc_state() -> Optional[dict]:
    try:
        manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
        sessions = list(manager.get_sessions())

        music_session = None
        music_app_id = ""
        for session in sessions:
            app_id = (session.source_app_user_model_id or "").lower()
            if any(key in app_id for key in MUSIC_APP_PRIORITY):
                music_session = session
                music_app_id = app_id
                break

        if music_session is None:
            try:
                current_session = manager.get_current_session()
            except Exception:
                current_session = None
            if current_session is not None:
                music_session = current_session
                music_app_id = current_session.source_app_user_model_id or ""
                log.info(f"Using current SMTC session fallback: {music_app_id}")

        if music_session is None:
            for session in sessions:
                try:
                    info = await session.try_get_media_properties_async()
                    if info.title:
                        music_session = session
                        music_app_id = session.source_app_user_model_id or ""
                        log.info(f"Using titled SMTC session fallback: {music_app_id}")
                        break
                except Exception:
                    continue

        if music_session is None:
            await log_available_smtc_sessions(sessions)
            return None

        info = await music_session.try_get_media_properties_async()
        timeline = music_session.get_timeline_properties()
        status = int(music_session.get_playback_info().playback_status)
        title = info.title or ""
        duration = 0.0
        try:
            start_time = timeline.start_time.total_seconds()
            end_time = timeline.end_time.total_seconds()
            if end_time > start_time:
                duration = end_time - start_time
        except Exception:
            duration = 0.0

        if not title:
            await log_available_smtc_sessions(sessions)
            return None

        return {
            "artist": info.artist or "",
            "title": title,
            "position": timeline.position.total_seconds(),
            "duration": duration,
            "playing": status == 4,  # 4=Playing, 5=Paused
            "app_name": get_app_display_name(music_app_id),
        }
    except Exception as e:
        log.warning(f"SMTC: {e}")
        return None


def parse_lrc(synced: str) -> list[tuple[float, str]]:
    lines = []
    for line in synced.splitlines():
        match = re.match(r"\[(\d+):(\d+\.\d+)\]\s*(.*)", line)
        if not match:
            continue
        timestamp = int(match.group(1)) * 60 + float(match.group(2))
        text = match.group(3).strip()
        if text:
            lines.append((timestamp, text))
    return normalize_lyrics(lines)


def normalize_lyrics(lines: list[tuple[float, str]]) -> list[tuple[float, str]]:
    normalized = []
    last_time: Optional[float] = None
    last_text = ""

    for timestamp, text in sorted(lines, key=lambda item: item[0]):
        if timestamp < 0:
            continue
        if last_time is not None and abs(timestamp - last_time) < MIN_LYRIC_GAP:
            if text == last_text:
                continue
        normalized.append((timestamp, text))
        last_time = timestamp
        last_text = text

    return normalized


def format_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_player_text(position: float, duration: float) -> str:
    duration = max(duration, 0.0)
    position = min(max(position, 0.0), duration) if duration else max(position, 0.0)
    width = max(5, TELEGRAM_PLAYER_BAR_WIDTH)
    ratio = position / duration if duration > 0 else 0.0
    knob_index = min(width - 1, max(0, round(ratio * (width - 1))))
    bar = ("━" * knob_index) + "●" + ("─" * (width - knob_index - 1))
    return f"▶ {format_mmss(position)} {bar} {format_mmss(duration)}"


async def search_lrclib(q: str) -> list[tuple[float, str]]:
    try:
        async with aiohttp.ClientSession(trust_env=LYRICS_USE_PROXY) as session:
            async with session.get(
                "https://lrclib.net/api/search",
                params={"q": q},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return []
                results = await response.json()

        for item in results:
            synced = item.get("syncedLyrics", "")
            if synced:
                lines = parse_lrc(synced)
                if lines:
                    return lines
    except Exception as e:
        log.warning(f"lrclib '{q}': {e}")
    return []


async def get_lyrics(artist: str, title: str) -> list[tuple[float, str]]:
    cache_key = f"{artist}|{title}"
    if cache_key in lyrics_cache:
        return lyrics_cache[cache_key]

    disk_cache = read_track_cache(cache_key)
    cached_lines = disk_cache.get("lyrics")
    if isinstance(cached_lines, list) and cached_lines:
        lines = normalize_lyrics(
            [
                (float(item["time"]), str(item["text"]))
                for item in cached_lines
                if isinstance(item, dict) and "time" in item and "text" in item
            ]
        )
        if lines:
            lyrics_cache[cache_key] = lines
            log.info(f"Lyrics cache hit: {artist} - {title} ({len(lines)} lines)")
            return lines
    if disk_cache.get("lyrics_missing") and cache_entry_fresh(float(disk_cache.get("lyrics_checked_at", 0.0))):
        log.info(f"Lyrics cache miss remembered: {artist} - {title}")
        lyrics_cache[cache_key] = []
        return []

    log.info(f"Searching lyrics: {artist} - {title}")
    queries = [
        f"{artist} {title}",
        title,
        f"{artist.split(',')[0].strip()} {title}",
    ]
    lines = []
    for q in queries:
        lines = await search_lrclib(q)
        if lines:
            log.info(f"lrclib: {len(lines)} synced lines")
            break

    if not lines:
        log.warning("Lyrics not found")
        write_track_cache(
            cache_key,
            {
                "artist": artist,
                "title": title,
                "lyrics": [],
                "lyrics_missing": True,
                "lyrics_checked_at": time.time(),
            },
        )
    else:
        log.info(f"Lyrics timing: {lines[0][0]:.1f}s..{lines[-1][0]:.1f}s")
        if lines[0][0] > 10:
            log.warning(f"First synced line starts late: {lines[0][0]:.1f}s")
        write_track_cache(
            cache_key,
            {
                "artist": artist,
                "title": title,
                "lyrics": [{"time": timestamp, "text": text} for timestamp, text in lines],
                "lyrics_missing": False,
                "lyrics_checked_at": time.time(),
            },
        )

    lyrics_cache[cache_key] = lines
    return lines


def first_artist_name(artist: str) -> str:
    return re.split(r",|&|feat\.|ft\.", artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()


async def search_deezer_cover(q: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                "https://api.deezer.com/search",
                params={"q": q},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()

        for item in data.get("data", []):
            album = item.get("album") or {}
            cover = album.get("cover_xl") or album.get("cover_big")
            if cover:
                return cover
    except Exception as e:
        log.warning(f"deezer '{q}': {e}")
    return None


async def get_cover_url(artist: str, title: str) -> Optional[str]:
    cache_key = f"{artist}|{title}"
    if cache_key in cover_cache:
        return cover_cache[cache_key]

    disk_cache = read_track_cache(cache_key)
    cached_cover = disk_cache.get("cover_url")
    if isinstance(cached_cover, str) and cached_cover:
        cover_cache[cache_key] = cached_cover
        log.info(f"Cover cache hit: {artist} - {title}")
        return cached_cover
    if disk_cache.get("cover_missing") and cache_entry_fresh(float(disk_cache.get("cover_checked_at", 0.0))):
        log.info(f"Cover cache miss remembered: {artist} - {title}")
        cover_cache[cache_key] = None
        return None

    artist_short = first_artist_name(artist)
    queries = [
        f'artist:"{artist_short}" track:"{title}"',
        f"{artist} {title}",
        title,
    ]
    cover_url = None
    for q in queries:
        cover_url = await search_deezer_cover(q)
        if cover_url:
            log.info(f"Cover found: {cover_url}")
            break

    if not cover_url:
        log.warning("Cover not found")
        write_track_cache(
            cache_key,
            {
                "artist": artist,
                "title": title,
                "cover_url": "",
                "cover_missing": True,
                "cover_checked_at": time.time(),
            },
        )
    else:
        write_track_cache(
            cache_key,
            {
                "artist": artist,
                "title": title,
                "cover_url": cover_url,
                "cover_missing": False,
                "cover_checked_at": time.time(),
            },
        )

    cover_cache[cache_key] = cover_url
    return cover_url


class DiscordActivityClient:
    def __init__(self, client_id: str):
        self.rpc = Presence(client_id)
        self._connected = False
        self._last_payload: Optional[tuple[str, str, str, str]] = None
        self._last_update = 0.0

    async def _connect(self) -> bool:
        if self._connected:
            return True
        try:
            await asyncio.wait_for(asyncio.to_thread(self.rpc.connect), timeout=8)
            self._connected = True
            log.info("Discord RPC connected")
            return True
        except Exception as e:
            log.warning(f"Discord RPC: {e}")
            return False

    @staticmethod
    def _trim(text: str) -> str:
        if len(text) <= MAX_STATUS_LEN:
            return text
        return text[: MAX_STATUS_LEN - 3] + "..."

    async def set_activity(
        self,
        artist: str,
        title: str,
        current_line: str,
        next_line: Optional[str],
        position: float,
        cover_url: Optional[str],
        app_name: str,
        force: bool = False,
    ):
        if not await self._connect():
            return

        details = self._trim(current_line or title)
        state = self._trim(f"{artist} - {title}" if artist else title)
        start = int(time.time() - max(position, 0.0))
        payload_key = (details, state, cover_url or "", app_name)
        now = time.monotonic()

        if payload_key == self._last_payload and not force:
            return
        if now - self._last_update < MIN_RPC_INTERVAL:
            return

        self._last_payload = payload_key
        self._last_update = now
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: self.rpc.update(
                        activity_type=ActivityType.LISTENING,
                        name=app_name,
                        details=details,
                        state=state,
                        start=start,
                        large_image=cover_url,
                    )
                ),
                timeout=8,
            )
            log.info(f"Activity -> {details!r} / {state!r}")
        except Exception as e:
            self._connected = False
            log.warning(f"Discord RPC update: {e}")

    async def clear_status(self):
        if not self._connected:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self.rpc.clear), timeout=8)
            self._last_payload = None
            log.info("Activity cleared")
        except Exception as e:
            self._connected = False
            log.warning(f"Discord RPC clear: {e}")


class NullDiscordActivityClient:
    async def set_activity(self, *args, **kwargs):
        return

    async def clear_status(self):
        return


class TelegramChannelClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(self.token and self.chat_id)
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.state_file = os.path.join(LOG_DIR, "telegram_state.json")
        self.track_message_ids: list[int] = []
        self.update_offset: Optional[int] = None
        self.current_track_key = ""
        self.current_title = ""
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        self.inflight_track_key = ""
        self.inflight_text = ""
        self.player_message_id: Optional[int] = None
        self.player_pinned = False
        self.current_player_text = ""
        self.queued_player_track_key = ""
        self.queued_player_text = ""
        self.last_player_enqueue = 0.0
        self.current_photo_url = ""
        self.failed_photo_url = ""
        self.failed_photo_at = 0.0
        self.queue: Optional[asyncio.Queue] = None
        self.worker_task: Optional[asyncio.Task] = None
        self._load_state()

        if self.enabled:
            log.info("Telegram channel updates enabled")
        else:
            log.info("Telegram channel updates disabled; set telegram_bot_token and telegram_chat_id in config.json")

    def start_worker(self):
        if not self.enabled or self.worker_task is not None:
            return
        self.queue = asyncio.Queue(maxsize=TELEGRAM_QUEUE_MAX)
        self.worker_task = asyncio.create_task(self._worker())
        log.info("Telegram worker started")

    def _payload_preview(self, payload: dict) -> str:
        safe = {}
        for key, value in payload.items():
            if key == "text" and isinstance(value, str):
                safe[key] = value[:80]
            else:
                safe[key] = value
        return json.dumps(safe, ensure_ascii=False)

    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as file:
                    data = json.load(file)
                old_message_id = data.get("message_id")
                self.track_message_ids = list(data.get("track_message_ids") or [])
                if old_message_id and old_message_id not in self.track_message_ids:
                    self.track_message_ids.append(int(old_message_id))
                player_message_id = data.get("player_message_id")
                if player_message_id:
                    self.player_message_id = int(player_message_id)
                self.player_pinned = bool(data.get("player_pinned", False))
                self.update_offset = data.get("update_offset")
                self.current_photo_url = data.get("photo_url", "")
                self.current_track_key = data.get("track_key", "")
        except Exception as e:
            log.warning(f"Telegram state load: {e}")

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "track_message_ids": self.track_message_ids,
                        "player_message_id": self.player_message_id,
                        "player_pinned": self.player_pinned,
                        "update_offset": self.update_offset,
                        "photo_url": self.current_photo_url,
                        "track_key": self.current_track_key,
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            log.warning(f"Telegram state save: {e}")

    async def _request(self, method: str, payload: dict) -> Optional[dict]:
        if not self.enabled:
            return None
        started = time.monotonic()
        log.info(f"Telegram request {method}: {self._payload_preview(payload)}")
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    f"{self.api}/{method}",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status == 200 and data.get("ok"):
                        elapsed = (time.monotonic() - started) * 1000
                        log.info(f"Telegram response {method}: ok in {elapsed:.0f}ms")
                        return data.get("result")

                    description = data.get("description", "") if isinstance(data, dict) else str(data)
                    if "message is not modified" not in description.lower():
                        log.warning(f"Telegram {method}: {response.status} {description}")
        except Exception as e:
            log.warning(f"Telegram {method}: {e}")
        return None

    async def _post_form(self, method: str, form: aiohttp.FormData) -> Optional[dict]:
        if not self.enabled:
            return None
        started = time.monotonic()
        log.info(f"Telegram request {method}: form-data")
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    f"{self.api}/{method}",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status == 200 and data.get("ok"):
                        elapsed = (time.monotonic() - started) * 1000
                        log.info(f"Telegram response {method}: ok in {elapsed:.0f}ms")
                        return data.get("result")
                    description = data.get("description", "") if isinstance(data, dict) else str(data)
                    log.warning(f"Telegram {method}: {response.status} {description}")
        except Exception as e:
            log.warning(f"Telegram {method}: {e}")
        return None

    async def _cleanup_service_messages(self, attempts: int = 3):
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(0.5)

            payload = {
                "timeout": 0,
                "allowed_updates": ["channel_post"],
            }
            if self.update_offset is not None:
                payload["offset"] = self.update_offset

            result = await self._request("getUpdates", payload)
            if not isinstance(result, list):
                return

            for update in result:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.update_offset = update_id + 1

                post = update.get("channel_post") or {}
                chat = post.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                chat_username = chat.get("username", "")
                target = self.chat_id.lstrip("@")
                is_target = chat_id == self.chat_id or (chat_username and chat_username.lower() == target.lower())
                if not is_target:
                    continue

                if "new_chat_title" in post or "new_chat_photo" in post or "pinned_message" in post:
                    message_id = post.get("message_id")
                    if message_id:
                        await self._request("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id})
                        log.info(f"Telegram service message deleted: {message_id}")

            self._save_state()

    async def _delete_track_messages(self, include_player: bool = False):
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        self.inflight_track_key = ""
        self.inflight_text = ""

        ids = list(dict.fromkeys(self.track_message_ids))
        if include_player and self.player_message_id and self.player_message_id not in ids:
            ids.append(self.player_message_id)
        if not include_player and self.player_message_id:
            ids = [message_id for message_id in ids if message_id != self.player_message_id]
        self.track_message_ids = []
        if not include_player and self.player_message_id:
            self.track_message_ids.append(self.player_message_id)
        if include_player:
            self.player_message_id = None
            self.player_pinned = False
            self.current_player_text = ""
            self.queued_player_track_key = ""
            self.queued_player_text = ""
            self.last_player_enqueue = 0.0
        self._save_state()

        if not ids:
            return

        async def delete_one(message_id: int):
            await self._request("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id})

        for start in range(0, len(ids), TELEGRAM_DELETE_BATCH_SIZE):
            batch = ids[start : start + TELEGRAM_DELETE_BATCH_SIZE]
            await asyncio.gather(*(delete_one(message_id) for message_id in batch))
        log.info(f"Telegram track messages deleted: {len(ids)}")

    async def _pin_player(self):
        if not self.player_message_id:
            return
        result = await self._request(
            "pinChatMessage",
            {
                "chat_id": self.chat_id,
                "message_id": self.player_message_id,
                "disable_notification": True,
            },
        )
        if result is not None:
            self.player_pinned = True
            self._save_state()
            log.info(f"Telegram player pinned: {self.player_message_id}")
            await self._cleanup_service_messages()

    async def _set_title(self, artist: str, title: str):
        channel_title = f"{artist} - {title}" if artist else title
        channel_title = channel_title[:128]
        if channel_title == self.current_title:
            return
        result = await self._request("setChatTitle", {"chat_id": self.chat_id, "title": channel_title})
        if result is not None:
            self.current_title = channel_title
            log.info(f"Telegram title -> {channel_title!r}")
            await self._cleanup_service_messages()

    async def _set_photo(self, cover_url: Optional[str]):
        if not cover_url or cover_url == self.current_photo_url:
            return
        if cover_url == self.failed_photo_url and time.monotonic() - self.failed_photo_at < 600:
            return

        try:
            photo_bytes = await get_cover_bytes(cover_url)
            if not photo_bytes:
                self.failed_photo_url = cover_url
                self.failed_photo_at = time.monotonic()
                return

            form = aiohttp.FormData()
            form.add_field("chat_id", self.chat_id)
            form.add_field("photo", photo_bytes, filename="cover.jpg", content_type="image/jpeg")
            result = await self._post_form("setChatPhoto", form)
            if result is not None:
                self.current_photo_url = cover_url
                self._save_state()
                log.info("Telegram photo updated")
                await self._cleanup_service_messages()
        except Exception as e:
            self.failed_photo_url = cover_url
            self.failed_photo_at = time.monotonic()
            log.warning(f"Telegram photo: {e}")

    async def _start_track(self, artist: str, title: str, cover_url: Optional[str]):
        track_key = f"{artist}|{title}"
        if track_key != self.current_track_key:
            await self._delete_track_messages(include_player=True)
            self.current_track_key = track_key
            self._save_state()

        await self._set_title(artist, title)
        await self._set_photo(cover_url)

    async def _send_line(self, track_key: str, text: str):
        text = text.strip()[:4096]
        if not text or text == self.current_text or (track_key == self.inflight_track_key and text == self.inflight_text):
            return

        self.inflight_track_key = track_key
        self.inflight_text = text
        result = await self._request(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_notification": True,
            },
        )
        if isinstance(result, dict) and result.get("message_id"):
            self.track_message_ids.append(int(result["message_id"]))
            self._save_state()
            log.info(f"Telegram line -> {text!r}")
        else:
            log.warning(f"Telegram line response missing; keeping duplicate guard for {text!r}")

        self.current_text = text

    async def _upsert_player(self, artist: str, title: str, position: float, duration: float):
        if duration <= 0:
            return
        track_key = f"{artist}|{title}"
        await self._start_track(artist, title, None)
        text = format_player_text(position, duration)
        if text == self.current_player_text:
            if not self.player_pinned:
                await self._pin_player()
            return

        if not self.player_message_id:
            result = await self._request(
                "sendMessage",
                {
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_notification": True,
                },
            )
            if isinstance(result, dict) and result.get("message_id"):
                self.player_message_id = int(result["message_id"])
                self.player_pinned = False
                self.current_player_text = text
                self._save_state()
                log.info(f"Telegram player -> {text!r}")
                await self._pin_player()
            return

        result = await self._request(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": self.player_message_id,
                "text": text,
            },
        )
        if result is not None:
            self.current_player_text = text
            self._save_state()
            log.info(f"Telegram player -> {text!r}")
            if not self.player_pinned:
                await self._pin_player()

    async def update(self, artist: str, title: str, line: str, cover_url: Optional[str]):
        if not self.enabled:
            return
        track_key = f"{artist}|{title}"
        await self._start_track(artist, title, cover_url)
        await self._send_line(track_key, line or title)

    async def clear_track(self):
        if not self.enabled:
            return
        await self._delete_track_messages()
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        self.inflight_track_key = ""
        self.inflight_text = ""
        self._save_state()

    async def reset_track_messages(self):
        if not self.enabled:
            return
        await self._delete_track_messages()
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        self.inflight_track_key = ""
        self.inflight_text = ""
        self._save_state()

    def enqueue_update(self, artist: str, title: str, line: str, cover_url: Optional[str]):
        if not self.enabled:
            return
        text = (line or title).strip()[:4096]
        track_key = f"{artist}|{title}"
        if track_key == self.queued_track_key and text and text == self.queued_text:
            return
        if track_key == self.inflight_track_key and text and text == self.inflight_text:
            return
        if track_key == self.current_track_key and text and text == self.current_text:
            return
        self.queued_track_key = track_key
        self.queued_text = text
        self.start_worker()
        if self.queue is None:
            return
        command = ("update", artist, title, text, cover_url)
        self._put_latest(command)

    def enqueue_player(self, artist: str, title: str, position: float, duration: float):
        if not self.enabled or duration <= 0:
            return
        track_key = f"{artist}|{title}"
        text = format_player_text(position, duration)
        now = time.monotonic()
        if track_key == self.queued_player_track_key and text == self.queued_player_text:
            return
        if track_key == self.current_track_key and text == self.current_player_text and self.player_pinned:
            return
        if self.player_message_id and now - self.last_player_enqueue < TELEGRAM_PLAYER_UPDATE_INTERVAL:
            return
        self.queued_player_track_key = track_key
        self.queued_player_text = text
        self.last_player_enqueue = now
        self.start_worker()
        if self.queue is None:
            return
        self._drop_pending_player_commands()
        self._put_latest(("player", artist, title, position, duration))

    def enqueue_clear(self):
        if not self.enabled:
            return
        self.start_worker()
        if self.queue is None:
            return
        self._put_latest(("clear",))

    def enqueue_reset_track_messages(self):
        if not self.enabled:
            return
        self.start_worker()
        if self.queue is None:
            return
        self._put_latest(("reset",))

    def _put_latest(self, command: tuple):
        if self.queue is None:
            return
        while self.queue.full():
            try:
                dropped = self.queue.get_nowait()
                self.queue.task_done()
                log.warning(f"Telegram queue full; dropped {dropped[0]}")
            except asyncio.QueueEmpty:
                break
        self.queue.put_nowait(command)
        log.info(f"Telegram queued {command[0]} size={self.queue.qsize()}")

    def _drop_pending_player_commands(self):
        if self.queue is None:
            return
        kept = []
        dropped_count = 0
        while True:
            try:
                command = self.queue.get_nowait()
                self.queue.task_done()
                if command and command[0] == "player":
                    dropped_count += 1
                else:
                    kept.append(command)
            except asyncio.QueueEmpty:
                break
        for command in kept:
            self.queue.put_nowait(command)
        if dropped_count:
            log.info(f"Telegram pending player commands dropped: {dropped_count}")

    async def _worker(self):
        if self.queue is None:
            return
        while True:
            command = await self.queue.get()
            try:
                action = command[0]
                log.info(f"Telegram worker action: {action}")
                if action == "update":
                    _, artist, title, line, cover_url = command
                    await self.update(artist, title, line, cover_url)
                elif action == "player":
                    _, artist, title, position, duration = command
                    await self._upsert_player(artist, title, position, duration)
                elif action == "clear":
                    await self.clear_track()
                elif action == "reset":
                    await self.reset_track_messages()
            except Exception as e:
                log.warning(f"Telegram worker: {e}")
            finally:
                self.queue.task_done()


class TelegramUserChannelClient:
    def __init__(
        self,
        api_id: str,
        api_hash: str,
        phone: str,
        channel: str,
        personal_channel: bool = True,
        metadata_client: Optional[TelegramChannelClient] = None,
    ):
        self.api_id = str(api_id).strip()
        self.api_hash = str(api_hash).strip()
        self.phone = str(phone).strip()
        self.channel = str(channel).strip()
        self.personal_channel = personal_channel
        self.metadata_client = metadata_client if metadata_client and metadata_client.enabled else None
        self.enabled = bool(self.api_id and self.api_hash and self.channel and TelegramClient is not None)
        self.proxy = make_telethon_proxy(str(TELEGRAM_PROXY or ""))
        self.session_files = telegram_session_bases()
        self.session_file = self.session_files[0]
        self.client = None
        self.entity = None
        self.input_channel = None
        self.track_message_ids: list[int] = []
        self.current_track_key = ""
        self.current_title = ""
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        self.player_message_id: Optional[int] = None
        self.player_pinned = False
        self.current_player_text = ""
        self.queued_player_track_key = ""
        self.queued_player_text = ""
        self.last_player_enqueue = 0.0
        self.current_photo_url = ""
        self.personal_attached = False
        self.title_flood_until = 0.0
        self.photo_flood_until = 0.0
        self.personal_flood_until = 0.0
        self.telegram_flood_until = 0.0
        self.last_flood_log = 0.0
        self.queue: Optional[asyncio.Queue] = None
        self.worker_task: Optional[asyncio.Task] = None

        if self.enabled:
            log.info("Telegram user account updates enabled")
            if self.metadata_client:
                log.info("Telegram metadata updates will use bot account")
        else:
            log.info("Telegram user mode disabled; set telegram_api_id, telegram_api_hash and telegram_channel")

    def start_worker(self):
        if not self.enabled or self.worker_task is not None:
            return
        self.queue = asyncio.Queue(maxsize=TELEGRAM_QUEUE_MAX)
        self.worker_task = asyncio.create_task(self._worker())
        log.info("Telegram user worker started")

    def _flood_active(self) -> bool:
        now = time.monotonic()
        if now >= self.telegram_flood_until:
            return False
        if now - self.last_flood_log >= 30.0:
            remaining = self.telegram_flood_until - now
            log.warning(f"Telegram user flood wait active; paused for {remaining:.0f}s")
            self.last_flood_log = now
        return True

    def _set_flood_wait(self, seconds: int):
        self.telegram_flood_until = time.monotonic() + max(seconds, 1) + 5
        log.warning(f"Telegram user paused for flood wait: {seconds}s")
        self._drop_pending_commands()

    async def _connect(self) -> bool:
        if not self.enabled:
            return False
        searched = []
        for session_file in self.session_files:
            searched.append(session_file + ".session")
            if self.client is not None and session_file != self.session_file:
                await self.client.disconnect()
                self.client = None
                self.entity = None
                self.input_channel = None
            self.session_file = session_file
            if self.client is None:
                os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
                self.client = TelegramClient(self.session_file, int(self.api_id), self.api_hash, proxy=self.proxy)
                if self.proxy:
                    log.info("Telegram user client proxy enabled")
            if not self.client.is_connected():
                await self.client.connect()
            if await self.client.is_user_authorized():
                break
        else:
            log.warning("Telegram user session is not authorized; run telegram_login.bat first")
            log.warning("Telegram session paths checked: " + " ; ".join(searched))
            return False
        if self.entity is None:
            self.entity = await self.client.get_entity(self.channel)
            self.input_channel = await self.client.get_input_entity(self.entity)
            log.info(f"Telegram user channel resolved: {self.channel}")
        return True

    async def _delete_track_messages(self, include_player: bool = False):
        self.current_text = ""
        self.queued_track_key = ""
        self.queued_text = ""
        if not await self._connect():
            return
        ids = list(dict.fromkeys(self.track_message_ids))
        if include_player and self.player_message_id and self.player_message_id not in ids:
            ids.append(self.player_message_id)
        if not include_player and self.player_message_id:
            ids = [message_id for message_id in ids if message_id != self.player_message_id]
        self.track_message_ids = []
        if include_player:
            self.player_message_id = None
            self.player_pinned = False
            self.current_player_text = ""
            self.queued_player_track_key = ""
            self.queued_player_text = ""
            self.last_player_enqueue = 0.0
        if not ids:
            return
        for start in range(0, len(ids), TELEGRAM_DELETE_BATCH_SIZE):
            batch = ids[start : start + TELEGRAM_DELETE_BATCH_SIZE]
            await self.client.delete_messages(self.entity, batch)
        log.info(f"Telegram user track messages deleted: {len(ids)}")

    async def _pin_player(self):
        if not self.player_message_id:
            return
        if not await self._connect():
            return
        try:
            await self.client.pin_message(self.entity, self.player_message_id, notify=False)
            self.player_pinned = True
            log.info(f"Telegram user player pinned: {self.player_message_id}")
            if self.metadata_client:
                await self.metadata_client._cleanup_service_messages()
        except Exception as e:
            log.warning(f"Telegram user pin player: {e}")

    async def _set_personal_channel(self, attach: bool, force: bool = False):
        if not self.personal_channel or not functions or not types:
            return
        if attach == self.personal_attached and not force:
            return
        if time.monotonic() < self.personal_flood_until:
            return
        if not await self._connect():
            return
        channel = self.input_channel if attach else types.InputChannelEmpty()
        try:
            await self.client(functions.account.UpdatePersonalChannelRequest(channel=channel))
            self.personal_attached = attach
            log.info(f"Telegram personal channel {'attached' if attach else 'detached'}")
        except Exception as e:
            if FloodWaitError is not None and isinstance(e, FloodWaitError):
                self.personal_flood_until = time.monotonic() + e.seconds + 5
                log.warning(f"Telegram personal channel flood wait: {e.seconds}s")
            else:
                log.warning(f"Telegram personal channel: {e}")

    async def _set_title(self, artist: str, title: str):
        channel_title = f"{artist} - {title}" if artist else title
        channel_title = channel_title[:128]
        if channel_title == self.current_title:
            return
        if time.monotonic() < self.title_flood_until:
            return
        if not await self._connect():
            return
        try:
            await self.client(functions.channels.EditTitleRequest(channel=self.input_channel, title=channel_title))
            self.current_title = channel_title
            log.info(f"Telegram user title -> {channel_title!r}")
        except Exception as e:
            if FloodWaitError is not None and isinstance(e, FloodWaitError):
                self.title_flood_until = time.monotonic() + e.seconds + 5
                log.warning(f"Telegram title flood wait: {e.seconds}s; lyrics will still be posted")
            else:
                log.warning(f"Telegram user title: {e}")

    async def _set_photo(self, cover_url: Optional[str]):
        if not cover_url or cover_url == self.current_photo_url:
            return
        if time.monotonic() < self.photo_flood_until:
            return
        if not await self._connect():
            return
        try:
            photo_bytes = await get_cover_bytes(cover_url)
            if not photo_bytes:
                return
            uploaded = await self.client.upload_file(BytesIO(photo_bytes), file_name="cover.jpg")
            photo = types.InputChatUploadedPhoto(file=uploaded)
            await self.client(functions.channels.EditPhotoRequest(channel=self.input_channel, photo=photo))
            self.current_photo_url = cover_url
            log.info("Telegram user photo updated")
        except Exception as e:
            if FloodWaitError is not None and isinstance(e, FloodWaitError):
                self.photo_flood_until = time.monotonic() + e.seconds + 5
                log.warning(f"Telegram photo flood wait: {e.seconds}s; lyrics will still be posted")
            else:
                log.warning(f"Telegram user photo: {e}")

    async def _start_track(self, artist: str, title: str, cover_url: Optional[str]):
        track_key = f"{artist}|{title}"
        if track_key != self.current_track_key:
            await self._delete_track_messages(include_player=True)
            self.current_track_key = track_key
        await self._set_personal_channel(True)
        if self.metadata_client:
            await self.metadata_client._set_title(artist, title)
            await self.metadata_client._set_photo(cover_url)
        else:
            await self._set_title(artist, title)
            await self._set_photo(cover_url)

    async def _send_line(self, text: str):
        text = text.strip()[:4096]
        if not text or text == self.current_text:
            return
        if not await self._connect():
            return
        message = await self.client.send_message(self.entity, text, silent=True)
        self.track_message_ids.append(int(message.id))
        self.current_text = text
        log.info(f"Telegram user line -> {text!r}")

    async def _upsert_player(self, artist: str, title: str, position: float, duration: float):
        if duration <= 0:
            return
        await self._start_track(artist, title, None)
        text = format_player_text(position, duration)
        if text == self.current_player_text:
            if not self.player_pinned:
                await self._pin_player()
            return
        if not await self._connect():
            return

        if not self.player_message_id:
            message = await self.client.send_message(self.entity, text, silent=True)
            self.player_message_id = int(message.id)
            self.player_pinned = False
            self.current_player_text = text
            log.info(f"Telegram user player -> {text!r}")
            await self._pin_player()
            return

        await self.client.edit_message(self.entity, self.player_message_id, text)
        self.current_player_text = text
        log.info(f"Telegram user player -> {text!r}")
        if not self.player_pinned:
            await self._pin_player()

    async def update(self, artist: str, title: str, line: str, cover_url: Optional[str]):
        if not self.enabled:
            return
        await self._start_track(artist, title, cover_url)
        await self._send_line(line or title)

    async def clear_track(self):
        if not self.enabled:
            return
        await self._delete_track_messages()
        self.current_title = ""
        await self._set_personal_channel(False, force=True)

    async def reset_track_messages(self):
        if not self.enabled:
            return
        await self._delete_track_messages()

    def enqueue_update(self, artist: str, title: str, line: str, cover_url: Optional[str]):
        if not self.enabled or self._flood_active():
            return
        text = (line or title).strip()[:4096]
        track_key = f"{artist}|{title}"
        if track_key == self.queued_track_key and text and text == self.queued_text:
            return
        if track_key == self.current_track_key and text and text == self.current_text:
            return
        self.queued_track_key = track_key
        self.queued_text = text
        self.start_worker()
        if self.queue is not None:
            self.queue.put_nowait(("update", artist, title, text, cover_url))
            log.info(f"Telegram user queued update size={self.queue.qsize()}")

    def enqueue_player(self, artist: str, title: str, position: float, duration: float):
        if not self.enabled or duration <= 0 or self._flood_active():
            return
        track_key = f"{artist}|{title}"
        text = format_player_text(position, duration)
        now = time.monotonic()
        if track_key == self.queued_player_track_key and text == self.queued_player_text:
            return
        if track_key == self.current_track_key and text == self.current_player_text and self.player_pinned:
            return
        if self.player_message_id and now - self.last_player_enqueue < TELEGRAM_PLAYER_UPDATE_INTERVAL:
            return
        self.queued_player_track_key = track_key
        self.queued_player_text = text
        self.last_player_enqueue = now
        self.start_worker()
        if self.queue is not None:
            self._drop_pending_player_commands()
            self.queue.put_nowait(("player", artist, title, position, duration))
            log.info(f"Telegram user queued player size={self.queue.qsize()}")

    def enqueue_clear(self):
        if not self.enabled or self._flood_active():
            return
        self.start_worker()
        if self.queue is not None:
            self._drop_pending_commands()
            self.queue.put_nowait(("clear",))
            log.info(f"Telegram user queued clear size={self.queue.qsize()}")

    def enqueue_reset_track_messages(self):
        if not self.enabled or self._flood_active():
            return
        self.start_worker()
        if self.queue is not None:
            self.queue.put_nowait(("reset",))

    def _drop_pending_commands(self):
        if self.queue is None:
            return
        dropped_count = 0
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                dropped_count += 1
            except asyncio.QueueEmpty:
                break
        if dropped_count:
            log.info(f"Telegram user pending commands dropped: {dropped_count}")

    def _drop_pending_player_commands(self):
        if self.queue is None:
            return
        kept = []
        dropped_count = 0
        while True:
            try:
                command = self.queue.get_nowait()
                self.queue.task_done()
                if command and command[0] == "player":
                    dropped_count += 1
                else:
                    kept.append(command)
            except asyncio.QueueEmpty:
                break
        for command in kept:
            self.queue.put_nowait(command)
        if dropped_count:
            log.info(f"Telegram user pending player commands dropped: {dropped_count}")

    async def _worker(self):
        if self.queue is None:
            return
        while True:
            command = await self.queue.get()
            try:
                action = command[0]
                if self._flood_active():
                    continue
                log.info(f"Telegram user worker action: {action}")
                if action == "update":
                    _, artist, title, line, cover_url = command
                    await self.update(artist, title, line, cover_url)
                elif action == "player":
                    _, artist, title, position, duration = command
                    await self._upsert_player(artist, title, position, duration)
                elif action == "clear":
                    await self.clear_track()
                elif action == "reset":
                    await self.reset_track_messages()
            except Exception as e:
                if FloodWaitError is not None and isinstance(e, FloodWaitError):
                    self._set_flood_wait(e.seconds)
                    continue
                log.warning(f"Telegram user worker: {e}")
            finally:
                self.queue.task_done()


def create_telegram_client():
    if TELEGRAM_MODE in ("off", "none", "disabled", "false", "0"):
        log.info("Telegram updates disabled by config")
        return NullTelegramClient()
    if TELEGRAM_MODE == "user":
        metadata_client = None
        metadata_chat_id = TELEGRAM_CHAT_ID or TELEGRAM_CHANNEL
        if TELEGRAM_BOT_TOKEN and metadata_chat_id:
            metadata_client = TelegramChannelClient(TELEGRAM_BOT_TOKEN, metadata_chat_id)
        return TelegramUserChannelClient(
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
            TELEGRAM_PHONE,
            TELEGRAM_CHANNEL,
            TELEGRAM_PERSONAL_CHANNEL,
            metadata_client,
        )
    return TelegramChannelClient(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)


class NullTelegramClient:
    def start_worker(self):
        return

    def enqueue_update(self, *args, **kwargs):
        return

    def enqueue_player(self, *args, **kwargs):
        return

    def enqueue_clear(self):
        return

    def enqueue_reset_track_messages(self):
        return



def get_current_lines(lines: list[tuple[float, str]], position: float) -> tuple[str, Optional[str]]:
    position += LYRIC_LOOKAHEAD
    if position < lines[0][0]:
        position = lines[0][0]
    if position > lines[-1][0]:
        position = lines[-1][0]

    current_index = 0
    for index, (timestamp, text) in enumerate(lines):
        if position >= timestamp:
            current_index = index
        else:
            break

    current = lines[current_index][1]
    next_line = lines[current_index + 1][1] if current_index + 1 < len(lines) else None
    return current, next_line


async def load_track_data(artist: str, title: str) -> tuple[list[tuple[float, str]], Optional[str]]:
    return await asyncio.gather(
        get_lyrics(artist, title),
        get_cover_url(artist, title),
    )


async def main():
    acquire_single_instance()
    dc = DiscordActivityClient(DISCORD_CLIENT_ID) if DISCORD_ENABLED else NullDiscordActivityClient()
    if DISCORD_ENABLED:
        log.info("Discord activity updates enabled")
    else:
        log.info("Discord activity updates disabled by config")
    tg = create_telegram_client()
    tg.start_worker()
    current_key: Optional[str] = None
    current_app_name = "Музыку"
    lines: list[tuple[float, str]] = []
    cover_url: Optional[str] = None
    track_data_task: Optional[asyncio.Task] = None
    none_count = 0
    prev_position = -999.0
    base_position = 0.0
    base_time = 0.0
    duration = 0.0
    force_activity_update = False
    last_stall_log = 0.0
    cleared_paused_key: Optional[str] = None

    log.info("Started - waiting for Yandex Music...")

    while True:
        state = await get_smtc_state()

        if state is None:
            none_count += 1
            if none_count >= 5 and current_key is not None:
                log.info("Player session lost - clearing activity")
                if track_data_task and not track_data_task.done():
                    track_data_task.cancel()
                track_data_task = None
                current_key = None
                current_app_name = "Музыку"
                lines = []
                cover_url = None
                duration = 0.0
                none_count = 0
                prev_position = -999.0
                force_activity_update = False
                cleared_paused_key = None
                await dc.clear_status()
                tg.enqueue_clear()

            if lines and current_key is not None:
                elapsed = time.monotonic() - base_time
                cur_pos = base_position + elapsed + TIME_OFFSET
                line, next_line = get_current_lines(lines, cur_pos)
                artist, title = current_key.split("|", 1)
                await dc.set_activity(
                    artist,
                    title,
                    line,
                    next_line,
                    cur_pos,
                    cover_url,
                    current_app_name,
                    force=force_activity_update,
                )
                tg.enqueue_player(artist, title, cur_pos, duration)
                tg.enqueue_update(artist, title, line, cover_url)
                force_activity_update = False
        else:
            none_count = 0
            artist = state["artist"]
            title = state["title"]
            position = state["position"]
            duration = state["duration"]
            playing = state["playing"]
            app_name = state["app_name"]
            key = f"{artist}|{title}"

            if not playing and current_key is None and cleared_paused_key == key:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if key != current_key:
                log.info(f"New track: {artist} - {title} ({app_name})")
                if track_data_task and not track_data_task.done():
                    track_data_task.cancel()
                current_key = key
                current_app_name = app_name
                lines = []
                cover_url = None
                prev_position = -999.0
                base_position = position
                base_time = time.monotonic()
                force_activity_update = True
                track_data_task = asyncio.create_task(load_track_data(artist, title))
            elif app_name != current_app_name:
                log.info(f"Player app changed: {current_app_name} -> {app_name}")
                current_app_name = app_name

            if track_data_task and track_data_task.done():
                try:
                    lines, cover_url = track_data_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    log.warning(f"Track data: {e}")
                track_data_task = None

            expected_position = base_position + (time.monotonic() - base_time)
            position_delta = position - expected_position
            raw_delta = position - prev_position if prev_position != -999.0 else 0.0
            raw_seek = prev_position != -999.0 and abs(raw_delta) >= SEEK_RAW_THRESHOLD
            if raw_seek and abs(position_delta) >= SEEK_THRESHOLD:
                log.info(
                    f"Seek detected: expected={expected_position:.1f}s actual={position:.1f}s "
                    f"delta={position_delta:+.1f}s raw={prev_position:.1f}s->{position:.1f}s"
                )
                base_position = position
                base_time = time.monotonic()
                force_activity_update = True
                tg.enqueue_reset_track_messages()
            elif raw_delta > 0.05:
                base_position = position
                base_time = time.monotonic()
            elif prev_position != -999.0 and abs(position_delta) >= SEEK_THRESHOLD:
                now = time.monotonic()
                if now - last_stall_log >= 5.0:
                    log.info(
                        f"SMTC position stalled: expected={expected_position:.1f}s actual={position:.1f}s "
                        f"delta={position_delta:+.1f}s raw_delta={raw_delta:+.1f}s"
                    )
                    last_stall_log = now
            prev_position = position

            if not playing:
                if current_key is not None and cleared_paused_key != current_key:
                    log.info("Paused - clearing activity")
                    if track_data_task and not track_data_task.done():
                        track_data_task.cancel()
                    track_data_task = None
                    cleared_paused_key = current_key
                    current_key = None
                    current_app_name = "Музыку"
                    lines = []
                    cover_url = None
                    duration = 0.0
                    prev_position = -999.0
                    force_activity_update = False
                    await dc.clear_status()
                    tg.enqueue_clear()
            else:
                cleared_paused_key = None
                elapsed = time.monotonic() - base_time
                cur_pos = base_position + elapsed + TIME_OFFSET
                if lines:
                    line, next_line = get_current_lines(lines, cur_pos)
                    await dc.set_activity(
                        artist,
                        title,
                        line,
                        next_line,
                        cur_pos,
                        cover_url,
                        current_app_name,
                        force=force_activity_update,
                    )
                    tg.enqueue_player(artist, title, cur_pos, duration)
                    tg.enqueue_update(artist, title, line, cover_url)
                    force_activity_update = False
                else:
                    await dc.set_activity(
                        artist,
                        title,
                        title,
                        None,
                        cur_pos,
                        cover_url,
                        current_app_name,
                        force=force_activity_update,
                    )
                    tg.enqueue_player(artist, title, cur_pos, duration)
                    tg.enqueue_update(artist, title, title, cover_url)
                    force_activity_update = False

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
