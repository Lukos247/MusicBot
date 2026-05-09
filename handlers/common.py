from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

import cache
import yandex_music_source as audio_source
from config import STORAGE_CHAT_ID
from yandex_music_source import FileTooLargeError

from . import _dedup as dedup

log = logging.getLogger(__name__)


def _safe_filename(performer: str, title: str) -> str:
    raw = f"{performer} - {title}".strip(" -")
    if not raw:
        raw = "audio"
    cleaned = "".join(c for c in raw if c.isalnum() or c in " -_().,'") .strip()
    if not cleaned:
        cleaned = "audio"
    return f"{cleaned[:80]}.m4a"


async def _safe_edit(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass


async def _safe_delete(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


async def materialize_file_id(bot: Bot, video_id: str) -> cache.CachedTrack | None:
    """Ensure that a Telegram file_id exists in the cache for video_id.

    On cache hit returns the CachedTrack. On miss: downloads via VK
    Music + ffmpeg, uploads to STORAGE_CHAT_ID to obtain a bot-scoped
    file_id, saves to the cache, and returns the freshly cached entry.

    Concurrent calls for the same video_id are deduplicated through
    handlers._dedup so only one download/upload happens.

    Raises FileTooLargeError if the audio exceeds the bot upload limit.
    Returns None for other failures (network, VK API, upload).
    """
    cached = await cache.get(video_id)
    if cached is not None:
        return cached

    ev = await dedup.acquire(video_id)
    if ev is None:
        return await cache.get(video_id)

    try:
        try:
            track = await audio_source.download(video_id)
        except FileTooLargeError:
            raise
        except Exception:
            log.exception("vkmusic download failed for %s", video_id)
            return None

        try:
            sent = await bot.send_audio(
                STORAGE_CHAT_ID,
                audio=FSInputFile(track.path, filename=_safe_filename(track.performer, track.title)),
                title=track.title or None,
                performer=track.performer or None,
                duration=track.duration or None,
            )
        except Exception:
            log.exception("upload to STORAGE_CHAT_ID failed for %s", video_id)
            audio_source.cleanup(track.path)
            return None

        if sent.audio is None:
            log.warning("STORAGE_CHAT_ID upload for %s returned no audio object", video_id)
            audio_source.cleanup(track.path)
            return None

        await cache.save(
            video_id,
            sent.audio.file_id,
            track.title,
            track.performer,
            track.duration,
        )
        audio_source.cleanup(track.path)
        return await cache.get(video_id)
    finally:
        dedup.release(video_id, ev)


async def deliver_track(
    bot: Bot,
    chat_id: int,
    video_id: str,
    status_msg: Message | None = None,
) -> bool:
    """Send the track for video_id to chat_id (DM flow).

    Resolves a file_id via materialize_file_id (cache or download+upload),
    then re-sends the audio to chat_id. On success the status message
    (if any) is deleted; on failure it is edited with an error description.
    """
    try:
        cached = await materialize_file_id(bot, video_id)
    except FileTooLargeError as e:
        size_mb = e.size // (1024 * 1024)
        if status_msg:
            await _safe_edit(status_msg, f"❌ Файл слишком большой ({size_mb} MB, лимит 50 MB).")
        return False

    if cached is None:
        if status_msg:
            await _safe_edit(status_msg, "❌ Не удалось скачать трек.")
        return False

    try:
        await bot.send_audio(
            chat_id,
            audio=cached.file_id,
            title=cached.title or None,
            performer=cached.performer or None,
            duration=cached.duration or None,
        )
    except TelegramBadRequest:
        log.exception("send_audio (file_id) failed for %s", video_id)
        if status_msg:
            await _safe_edit(status_msg, "❌ Ошибка при отправке аудио.")
        return False

    if status_msg:
        await _safe_delete(status_msg)
    return True
