"""
Конфигурация бота. Загружает значения из переменных окружения (.env).
"""
import os
import logging
from pathlib import Path
from typing import Set

from dotenv import load_dotenv

# Загружаем .env из корня проекта
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_admins(raw: str) -> Set[int]:
    """Парсим список ID администраторов из строки через запятую."""
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                raise ValueError(f"Некорректный ADMIN_ID: {part!r}")
    return result


# --- Основные настройки ---
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в .env")

ADMIN_IDS: Set[int] = _parse_admins(os.getenv("ADMIN_ID", ""))
if not ADMIN_IDS:
    raise RuntimeError("Не задан ADMIN_ID в .env (хотя бы один Telegram ID)")

# --- БД ---
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))

# --- Логи ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR: Path = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE: Path = LOG_DIR / "bot.log"


def setup_logging() -> None:
    """Настройка логирования: вывод в файл и в консоль."""
    log_format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format=log_format,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    # Снижаем шум от сторонних библиотек
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором."""
    return user_id in ADMIN_IDS
