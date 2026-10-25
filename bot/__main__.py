import asyncio
import logging

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .chain import BscClient
from .config import load_config
from .db import Database
from .handlers import router
from .tasks import backfill_loop, housekeeping_loop, scanner_loop


class OwnerOnly(BaseMiddleware):
    """Бот отвечает только владельцу, всех остальных молча игнорирует."""

    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            return None
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    db = await Database.connect(cfg.database_url)
    await db.init()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["cfg"] = cfg
    dp.update.outer_middleware(OwnerOnly(cfg.owner_id))
    dp.include_router(router)

    await bot.set_my_commands([BotCommand(command="start", description="Меню и справка")])

    session = aiohttp.ClientSession()
    client = BscClient(session, cfg.rpc_urls)
    background = [
        asyncio.create_task(scanner_loop(bot, db, cfg, client)),
        asyncio.create_task(housekeeping_loop(bot, db, cfg)),
        asyncio.create_task(backfill_loop(bot, db, cfg, client)),
    ]
    logging.info("Бот запущен, слежу за кошельком %s", cfg.wallet)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in background:
            t.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await session.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
