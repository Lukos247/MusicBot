"""Yandex Music as the bot's audio source.

Uses the user's Yandex.Music access token (`YANDEX_TOKEN`) obtained via
OAuth implicit flow at oauth.yandex.ru. Requires an active Yandex Plus
subscription on that account for streaming/download to work.

video_id format: stringified Yandex track id (numeric, e.g. "12345").
Stable across re-fetches and short enough for Telegram's 64-byte
inline result_id cap.

The yandex-music library provides ClientAsync — same async surface
as our existing aiogram code. ffmpeg is no longer in the download
path; the library writes mp3/aac directly to disk.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from yandex_music import ClientAsync, Track
from yandex_music.exceptions import YandexMusicError

from config import DOWNLOADS_DIR, MAX_AUDIO_BYTES, YANDEX_TOKEN

log = logging.getLogger(__name__)


class FileTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"File too large: {size} bytes")
        self.size = size


@dataclass(slots=True)
class TrackMeta:
    video_id: str  # stringified yandex track id
    title: str
    performer: str
    duration: int


@dataclass(slots=True)
class DownloadedTrack:
    path: Path
    title: str
    performer: str
    duration: int


_client: ClientAsync | None = None
_client_lock = asyncio.Lock()


def is_configured() -> bool:
    return bool(YANDEX_TOKEN)


async def _get_client() -> ClientAsync | None:
    """Lazy-init a single ClientAsync. Yandex's auth.init() makes one
    HTTP call so we only do it once."""
    global _client
    if _client is not None:
        return _client
    if not YANDEX_TOKEN:
        return None
    async with _client_lock:
        if _client is None:
            try:
                client = ClientAsync(YANDEX_TOKEN)
                await client.init()
                _client = client
                me = client.me
                acct = (me.account.display_name if me and me.account else "?")
                log.info("Yandex.Music client ready: account=%s", acct)
            except YandexMusicError as e:
                log.error("Yandex.Music init failed: %s", e)
                return None
            except Exception:
                log.exception("Yandex.Music init crashed")
                return None
    return _client


def _track_artists(track: Track) -> str:
    """Comma-separated artist names, trimmed."""
    if not track.artists:
        return "Unknown"
    names = [a.name for a in track.artists if a.name]
    return ", ".join(names) if names else "Unknown"


def _track_meta(track: Track) -> TrackMeta:
    duration_ms = track.duration_ms or 0
    return TrackMeta(
        video_id=str(track.id),
        title=(track.title or "").strip() or "Unknown",
        performer=_track_artists(track),
        duration=duration_ms // 1000,
    )


async def search(query: str, limit: int) -> list[TrackMeta]:
    """Search Yandex Music for tracks. Falls back to empty list on any
    error (logged); caller treats that as 'no results'."""
    query = query.strip()
    if not query:
        return []
    client = await _get_client()
    if client is None:
        return []
    try:
        result = await client.search(query, type_="track")
    except YandexMusicError as e:
        log.warning("Yandex.Music search failed for %r: %s", query, e)
        return []
    except Exception:
        log.exception("Yandex.Music search crashed for %r", query)
        return []
    if result is None or result.tracks is None:
        log.info("Yandex.Music search %r → no tracks block", query)
        return []
    tracks = (result.tracks.results or [])[:limit]
    log.info("Yandex.Music search %r → %d items", query, len(tracks))
    return [_track_meta(t) for t in tracks]


_VIDEO_ID_RE = re.compile(r"^\d+$")


def _safe_filename(performer: str, title: str) -> str:
    raw = f"{performer} - {title}".strip(" -") or "audio"
    cleaned = "".join(c for c in raw if c.isalnum() or c in " -_().,'") .strip()
    return f"{(cleaned or 'audio')[:80]}.mp3"


async def download(video_id: str) -> DownloadedTrack:
    """Fetch the track by id and download it as mp3 to DOWNLOADS_DIR."""
    if not _VIDEO_ID_RE.match(video_id):
        raise RuntimeError(f"Bad Yandex track id: {video_id!r}")
    client = await _get_client()
    if client is None:
        raise RuntimeError("Yandex.Music client is not configured")

    try:
        # `tracks` returns a list — we asked for one, take the first.
        results = await client.tracks([video_id])
    except YandexMusicError as e:
        raise RuntimeError(f"Yandex.Music tracks() failed for {video_id}: {e}")
    if not results:
        raise RuntimeError(f"Yandex.Music: track {video_id} not found")
    track: Track = results[0]

    title = (track.title or "").strip() or "Unknown"
    performer = _track_artists(track)
    duration = (track.duration_ms or 0) // 1000

    output = DOWNLOADS_DIR / f"ya_{video_id}.mp3"
    if output.exists():
        try:
            output.unlink()
        except OSError:
            pass

    try:
        # yandex-music auto-picks the best codec at the requested bitrate.
        # 192 kbps mp3 is fine for Telegram audio; Plus subscribers get
        # higher options but 192 keeps file size predictable.
        await track.download_async(str(output), codec="mp3", bitrate_in_kbps=192)
    except YandexMusicError as e:
        raise RuntimeError(f"Yandex.Music download failed for {video_id}: {e}")
    except Exception as e:
        raise RuntimeError(f"Yandex.Music download crashed for {video_id}: {e}")

    if not output.exists():
        raise RuntimeError(f"Yandex.Music download produced no file for {video_id}")
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
        performer=performer,
        duration=duration,
    )


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Failed to remove file %s", path)


# Module-load diagnostics — log.* may be swallowed before main() sets
# up basicConfig.
print(
    f"[yandex_music_source] YANDEX_TOKEN: {'present' if YANDEX_TOKEN else 'absent'} "
    f"({len(YANDEX_TOKEN)} chars)" if YANDEX_TOKEN else
    "[yandex_music_source] YANDEX_TOKEN absent — source disabled",
    file=sys.stderr, flush=True,
)
