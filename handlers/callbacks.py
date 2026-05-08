from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from .common import deliver_track

router = Router(name="callbacks")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "loading")
async def loading_stub(query: CallbackQuery) -> None:
    await query.answer("Идёт загрузка трека, подожди…", show_alert=False)


@router.callback_query(F.data.startswith("pick:"))
async def pick_track(query: CallbackQuery, bot: Bot) -> None:
    if not query.data or query.message is None:
        await query.answer()
        return

    video_id = query.data.split(":", 1)[1]
    if not video_id:
        await query.answer("Некорректный запрос", show_alert=False)
        return

    await query.answer()
    try:
        await query.message.edit_text("⏳ Скачиваю трек…", reply_markup=None)
    except TelegramBadRequest:
        pass

    await deliver_track(
        bot,
        query.message.chat.id,
        video_id,
        status_msg=query.message,
    )
