from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from config import DOWNLOADS_DIR, MAX_AUDIO_BYTES

log = logging.getLogger(__name__)


def _resolve_ffmpeg_dir() -> str | None:
    """Return a directory containing ffmpeg.exe/ffprobe.exe, or None if it
    is already discoverable via PATH. Falls back to the standard winget
    install location if the binary is not yet on PATH (winget updates the
    user PATH, but existing shells won't see it until restart)."""
    if shutil.which("ffmpeg"):
        return None
    local_app = os.environ.get("LOCALAPPDATA")
    if not local_app:
        return None
    base = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
    if not base.exists():
        return None
    for ffmpeg_exe in base.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe"):
        if (ffmpeg_exe.parent / "ffprobe.exe").exists():
            return str(ffmpeg_exe.parent)
    return None


_FFMPEG_DIR = _resolve_ffmpeg_dir()
if _FFMPEG_DIR:
    log.info("Using bundled ffmpeg from %s", _FFMPEG_DIR)


# Datacenter IPs (Fly, Render, Railway, ...) often trip YouTube's "confirm
# you're not a bot" challenge against the default `web` player client. Pin
# yt-dlp to mobile/tv clients which still go through.
_YT_PLAYER_CLIENTS = ["default", "android", "ios", "tv"]


def _resolve_cookie_file() -> str | None:
    """If YT_COOKIES env var is set with the contents of a Netscape-format
    cookies.txt, drop it on disk so yt-dlp can read it. Returns the path or
    None if no cookies were provided."""
    raw = os.environ.get("YT_COOKIES", "")
    if not raw.strip():
        return None
    target = Path("/tmp/yt_cookies.txt") if os.name != "nt" else Path(os.environ.get("TEMP", ".")) / "yt_cookies.txt"
    try:
        target.write_text(raw, encoding="utf-8")
    except OSError:
        log.exception("failed to write YT_COOKIES to %s", target)
        return None
    log.info("Using YouTube cookies from YT_COOKIES env var (%s)", target)
    return str(target)


_YT_COOKIE_FILE = _resolve_cookie_file()

_TITLE_NOISE = re.compile(
    r"\s*[\(\[]\s*(official\s*(music\s*)?video|official\s*audio|lyrics?|"
    r"audio|video|hd|4k|m/v|mv|live|remastered|visualizer)\s*[\)\]]\s*",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TrackMeta:
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


def _split_title(raw_title: str, uploader: str) -> tuple[str, str]:
    cleaned = _TITLE_NOISE.sub(" ", raw_title or "").strip(" -–—")
    for sep in (" - ", " – ", " — "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left
    performer = (uploader or "").removesuffix(" - Topic").strip()
    return cleaned or (raw_title or "Unknown"), performer or "Unknown"


def _search_blocking(query: str, limit: int) -> list[TrackMeta]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": f"ytsearch{limit}",
        "noplaylist": True,
        "socket_timeout": 10,
        "extractor_args": {"youtube": {"player_client": _YT_PLAYER_CLIENTS}},
    }
    if _YT_COOKIE_FILE:
        opts["cookiefile"] = _YT_COOKIE_FILE
    results: list[TrackMeta] = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except DownloadError as e:
        log.warning("yt-dlp search failed for %r: %s", query, e)
        return []
    except Exception:
        log.exception("Unexpected error during yt-dlp search for %r", query)
        return []

    entries = (info or {}).get("entries") or []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        raw_title = entry.get("title") or ""
        uploader = entry.get("uploader") or entry.get("channel") or ""
        duration = int(entry.get("duration") or 0)
        title, performer = _split_title(raw_title, uploader)
        results.append(
            TrackMeta(
                video_id=video_id,
                title=title,
                performer=performer,
                duration=duration,
            )
        )
    return results


async def search(query: str, limit: int) -> list[TrackMeta]:
    query = query.strip()
    if not query:
        return []
    return await asyncio.to_thread(_search_blocking, query, limit)


def _download_blocking(video_id: str) -> DownloadedTrack:
    out_template = str(DOWNLOADS_DIR / "%(id)s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            }
        ],
        "prefer_ffmpeg": True,
        "extractor_args": {"youtube": {"player_client": _YT_PLAYER_CLIENTS}},
    }
    if _FFMPEG_DIR:
        opts["ffmpeg_location"] = _FFMPEG_DIR
    if _YT_COOKIE_FILE:
        opts["cookiefile"] = _YT_COOKIE_FILE
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp returned no info")

    raw_title = info.get("title") or ""
    uploader = info.get("uploader") or info.get("channel") or ""
    duration = int(info.get("duration") or 0)
    title, performer = _split_title(raw_title, uploader)

    final_path = DOWNLOADS_DIR / f"{video_id}.m4a"
    if not final_path.exists():
        for candidate in DOWNLOADS_DIR.glob(f"{video_id}.*"):
            final_path = candidate
            break
    if not final_path.exists():
        raise RuntimeError("Downloaded file not found on disk")

    size = final_path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        try:
            final_path.unlink()
        except OSError:
            pass
        raise FileTooLargeError(size)

    return DownloadedTrack(
        path=final_path,
        title=title,
        performer=performer,
        duration=duration,
    )


class FileTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"File too large: {size} bytes")
        self.size = size


async def download(video_id: str) -> DownloadedTrack:
    return await asyncio.to_thread(_download_blocking, video_id)


def cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Failed to remove file %s", path)
