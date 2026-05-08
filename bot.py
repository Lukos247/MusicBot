from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

import cache
from config import BOT_TOKEN
from handlers import build_router


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    await cache.init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(build_router())

    me = await bot.get_me()
    logging.info("Bot started @%s (id=%s)", me.username, me.id)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down")
        sys.exit(0)
