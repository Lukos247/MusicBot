"""VK Music as a primary audio source.

Mirrors the public surface of `youtube.py` (TrackMeta, DownloadedTrack,
search, download, cleanup, FileTooLargeError) so handlers can call either
module interchangeably.

The video_id field uses VK's standard "{owner_id}_{audio_id}" identifier
(owner_id is negative for groups). This is stable across re-fetches and
fits cleanly into the existing SQLite cache (TEXT primary key).

Networking notes:
- vk_api.audio.VkAudio scrapes m.vk.com under the hood — the public VK
  API removed audio endpoints in 2017. We pass a Kate-Mobile-style token
  obtained via vkhost.github.io.
- Some tracks are served as HLS m3u8; vk_api converts those to direct
  mp3 URLs by default. ffmpeg handles both transparently anyway.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from itertools import islice
from pathlib import Path

import vk_api
from vk_api.audio import VkAudio

from config import DOWNLOADS_DIR, MAX_AUDIO_BYTES, VK_TOKEN
# Reuse the canonical types from youtube.py so handlers and the source
# router don't have to distinguish — TrackMeta/DownloadedTrack are a
# common shape, and FileTooLargeError must be a single class for the
# `except FileTooLargeError` clauses in handlers/common.py to catch both.
from youtube import DownloadedTrack, FileTooLargeError, TrackMeta

log = logging.getLogger(__name__)


_session: vk_api.VkApi | None = None
_audio: VkAudio | None = None


def _get_audio() -> VkAudio | None:
    """Lazy-init the VkAudio scraper. Returns None if VK_TOKEN is not set."""
    global _session, _audio
    if not VK_TOKEN:
        return None
    if _audio is None:
        try:
            _session = vk_api.VkApi(token=VK_TOKEN)
            _audio = VkAudio(_session)
            log.info("VkAudio initialised, user_id=%s", _audio.user_id)
        except Exception:
            log.exception("VkAudio initialisation failed — VK_TOKEN invalid?")
            _audio = None
    return _audio


def _to_track_meta(item: dict) -> TrackMeta:
    return TrackMeta(
        video_id=f"{item['owner_id']}_{item['id']}",
        title=(item.get("title") or "").strip() or "Unknown",
        performer=(item.get("artist") or "").strip() or "Unknown",
        duration=int(item.get("duration") or 0),
    )


def _search_blocking(query: str, limit: int) -> list[TrackMeta]:
    audio = _get_audio()
    if audio is None:
        return []
    try:
        # search() returns an islice iterator; cap explicitly
        return [_to_track_meta(item) for item in islice(audio.search(q=query, count=limit), limit)]
    except Exception:
        log.exception("VK audio search failed for %r", query)
        return []


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


def _safe_filename_part(text: str) -> str:
    cleaned = "".join(c for c in text if c.isalnum() or c in " -_().,'").strip()
    return cleaned[:50]


def _download_blocking(video_id: str) -> DownloadedTrack:
    audio = _get_audio()
    if audio is None:
        raise RuntimeError("VK_TOKEN not configured")

    parsed = _parse_video_id(video_id)
    if parsed is None:
        raise RuntimeError(f"Bad VK audio id: {video_id!r}")
    owner_id, audio_id = parsed

    # Re-fetch to get a fresh signed URL — VK URLs expire after a few hours.
    item = audio.get_audio_by_id(owner_id, audio_id)
    if not item or isinstance(item, list) and not item:
        raise RuntimeError(f"VK audio not found: {video_id}")
    if isinstance(item, list):
        item = item[0]

    url = item.get("url")
    if not url:
        # Some tracks have no playable URL (DRM / region-locked / removed).
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

    # ffmpeg reads both direct mp3 and HLS m3u8 transparently.
    # -vn drops any video track, -c:a aac transcodes to AAC m4a.
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


def is_configured() -> bool:
    """Whether VK_TOKEN is set. Use to decide if VK should be tried."""
    return bool(VK_TOKEN)
