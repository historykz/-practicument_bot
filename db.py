"""
Слой работы с БД (SQLite через aiosqlite).

Все функции принимают/возвращают примитивы и словари (sqlite3.Row),
чтобы их было удобно использовать в хендлерах.
"""
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)


# =========================================================
# ИНИЦИАЛИЗАЦИЯ
# =========================================================
async def init_db() -> None:
    """Создаём таблицы, если их ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER UNIQUE NOT NULL,
                username    TEXT,
                full_name   TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                questions_json  TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS access (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                start_date  TEXT NOT NULL,
                end_date    TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_access_user ON access(user_id);
            CREATE INDEX IF NOT EXISTS idx_access_active ON access(is_active);

            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);

            CREATE TABLE IF NOT EXISTS results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                test_id       INTEGER NOT NULL,
                score         INTEGER NOT NULL,
                total         INTEGER NOT NULL,
                completed_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_results_user ON results(user_id);
            """
        )
        await db.commit()
        logger.info("База данных инициализирована: %s", DB_PATH)


async def get_db() -> aiosqlite.Connection:
    """
    Открыть соединение с БД с включёнными row_factory.
    Использовать как `async with get_db() as db: ...`.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


def _now() -> str:
    """Текущее время в ISO-строке (UTC-aware → naive, секунды)."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


# =========================================================
# USERS
# =========================================================
async def add_user(user_id: int, username: Optional[str], full_name: str) -> None:
    """Добавить пользователя, если его нет; иначе обновить данные."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, username, full_name, _now()),
        )
        await db.commit()


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    """Получить пользователя по Telegram ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()


async def get_all_users(limit: int = 10, offset: int = 0) -> List[aiosqlite.Row]:
    """Постраничный список пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return list(await cur.fetchall())


async def search_users(query: str) -> List[aiosqlite.Row]:
    """Поиск по ID или username (без @)."""
    query = query.strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Если число — ищем по user_id
        if query.isdigit():
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?", (int(query),)
            ) as cur:
                return list(await cur.fetchall())

        # Иначе — поиск по username/full_name
        like = f"%{query}%"
        async with db.execute(
            """
            SELECT * FROM users
            WHERE username LIKE ? OR full_name LIKE ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (like, like),
        ) as cur:
            return list(await cur.fetchall())


async def count_users() -> int:
    """Общее число пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# =========================================================
# ACCESS
# =========================================================
async def grant_access(user_id: int, days: int) -> Tuple[str, str]:
    """
    Выдать доступ пользователю.
    Если у него уже есть активный доступ — продлеваем от его end_date.
    Возвращает (start_date, end_date) в виде ISO-строк.
    """
    now = datetime.now().replace(microsecond=0)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Деактивируем предыдущие активные записи
        async with db.execute(
            "SELECT MAX(end_date) AS end_date FROM access "
            "WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        base = now
        if row and row["end_date"]:
            try:
                existing_end = datetime.fromisoformat(row["end_date"])
                if existing_end > now:
                    base = existing_end
            except ValueError:
                pass

        end_date = base + timedelta(days=days)

        # Деактивируем старые записи и вставляем новую
        await db.execute(
            "UPDATE access SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.execute(
            """
            INSERT INTO access (user_id, start_date, end_date, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (
                user_id,
                now.isoformat(sep=" "),
                end_date.isoformat(sep=" "),
                ),
        )
        await db.commit()

        return now.isoformat(sep=" "), end_date.isoformat(sep=" ")


async def get_active_access(user_id: int) -> Optional[aiosqlite.Row]:
    """Получить активный доступ пользователя (если есть и не истёк)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM access
            WHERE user_id = ? AND is_active = 1
              AND end_date > ?
            ORDER BY end_date DESC
            LIMIT 1
            """,
            (user_id, _now()),
        ) as cur:
            return await cur.fetchone()


async def deactivate_expired() -> List[int]:
    """
    Деактивировать записи, у которых истёк срок.
    Возвращает список user_id, у кого только что закончился доступ.
    """
    now_iso = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT DISTINCT user_id FROM access
            WHERE is_active = 1 AND end_date <= ?
            """,
            (now_iso,),
        ) as cur:
            rows = await cur.fetchall()
        expired_ids = [row["user_id"] for row in rows]

        if expired_ids:
            await db.execute(
                "UPDATE access SET is_active = 0 "
                "WHERE is_active = 1 AND end_date <= ?",
                (now_iso,),
            )
            await db.commit()

        return expired_ids


async def deactivate_user_access(user_id: int) -> None:
    """Принудительно деактивировать все доступы пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE access SET is_active = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def count_active_access() -> int:
    """Количество пользователей с активным неистёкшим доступом."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(DISTINCT user_id) FROM access
            WHERE is_active = 1 AND end_date > ?
            """,
            (_now(),),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# =========================================================
# REQUESTS
# =========================================================
async def create_request(user_id: int) -> int:
    """Создать заявку на покупку. Возвращает её ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO requests (user_id, status, created_at) VALUES (?, 'pending', ?)",
            (user_id, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def get_request(req_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM requests WHERE id = ?", (req_id,)
        ) as cur:
            return await cur.fetchone()


async def get_pending_request_for_user(user_id: int) -> Optional[aiosqlite.Row]:
    """Есть ли уже активная (pending) заявка у пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM requests
            WHERE user_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ) as cur:
            return await cur.fetchone()


async def list_requests(status: Optional[str] = None, limit: int = 20) -> List[aiosqlite.Row]:
    """Список заявок (по статусу или все)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            q = "SELECT * FROM requests WHERE status = ? ORDER BY id DESC LIMIT ?"
            args = (status, limit)
        else:
            q = "SELECT * FROM requests ORDER BY id DESC LIMIT ?"
            args = (limit,)
        async with db.execute(q, args) as cur:
            return list(await cur.fetchall())


async def update_request_status(req_id: int, status: str) -> None:
    """Сменить статус заявки: pending → granted/rejected/contacted."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (status, req_id),
        )
        await db.commit()


async def count_requests() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM requests") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_pending_requests() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# =========================================================
# TESTS
# =========================================================
async def add_test(title: str, questions: list) -> int:
    """
    Сохранить тест.
    `questions` — список словарей:
      [{"q": "Текст вопроса", "options": ["A", "B", ...], "correct": 0}, ...]
    Индекс правильного ответа — 0-based.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tests (title, questions_json, created_at) VALUES (?, ?, ?)",
            (title, json.dumps(questions, ensure_ascii=False), _now()),
        )
        await db.commit()
        return cur.lastrowid


async def get_test(test_id: int) -> Optional[dict]:
    """Получить тест: словарь {id, title, questions, created_at} или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tests WHERE id = ?", (test_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "title": row["title"],
                "questions": json.loads(row["questions_json"]),
                "created_at": row["created_at"],
            }


async def list_tests() -> List[aiosqlite.Row]:
    """Все тесты, новые сверху."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, created_at, questions_json FROM tests ORDER BY id DESC"
        ) as cur:
            return list(await cur.fetchall())


async def delete_test(test_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tests WHERE id = ?", (test_id,))
        await db.commit()


async def count_tests() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM tests") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# =========================================================
# RESULTS
# =========================================================
async def save_result(user_id: int, test_id: int, score: int, total: int) -> None:
    """Сохранить результат прохождения теста."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO results (user_id, test_id, score, total, completed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, test_id, score, total, _now()),
        )
        await db.commit()


async def count_results() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM results") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
