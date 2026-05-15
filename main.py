"""
Точка входа: инициализация БД, бота, диспетчера, планировщика.

Запуск:
    python main.py
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, setup_logging
from database import init_db
from handlers import register_all_routers
from utils.scheduler import setup_scheduler, check_expired_access

logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    """Действия при запуске."""
    await init_db()

    # Стартовая проверка истекших доступов
    await check_expired_access(bot)

    # Планировщик
    setup_scheduler(bot)

    me = await bot.get_me()
    logger.info("Бот @%s (id=%s) запущен", me.username, me.id)


async def main() -> None:
    setup_logging()
    logger.info("Старт приложения")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    register_all_routers(dp)
    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал остановки")
