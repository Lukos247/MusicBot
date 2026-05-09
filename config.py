from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "cache.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Copy .env.example to .env and put your bot token there."
    )

_DEFAULT_STORAGE_CHAT_ID = -1003994795691
_storage_raw = os.getenv("STORAGE_CHAT_ID", "").strip()
if _storage_raw:
    try:
        STORAGE_CHAT_ID: int | str = int(_storage_raw)
    except ValueError:
        STORAGE_CHAT_ID = _storage_raw
else:
    STORAGE_CHAT_ID = _DEFAULT_STORAGE_CHAT_ID

VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
# Browser-cookies path (preferred when password flow is flood-blocked).
# VK_COOKIES_B64 = base64-encoded Netscape cookies.txt exported from a
# logged-in vk.com browser session. VK_USER_ID = your numeric VK id
# (visible at vk.com/edit — e.g. 79551234).
VK_COOKIES_B64 = os.getenv("VK_COOKIES_B64", "").strip()
VK_USER_ID = os.getenv("VK_USER_ID", "").strip()

MAX_AUDIO_BYTES = 49 * 1024 * 1024
SEARCH_LIMIT_DM = 5
SEARCH_LIMIT_INLINE = 10
INLINE_CACHE_TIME = 5
INLINE_SEARCH_TIMEOUT = 8.0
