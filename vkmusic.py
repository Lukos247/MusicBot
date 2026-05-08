"""VK Music — the bot's only audio source.

video_id format: VK's canonical "{owner_id}_{audio_id}" string —
e.g. "12345_67890" or "-2000000000_67890" for groups. Stable across
re-fetches and fits the existing SQLite cache (TEXT primary key).

Networking:
- Direct calls to https://api.vk.com/method (NOT the m.vk.com web
  scraping that vk_api.audio.VkAudio does — that path crashes with
  IndexError on a token-only auth).
- The token MUST come from the direct-authorization flow
  (oauth.vk.com/token, grant_type=password, Kate Mobile client_id +
  client_secret). Implicit-flow tokens from vkhost.github.io get
  rejected with `error 3: Unknown method passed` on audio.search
  since VK's mid-2025 tightening — see scripts/get_vk_audio_token.py.
- The Kate Mobile User-Agent must accompany every request — VK uses
  it as a signal that the call is allowed to reach the audio API.
- ffmpeg downloads the resulting URL (mp3 or HLS m3u8 — both work).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

import requests

from config import DOWNLOADS_DIR, MAX_AUDIO_BYTES, VK_TOKEN

log = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    """Raised when the downloaded audio exceeds the bot's upload limit
    (Telegram caps regular bots at 50 MB)."""

    def __init__(self, size: int) -> None:
        super().__init__(f"File too large: {size} bytes")
        self.size = size


@dataclass(slots=True)
class TrackMeta:
    """Search-result metadata. video_id is VK's canonical
    "{owner_id}_{audio_id}" string."""

    video_id: str
    title: str
    performer: str
    duration: int


@dataclass(slots=True)
class DownloadedTrack:
    path: Path
    title: str
    performer: str
    duration: int

_VK_API_BASE = "https://api.vk.com/method"
_VK_API_VERSION = "5.131"
# Kate Mobile UA — pairs with the Kate-Mobile-flow token to get audio API access.
_VK_UA = (
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; "
    "x86; unknown Android SDK built for x86; en)"
)


def _diag(msg: str) -> None:
    """Module-import-time visibility — log.* may be swallowed before
    main() runs basicConfig."""
    print(f"[vkmusic] {msg}", file=sys.stderr, flush=True)


def is_configured() -> bool:
    return bool(VK_TOKEN)


def _vk_call(method: str, **params: Any) -> dict | None:
    """Synchronous VK API call. Returns the `response` dict on success,
    None on error (already logged)."""
    if not VK_TOKEN:
        return None
    full_params = dict(params)
    full_params["access_token"] = VK_TOKEN
    full_params["v"] = _VK_API_VERSION
    try:
        resp = requests.get(
            f"{_VK_API_BASE}/{method}",
            params=full_params,
            headers={"User-Agent": _VK_UA},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("VK %s network error: %s", method, e)
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("VK %s returned non-JSON: %s", method, resp.text[:200])
        return None
    if "error" in data:
        err = data["error"]
        log.warning("VK %s API error %s: %s", method, err.get("error_code"), err.get("error_msg"))
        return None
    return data.get("response")


def _to_track_meta(item: dict) -> TrackMeta:
    return TrackMeta(
        video_id=f"{item['owner_id']}_{item['id']}",
        title=(item.get("title") or "").strip() or "Unknown",
        performer=(item.get("artist") or "").strip() or "Unknown",
        duration=int(item.get("duration") or 0),
    )


def _search_blocking(query: str, limit: int) -> list[TrackMeta]:
    response = _vk_call("audio.search", q=query, count=limit, auto_complete=1)
    if response is None:
        return []
    items = response.get("items") or []
    log.info("VK audio.search %r → %d items", query, len(items))
    return [_to_track_meta(item) for item in islice(items, limit)]


async def search(query: str, limit: int) -> list[TrackMeta]:
    query = query.strip()
    if not query:
        return []
    return await asyncio.to_thread(_search_blocking, query, limit)


_VIDEO_ID_RE = re.compile(r"^(-?\d+)_(\d+)$")


def _parse_video_id(video_id: str) -> tuple[int, int] | None:
    m = _VIDEO_ID_RE.match(video_id)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _fetch_audio(owner_id: int, audio_id: int) -> dict | None:
    """audio.getById refreshes URL — VK URLs expire after a few hours."""
    response = _vk_call("audio.getById", audios=f"{owner_id}_{audio_id}")
    if response is None:
        return None
    if isinstance(response, list):
        return response[0] if response else None
    return response


def _download_blocking(video_id: str) -> DownloadedTrack:
    parsed = _parse_video_id(video_id)
    if parsed is None:
        raise RuntimeError(f"Bad VK audio id: {video_id!r}")
    owner_id, audio_id = parsed

    item = _fetch_audio(owner_id, audio_id)
    if not item:
        raise RuntimeError(f"VK audio not found: {video_id}")

    url = item.get("url")
    if not url:
        # Some tracks don't expose a URL even with Kate token (DRM /
        # geo-restricted / removed). Caller will surface this as a generic
        # download-failed message to the user.
        raise RuntimeError(f"VK audio has no playable URL: {video_id}")

    title = (item.get("title") or "").strip() or "Unknown"
    artist = (item.get("artist") or "").strip() or "Unknown"
    duration = int(item.get("duration") or 0)

    output = DOWNLOADS_DIR / f"vk_{owner_id}_{audio_id}.m4a"
    if output.exists():
        try:
            output.unlink()
        except OSError:
            pass

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-nostdin",
        "-i", url,
        "-vn",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out downloading {video_id}")

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:400].strip()
        raise RuntimeError(f"ffmpeg failed for {video_id}: {err}")
    if not output.exists():
        raise RuntimeError(f"ffmpeg ran but output missing for {video_id}")

    size = output.stat().st_size
    if size > MAX_AUDIO_BYTES:
        try:
            output.unlink()
        except OSError:
            pass
        raise FileTooLargeError(size)

    return DownloadedTrack(
        path=output,
        title=title,
        performer=artist,
        duration=duration,
    )


async def download(video_id: str) -> DownloadedTrack:
    return await asyncio.to_thread(_download_blocking, video_id)


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Failed to remove file %s", path)


# Module-load smoke test — confirms token presence and emits a hint that
# audio.search will be tried. Actual API call happens lazily on first
# search to avoid blocking startup on a slow VK request.
if VK_TOKEN:
    _diag(f"VK_TOKEN present ({len(VK_TOKEN)} chars), audio.search ready")
else:
    _diag("VK_TOKEN absent, vkmusic disabled")
