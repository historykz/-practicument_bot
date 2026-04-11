import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    SwitchInlineQueryChosenChat,
    Update,
)
from telegram.constants import PollType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    InlineQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()
BUY_CONTACT = os.getenv("BUY_CONTACT", "@your_manager_username").strip()

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения")

if not ADMINS_RAW:
    raise ValueError("Не найден ADMINS в переменных окружения")

ADMINS = {int(x.strip()) for x in ADMINS_RAW.split(",") if x.strip().isdigit()}

DB_PATH = "ent_bot.db"
MIN_TIMER = 5
MAX_TIMER = 600
COUNTDOWN_SECONDS = 3
NO_ANSWER_STREAK_LIMIT = 2

# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# СОСТОЯНИЯ
# =========================================================

ADMIN_MENU = 10

CREATE_TEST_SUBJECT = 20
CREATE_TEST_NAME = 21
CREATE_TEST_ACCESS = 22
CREATE_TEST_TIMER = 23
CREATE_TEST_QUESTIONS = 24

GRANT_ACCESS_SELECT_TEST = 30
GRANT_ACCESS_ENTER_USER_ID = 31

REVOKE_ACCESS_SELECT_TEST = 35
REVOKE_ACCESS_ENTER_USER_ID = 36

EDIT_TEST_SELECT = 40
EDIT_TEST_MENU = 41
EDIT_TEST_NEW_TITLE = 42
EDIT_TEST_NEW_ACCESS = 43
EDIT_TEST_NEW_TIMER = 44
EDIT_TEST_REPLACE_QUESTIONS = 45

DELETE_TEST_SELECT = 50
DELETE_TEST_CONFIRM = 51

GROUP_LAUNCH_SELECT = 70

# =========================================================
# REGEX
# =========================================================

OPTION_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁёІіҚқҢңҒғҮүҰұӨөҺһ]|[A-DА-Г])[\)\.\-:]\s*(.+)$")
LAUNCH_TEXT_RE = re.compile(r"(?:^|\s)(?:@\w+\s+)?launch_test_(\d+)(?:\s|$)", re.IGNORECASE)

# =========================================================
# БАЗА
# =========================================================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column_exists(table_name: str, column_name: str, column_sql: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [row["name"] for row in cur.fetchall()]
    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")
        conn.commit()
    conn.close()


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS telegram_users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        title TEXT NOT NULL,
        access_type TEXT NOT NULL CHECK(access_type IN ('free', 'paid')),
        question_timer INTEGER DEFAULT 30,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        position INTEGER NOT NULL,
        FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL,
        option_text TEXT NOT NULL,
        is_correct INTEGER DEFAULT 0,
        position INTEGER NOT NULL,
        FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_access (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        test_id INTEGER NOT NULL,
        UNIQUE(telegram_id, test_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        test_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

    ensure_column_exists("tests", "question_timer", "question_timer INTEGER DEFAULT 30")


# =========================================================
# DB HELPERS
# =========================================================

def upsert_telegram_user(user) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_users (telegram_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user.id, user.username, user.first_name))
    conn.commit()
    conn.close()


def create_test(subject: str, title: str, access_type: str, created_by: int, question_timer: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tests (subject, title, access_type, question_timer, created_by)
        VALUES (?, ?, ?, ?, ?)
    """, (subject, title, access_type, question_timer, created_by))
    test_id = cur.lastrowid
    conn.commit()
    conn.close()
    return test_id


def update_test_title(test_id: int, new_title: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tests SET title = ? WHERE id = ?", (new_title, test_id))
    conn.commit()
    conn.close()


def update_test_access(test_id: int, new_access: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tests SET access_type = ? WHERE id = ?", (new_access, test_id))
    conn.commit()
    conn.close()


def update_test_timer(test_id: int, new_timer: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tests SET question_timer = ? WHERE id = ?", (new_timer, test_id))
    conn.commit()
    conn.close()


def delete_test(test_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM questions WHERE test_id = ?", (test_id,))
    question_ids = [row["id"] for row in cur.fetchall()]

    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        cur.execute(f"DELETE FROM options WHERE question_id IN ({placeholders})", question_ids)

    cur.execute("DELETE FROM questions WHERE test_id = ?", (test_id,))
    cur.execute("DELETE FROM user_access WHERE test_id = ?", (test_id,))
    cur.execute("DELETE FROM test_results WHERE test_id = ?", (test_id,))
    cur.execute("DELETE FROM tests WHERE id = ?", (test_id,))

    conn.commit()
    conn.close()


def replace_test_questions(test_id: int, parsed_questions: List[Dict[str, Any]]) -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM questions WHERE test_id = ?", (test_id,))
    question_ids = [row["id"] for row in cur.fetchall()]

    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        cur.execute(f"DELETE FROM options WHERE question_id IN ({placeholders})", question_ids)

    cur.execute("DELETE FROM questions WHERE test_id = ?", (test_id,))

    for i, q in enumerate(parsed_questions, start=1):
        cur.execute("""
            INSERT INTO questions (test_id, question_text, position)
            VALUES (?, ?, ?)
        """, (test_id, q["question_text"], i))
        question_id = cur.lastrowid

        for j, opt in enumerate(q["options"], start=1):
            cur.execute("""
                INSERT INTO options (question_id, option_text, is_correct, position)
                VALUES (?, ?, ?, ?)
            """, (question_id, opt["text"], 1 if opt["is_correct"] else 0, j))

    conn.commit()
    conn.close()


def add_question(test_id: int, question_text: str, options: List[Dict[str, Any]], position: int) -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO questions (test_id, question_text, position)
        VALUES (?, ?, ?)
    """, (test_id, question_text, position))
    question_id = cur.lastrowid

    for i, opt in enumerate(options, start=1):
        cur.execute("""
            INSERT INTO options (question_id, option_text, is_correct, position)
            VALUES (?, ?, ?, ?)
        """, (question_id, opt["text"], 1 if opt["is_correct"] else 0, i))

    conn.commit()
    conn.close()


def grant_access(telegram_id: int, test_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO user_access (telegram_id, test_id)
        VALUES (?, ?)
    """, (telegram_id, test_id))
    conn.commit()
    conn.close()


def revoke_access(telegram_id: int, test_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM user_access
        WHERE telegram_id = ? AND test_id = ?
    """, (telegram_id, test_id))
    conn.commit()
    conn.close()


def has_access(telegram_id: int, test_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM user_access WHERE telegram_id = ? AND test_id = ?
    """, (telegram_id, test_id))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def get_subject_tests(subject: str) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tests
        WHERE subject = ?
        ORDER BY id DESC
    """, (subject,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_tests() -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tests ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_test_by_id(test_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tests WHERE id = ?", (test_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_questions_for_test(test_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM questions
        WHERE test_id = ?
        ORDER BY position ASC
    """, (test_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_options_for_question(question_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM options
        WHERE question_id = ?
        ORDER BY position ASC
    """, (question_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def save_result(telegram_id: int, test_id: int, score: int, total: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO test_results (telegram_id, test_id, score, total)
        VALUES (?, ?, ?, ?)
    """, (telegram_id, test_id, score, total))
    conn.commit()
    conn.close()


# =========================================================
# ПАРСИНГ ВОПРОСОВ
# =========================================================

def parse_bulk_questions(raw_text: str) -> List[Dict[str, Any]]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw_text.strip()) if b.strip()]
    result: List[Dict[str, Any]] = []

    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if len(lines) < 3:
            raise ValueError(f"Слишком короткий блок:\n{block}")

        question_lines = []
        options = []

        for line in lines:
            m = OPTION_RE.match(line)
            if m:
                option_text = m.group(2).strip()
                is_correct = option_text.endswith("*")
                option_text = option_text[:-1].strip() if is_correct else option_text
                options.append({
                    "text": option_text,
                    "is_correct": is_correct,
                })
            else:
                question_lines.append(line)

        question_text = "\n".join(question_lines).strip()

        if not question_text:
            raise ValueError(f"Не найден текст вопроса:\n{block}")

        if len(options) < 2:
            raise ValueError(f"У вопроса должно быть минимум 2 варианта:\n{block}")

        correct_count = sum(1 for x in options if x["is_correct"])
        if correct_count != 1:
            raise ValueError(
                f"У каждого вопроса должен быть ровно 1 правильный ответ со звездочкой '*':\n{block}"
            )

        result.append({
            "question_text": question_text,
            "options": options,
        })

    return result


# =========================================================
# УТИЛИТЫ
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def protect_for_user(user_id: int) -> bool:
    return not is_admin(user_id)


def get_user_tag(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or "пользователь"


def subject_code(subject_button_text: str) -> str:
    mapping = {
        "История Казахстана": "history_kz",
        "Биология": "biology",
        "Химия": "chemistry",
        "Математическая грамотность": "math_literacy",
    }
    return mapping.get(subject_button_text, "")


def subject_label(subject_code_value: str) -> str:
    mapping = {
        "history_kz": "История Казахстана",
        "biology": "Биология",
        "chemistry": "Химия",
        "math_literacy": "Математическая грамотность",
    }
    return mapping.get(subject_code_value, subject_code_value)


def is_group_chat(chat_type: Optional[str]) -> bool:
    return chat_type in {"group", "supergroup"}


def get_sessions_store(application: Application) -> Dict[int, Dict[str, Any]]:
    if "quiz_sessions" not in application.bot_data:
        application.bot_data["quiz_sessions"] = {}
    return application.bot_data["quiz_sessions"]


def get_poll_map(application: Application) -> Dict[str, int]:
    if "poll_to_user" not in application.bot_data:
        application.bot_data["poll_to_user"] = {}
    return application.bot_data["poll_to_user"]


def get_group_quiz_store(application: Application) -> Dict[int, Dict[str, Any]]:
    if "group_quiz_sessions" not in application.bot_data:
        application.bot_data["group_quiz_sessions"] = {}
    return application.bot_data["group_quiz_sessions"]


def get_group_poll_map(application: Application) -> Dict[str, Dict[str, Any]]:
    if "group_poll_map" not in application.bot_data:
        application.bot_data["group_poll_map"] = {}
    return application.bot_data["group_poll_map"]


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    mins = total // 60
    secs = total % 60
    return f"{mins} min {secs} sec"


async def safe_delete_message(application: Application, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def build_topics_keyboard(subject: str) -> InlineKeyboardMarkup:
    tests = get_subject_tests(subject)
    keyboard = []

    for t in tests:
        lock = "🔓" if t["access_type"] == "free" else "🔒"
        timer = f"⏱ {t['question_timer']} сек"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{t['title']} {lock} | {timer}",
                callback_data=f"open_test:{t['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)


def build_tests_inline_keyboard(prefix: str) -> InlineKeyboardMarkup:
    tests = get_all_tests()
    keyboard = []

    for t in tests:
        keyboard.append([
            InlineKeyboardButton(
                text=f"#{t['id']} | {subject_label(t['subject'])} | {t['title']}",
                callback_data=f"{prefix}:{t['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_inline")])
    return InlineKeyboardMarkup(keyboard)


def build_group_lobby_text(test: sqlite3.Row, questions_count: int, ready_count: int = 0) -> str:
    extra = f"\n👥 Готовы участников: {ready_count}\n" if ready_count else "\n"
    return (
        f"🎲 Приготовьтесь пройти тест «{test['title']}»\n\n"
        f"🖊 {questions_count} вопросов\n"
        f"⏱ {int(test['question_timer'])} секунд на вопрос\n"
        f"📰 Ответы видны участникам группы и автору теста"
        f"{extra}\n"
        f"🏁 Вопросы появятся, когда хотя бы 2 человека будут готовы отвечать.\n"
        f"Чтобы остановить тест, отправьте /stop"
    )


def build_group_lobby_keyboard(test_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пройти тест", callback_data=f"group_join_quiz:{test_id}")]
    ])


def build_private_card_keyboard(test_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пройти тест", callback_data=f"start_private_test:{test_id}")],
        [InlineKeyboardButton(
            "Отправить в группу",
            switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
                query=f"launch_test_{test_id}",
                allow_user_chats=False,
                allow_bot_chats=False,
                allow_group_chats=True,
                allow_channel_chats=False,
            ),
        )],
        [InlineKeyboardButton("Поделиться", switch_inline_query=f"launch_test_{test_id}")],
    ])


# =========================================================
# ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    upsert_telegram_user(user)

    text = (
        f"Здравствуйте, {get_user_tag(user)}!\n\n"
        "Рад видеть, что вы усердно готовитесь к ЕНТ и нуждаетесь в практике.\n"
        "Не переживайте — здесь вы найдёте много полезных тестов.\n\n"
        f"Ваш Telegram ID: {user.id}\n\n"
        "Выберите предмет для практики:"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu_kb(user.id),
        protect_content=protect_for_user(user.id),
    )
    return ConversationHandler.END


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"Ваш Telegram ID: {user_id}",
        protect_content=protect_for_user(user_id),
    )


async def show_subject_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    user_id = update.effective_user.id
    code = subject_code(text)

    if not code:
        await update.message.reply_text(
            "Пожалуйста, выберите предмет через кнопки.",
            protect_content=protect_for_user(user_id),
        )
        return ConversationHandler.END

    tests = get_subject_tests(code)
    if not tests:
        await update.message.reply_text(
            "По этому предмету пока нет тем.\n\nВыберите другой предмет:",
            reply_markup=main_menu_kb(user_id),
            protect_content=protect_for_user(user_id),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Выберите тему практики:",
        reply_markup=build_topics_keyboard(code),
        protect_content=protect_for_user(user_id),
    )
    return ConversationHandler.END


# =========================================================
# ЛИЧНЫЙ ТЕСТ / КАРТОЧКА
# =========================================================

async def open_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text(
            "Тест не найден.",
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    if test["access_type"] == "paid" and not has_access(query.from_user.id, test_id):
        keyboard = []

        if BUY_CONTACT.startswith("@"):
            keyboard.append([
                InlineKeyboardButton(
                    "💬 Получить доступ",
                    url=f"https://t.me/{BUY_CONTACT.replace('@', '')}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton("⬅️ Назад", callback_data=f"back_to_subject:{test['subject']}")
        ])

        await query.message.reply_text(
            f"🔒 Этот раздел закрыт.\n\nДля получения доступа напишите: {BUY_CONTACT}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    questions = get_questions_for_test(test_id)
    if not questions:
        await query.message.reply_text(
            "В этом тесте пока нет вопросов.",
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    if query.message.chat.type == "private":
        text = (
            f"📚 Тест: {test['title']}\n"
            f"🖊 Вопросов: {len(questions)}\n"
            f"⏱ {int(test['question_timer'])} сек на вопрос\n"
            f"{'🔓 Бесплатный' if test['access_type'] == 'free' else '🔒 Платный'}"
        )
        await query.message.reply_text(
            text,
            reply_markup=build_private_card_keyboard(test_id),
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    await start_private_or_direct_test(
        application=context.application,
        user_id=query.from_user.id,
        chat_id=query.message.chat_id,
        test_id=test_id,
    )


async def start_private_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    await start_private_or_direct_test(
        application=context.application,
        user_id=query.from_user.id,
        chat_id=query.message.chat_id,
        test_id=test_id,
    )


async def start_private_or_direct_test(application: Application, user_id: int, chat_id: int, test_id: int) -> None:
    test = get_test_by_id(test_id)
    questions = get_questions_for_test(test_id)

    if not test or not questions:
        await application.bot.send_message(
            chat_id=chat_id,
            text="Тест не найден или в нём нет вопросов.",
            protect_content=protect_for_user(user_id),
        )
        return

    sessions = get_sessions_store(application)

    if user_id in sessions:
        await finish_user_test(
            application=application,
            user_id=user_id,
            send_result=False,
        )

    sessions[user_id] = {
        "user_id": user_id,
        "chat_id": chat_id,
        "test_id": test["id"],
        "test_title": test["title"],
        "subject": test["subject"],
        "timer": int(test["question_timer"]),
        "questions": [dict(q) for q in questions],
        "current_index": 0,
        "score": 0,
        "active": True,
        "paused": False,
        "current_poll_id": None,
        "current_poll_message_id": None,
        "current_control_message_id": None,
        "current_correct_option_index": None,
        "answered_current": False,
    }

    await application.bot.send_message(
        chat_id=chat_id,
        text=f"▶️ Начинаем тест: {test['title']}\n⏱ Таймер на каждый вопрос: {int(test['question_timer'])} сек",
        protect_content=protect_for_user(user_id),
    )

    await send_next_question(application, user_id)


async def send_next_question(application: Application, user_id: int) -> None:
    sessions = get_sessions_store(application)
    poll_map = get_poll_map(application)

    session = sessions.get(user_id)
    if not session or not session["active"]:
        return

    questions = session["questions"]
    idx = session["current_index"]

    if idx >= len(questions):
        await finish_user_test(
            application=application,
            user_id=user_id,
            send_result=True,
        )
        return

    q = questions[idx]
    options_rows = get_options_for_question(q["id"])

    shuffled = []
    for row in options_rows:
        shuffled.append({
            "text": row["option_text"],
            "is_correct": int(row["is_correct"]) == 1,
        })

    random.shuffle(shuffled)
    option_texts = [x["text"] for x in shuffled]
    correct_index = next(i for i, x in enumerate(shuffled) if x["is_correct"])

    progress_text = f"[{idx + 1}/{len(questions)}] {q['question_text']}"

    poll_message = await application.bot.send_poll(
        chat_id=session["chat_id"],
        question=progress_text,
        options=option_texts,
        type=PollType.QUIZ,
        correct_option_id=correct_index,
        is_anonymous=False,
        open_period=session["timer"],
        protect_content=protect_for_user(user_id),
    )

    session["current_poll_id"] = poll_message.poll.id
    session["current_poll_message_id"] = poll_message.message_id
    session["current_correct_option_index"] = correct_index
    session["answered_current"] = False
    session["paused"] = False

    poll_map[poll_message.poll.id] = user_id

    control_message = await application.bot.send_message(
        chat_id=session["chat_id"],
        text=(
            f"⏱ У вас {session['timer']} сек на ответ.\n"
            "Чтобы остановить тест, нажмите кнопку ниже."
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏹ Завершить тест", callback_data="finish_test_now")]
        ]),
        protect_content=protect_for_user(user_id),
    )
    session["current_control_message_id"] = control_message.message_id

    application.job_queue.run_once(
        quiz_timeout_job,
        when=session["timer"] + 1,
        data={"user_id": user_id, "poll_id": poll_message.poll.id},
        name=f"quiz_timeout_{user_id}_{poll_message.poll.id}",
    )


async def quiz_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    data = context.job.data
    user_id = data["user_id"]
    poll_id = data["poll_id"]

    sessions = get_sessions_store(application)
    session = sessions.get(user_id)

    if not session or not session["active"]:
        return
    if session["current_poll_id"] != poll_id:
        return
    if session["answered_current"]:
        return

    session["paused"] = True
    session["answered_current"] = True
    session["current_index"] += 1

    score = session["score"]
    total = len(session["questions"])
    answered_count = session["current_index"]

    await application.bot.send_message(
        chat_id=session["chat_id"],
        text=(
            "⏰ Время вышло.\n\n"
            f"Текущий результат: {score}/{total}\n"
            f"Пройдено вопросов: {answered_count}/{total}\n\n"
            "Что хотите сделать дальше?"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Продолжить", callback_data="continue_test")],
            [InlineKeyboardButton("⏹ Завершить тест", callback_data="finish_test_now")]
        ]),
        protect_content=protect_for_user(user_id),
    )


async def continue_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    sessions = get_sessions_store(context.application)
    session = sessions.get(query.from_user.id)

    if not session or not session["active"]:
        await query.message.reply_text(
            "Активный тест не найден.",
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    session["paused"] = False
    await query.message.reply_text(
        "▶️ Продолжаем тест.",
        protect_content=protect_for_user(query.from_user.id),
    )
    await send_next_question(context.application, query.from_user.id)


async def finish_test_now_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    await finish_user_test(
        application=context.application,
        user_id=query.from_user.id,
        send_result=True,
    )


async def finish_user_test(
    application: Application,
    user_id: int,
    send_result: bool = True
) -> None:
    sessions = get_sessions_store(application)
    poll_map = get_poll_map(application)

    session = sessions.get(user_id)
    if not session:
        return

    chat_id = session["chat_id"]
    test_id = session["test_id"]
    title = session["test_title"]
    score = session["score"]
    total = len(session["questions"])

    try:
        if session.get("current_poll_message_id"):
            await application.bot.stop_poll(
                chat_id=chat_id,
                message_id=session["current_poll_message_id"]
            )
    except Exception:
        pass

    if session.get("current_poll_id"):
        poll_map.pop(session["current_poll_id"], None)

    if session["current_index"] > 0:
        save_result(user_id, test_id, score, total)

    sessions.pop(user_id, None)

    if not send_result:
        return

    percent = round((score / total) * 100, 1) if total else 0

    await application.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Тест завершён.\n\n"
            f"📚 {title}\n"
            f"🏆 Результат: {score}/{total}\n"
            f"📈 Процент: {percent}%"
        ),
        protect_content=protect_for_user(user_id),
    )

    await application.bot.send_message(
        chat_id=chat_id,
        text="Выберите предмет для практики:",
        reply_markup=main_menu_kb(user_id),
        protect_content=protect_for_user(user_id),
    )


# =========================================================
# INLINE ОТПРАВКА В ГРУППУ
# =========================================================

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    query_text = (inline_query.query or "").strip()

    if not query_text.startswith("launch_test_"):
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    raw_id = query_text.replace("launch_test_", "").strip()
    if not raw_id.isdigit():
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    test_id = int(raw_id)
    test = get_test_by_id(test_id)
    questions = get_questions_for_test(test_id)

    if not test or not questions:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    result = InlineQueryResultArticle(
        id=f"launch-{test_id}",
        title=f"Тест: {test['title']}",
        description=f"{len(questions)} вопросов • {int(test['question_timer'])} сек",
        input_message_content=InputTextMessageContent(
            message_text=f"launch_test_{test_id}\n\n{build_group_lobby_text(test, len(questions))}"
        ),
        reply_markup=build_group_lobby_keyboard(test_id),
    )

    await inline_query.answer([result], cache_time=1, is_personal=True)


async def group_launch_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_group_chat(update.effective_chat.type):
        return

    text = (update.message.text or "").strip()
    match = LAUNCH_TEXT_RE.search(text)
    if not match:
        return

    user_id = update.effective_user.id
    if not is_admin(user_id):
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    test_id = int(match.group(1))
    await create_group_lobby(
        application=context.application,
        chat_id=update.effective_chat.id,
        creator_id=user_id,
        source_message_id=update.message.message_id,
        test_id=test_id,
    )


async def create_group_lobby(
    application: Application,
    chat_id: int,
    creator_id: int,
    source_message_id: Optional[int],
    test_id: int,
) -> bool:
    test = get_test_by_id(test_id)
    questions = get_questions_for_test(test_id)

    if not test or not questions:
        if source_message_id:
            await application.bot.send_message(chat_id=chat_id, text="Тест не найден или в нём нет вопросов.")
        return False

    sessions = get_group_quiz_store(application)

    old = sessions.get(chat_id)
    if old and old.get("active"):
        await application.bot.send_message(chat_id=chat_id, text="В этом чате уже есть активный тест.")
        return False

    sessions[chat_id] = {
        "active": True,
        "started": False,
        "paused": False,
        "chat_id": chat_id,
        "created_by": creator_id,
        "title": test["title"],
        "test_id": test_id,
        "timer": int(test["question_timer"]),
        "questions": [dict(q) for q in questions],
        "current_index": 0,
        "participants": {},
        "announcement_message_id": None,
        "current_poll_id": None,
        "current_poll_message_id": None,
        "current_poll_started_at": None,
        "current_control_message_id": None,
        "no_answer_streak": 0,
        "countdown_message_ids": [],
    }

    msg = await application.bot.send_message(
        chat_id=chat_id,
        text=build_group_lobby_text(test, len(questions), 0),
        reply_markup=build_group_lobby_keyboard(test_id),
        protect_content=False,
    )

    sessions[chat_id]["announcement_message_id"] = msg.message_id

    if source_message_id:
        await safe_delete_message(application, chat_id, source_message_id)

    return True


# =========================================================
# ГРУППОВОЙ ТЕСТ
# =========================================================

async def group_launch_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("У вас нет доступа.", protect_content=False)
        return ConversationHandler.END

    if query.message.chat.type == "private":
        await query.message.reply_text(
            "Эту функцию нужно запускать из группы.\n\nИли откройте тест в личке бота и нажмите «Отправить в группу».",
            protect_content=False,
        )
        return ConversationHandler.END

    test_id = int(query.data.split(":")[1])

    ok = await create_group_lobby(
        application=context.application,
        chat_id=query.message.chat_id,
        creator_id=query.from_user.id,
        source_message_id=None,
        test_id=test_id,
    )

    await query.message.reply_text(
        "✅ Лобби теста создано." if ok else "Не удалось создать лобби теста.",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )
    return ADMIN_MENU


async def group_join_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    chat_id = query.message.chat_id

    test = get_test_by_id(test_id)
    questions = get_questions_for_test(test_id)

    if not test or not questions:
        await query.answer("Тест недоступен.", show_alert=True)
        return

    sessions = get_group_quiz_store(context.application)
    session = sessions.get(chat_id)

    if not session:
        created = await create_group_lobby(
            application=context.application,
            chat_id=chat_id,
            creator_id=query.from_user.id,
            source_message_id=None,
            test_id=test_id,
        )
        if not created:
            await query.answer("Не удалось создать лобби.", show_alert=True)
            return
        session = sessions.get(chat_id)

    if session.get("started") and not session.get("paused"):
        await query.answer("Тест уже начался.", show_alert=True)
        return

    user = query.from_user
    if user.id not in session["participants"]:
        session["participants"][user.id] = {
            "name": f"@{user.username}" if user.username else (user.full_name or "Участник"),
            "score": 0,
            "time_spent": 0.0,
        }

    count = len(session["participants"])

    try:
        await query.message.edit_text(
            text=build_group_lobby_text(test, len(session["questions"]), count),
            reply_markup=build_group_lobby_keyboard(test_id)
        )
    except Exception:
        pass

    if count >= 2 and not session["started"]:
        session["started"] = True
        session["paused"] = False
        session["no_answer_streak"] = 0
        await run_group_countdown(context.application, chat_id)
        await send_next_group_question(context.application, chat_id)


async def run_group_countdown(application: Application, chat_id: int) -> None:
    sessions = get_group_quiz_store(application)
    session = sessions.get(chat_id)
    if not session or not session.get("active"):
        return

    ids = []

    start_msg = await application.bot.send_message(chat_id=chat_id, text="🚀 Достаточно участников. Начинаем тест!")
    ids.append(start_msg.message_id)

    for n in [3, 2, 1]:
        msg = await application.bot.send_message(chat_id=chat_id, text=f"⏳ {n}...")
        ids.append(msg.message_id)
        await asyncio.sleep(1)

    session["countdown_message_ids"] = ids


async def send_next_group_question(application: Application, chat_id: int) -> None:
    sessions = get_group_quiz_store(application)
    poll_map = get_group_poll_map(application)

    session = sessions.get(chat_id)
    if not session or not session.get("active"):
        return
    if session.get("paused"):
        return

    for msg_id in session.get("countdown_message_ids", []):
        await safe_delete_message(application, chat_id, msg_id)
    session["countdown_message_ids"] = []

    idx = session["current_index"]
    questions = session["questions"]

    if idx >= len(questions):
        await finish_group_quiz(application, chat_id)
        return

    q = questions[idx]
    options_rows = get_options_for_question(q["id"])

    shuffled = []
    for row in options_rows:
        shuffled.append({
            "text": row["option_text"],
            "is_correct": int(row["is_correct"]) == 1,
        })

    random.shuffle(shuffled)
    option_texts = [x["text"] for x in shuffled]
    correct_index = next(i for i, x in enumerate(shuffled) if x["is_correct"])

    poll_msg = await application.bot.send_poll(
        chat_id=chat_id,
        question=q["question_text"],
        options=option_texts,
        type=PollType.QUIZ,
        correct_option_id=correct_index,
        is_anonymous=False,
        open_period=session["timer"],
        protect_content=False,
    )

    session["current_poll_id"] = poll_msg.poll.id
    session["current_poll_message_id"] = poll_msg.message_id
    session["current_poll_started_at"] = time.time()

    ctrl = await application.bot.send_message(
        chat_id=chat_id,
        text=f"⏱ На ответ {session['timer']} сек.",
        protect_content=False,
    )
    session["current_control_message_id"] = ctrl.message_id

    poll_map[poll_msg.poll.id] = {
        "chat_id": chat_id,
        "correct_index": correct_index,
        "answered_users": set(),
    }

    application.job_queue.run_once(
        group_advance_question_job,
        when=session["timer"] + 1,
        data={"chat_id": chat_id, "poll_id": poll_msg.poll.id},
        name=f"group_quiz_{chat_id}_{idx}",
    )


async def group_advance_question_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    chat_id = context.job.data["chat_id"]
    poll_id = context.job.data["poll_id"]

    sessions = get_group_quiz_store(application)
    poll_map = get_group_poll_map(application)

    session = sessions.get(chat_id)
    if not session or not session.get("active"):
        return
    if session.get("current_poll_id") != poll_id:
        return

    poll_meta = poll_map.get(poll_id, {})
    answered_users = poll_meta.get("answered_users", set())

    try:
        if session.get("current_poll_message_id"):
            await application.bot.stop_poll(
                chat_id=chat_id,
                message_id=session["current_poll_message_id"]
            )
    except Exception:
        pass

    await safe_delete_message(application, chat_id, session.get("current_control_message_id"))

    if len(answered_users) == 0:
        session["no_answer_streak"] += 1
    else:
        session["no_answer_streak"] = 0

    poll_map.pop(poll_id, None)

    prev_poll_id = session.get("current_poll_message_id")
    prev_ctrl_id = session.get("current_control_message_id")

    session["current_index"] += 1
    session["current_poll_id"] = None
    session["current_poll_message_id"] = None
    session["current_control_message_id"] = None

    total_questions = len(session["questions"])
    answered_count = session["current_index"]

    if session["no_answer_streak"] >= NO_ANSWER_STREAK_LIMIT:
        session["paused"] = True
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏸ Тест поставлен на паузу.\n\n"
                "Два вопроса подряд остались без ответов.\n\n"
                f"Пройдено вопросов: {answered_count}/{total_questions}\n"
                "Выберите действие:"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Продолжить", callback_data=f"group_continue:{chat_id}")],
                [InlineKeyboardButton("⏹ Завершить тест", callback_data=f"group_finish:{chat_id}")],
            ]),
            protect_content=False,
        )
        return

    await safe_delete_message(application, chat_id, prev_poll_id)
    await safe_delete_message(application, chat_id, prev_ctrl_id)
    await send_next_group_question(application, chat_id)


async def continue_group_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = int(query.data.split(":")[1])
    sessions = get_group_quiz_store(context.application)
    session = sessions.get(chat_id)

    if not session or not session.get("active"):
        await query.answer("Активный тест не найден.", show_alert=True)
        return

    session["paused"] = False
    session["no_answer_streak"] = 0

    await run_group_countdown(context.application, chat_id)
    await send_next_group_question(context.application, chat_id)


async def finish_group_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = int(query.data.split(":")[1])
    if not is_admin(query.from_user.id):
        await query.answer("Завершить тест может только админ.", show_alert=True)
        return

    await finish_group_quiz(context.application, chat_id)


async def finish_group_quiz(application: Application, chat_id: int) -> None:
    sessions = get_group_quiz_store(application)
    poll_map = get_group_poll_map(application)

    session = sessions.get(chat_id)
    if not session:
        return

    try:
        if session.get("current_poll_message_id"):
            await application.bot.stop_poll(
                chat_id=chat_id,
                message_id=session["current_poll_message_id"]
            )
    except Exception:
        pass

    await safe_delete_message(application, chat_id, session.get("current_control_message_id"))

    if session.get("current_poll_id"):
        poll_map.pop(session["current_poll_id"], None)

    participants = list(session["participants"].values())
    participants.sort(key=lambda x: (-x["score"], x["time_spent"]))

    total_questions = len(session["questions"])
    lines = [
        f"🏁 Тест «{session['title']}» завершён!",
        "",
        f"🖊 Всего вопросов: {total_questions}",
        ""
    ]

    if not participants:
        lines.append("Пока нет участников.")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for i, p in enumerate(participants, start=1):
            prefix = medals[i - 1] if i <= 3 else f"{i}."
            lines.append(f"{prefix} {p['name']} — {p['score']} ({format_elapsed(p['time_spent'])})")

    await application.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        protect_content=False,
    )

    sessions.pop(chat_id, None)


async def stop_group_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        return

    if not is_admin(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    sessions = get_group_quiz_store(context.application)

    if chat_id not in sessions or not sessions[chat_id].get("active"):
        return

    await finish_group_quiz(context.application, chat_id)


async def group_message_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_group_chat(update.effective_chat.type):
        return

    text = update.message.text or ""

    if LAUNCH_TEXT_RE.search(text):
        return

    user_id = update.effective_user.id
    if is_admin(user_id):
        return

    sessions = get_group_quiz_store(context.application)
    session = sessions.get(update.effective_chat.id)

    if not session or not session.get("active") or not session.get("started") or session.get("paused"):
        return

    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_group_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user
    selected_option_ids = answer.option_ids

    poll_map = get_group_poll_map(context.application)
    poll_meta = poll_map.get(poll_id)
    if not poll_meta:
        return False

    chat_id = poll_meta["chat_id"]
    sessions = get_group_quiz_store(context.application)
    session = sessions.get(chat_id)

    if not session or not session.get("active"):
        return True

    if user.id not in session["participants"]:
        session["participants"][user.id] = {
            "name": f"@{user.username}" if user.username else (user.full_name or "Участник"),
            "score": 0,
            "time_spent": 0.0,
        }

    if user.id in poll_meta["answered_users"]:
        return True

    poll_meta["answered_users"].add(user.id)

    selected_index = selected_option_ids[0] if selected_option_ids else None
    correct_index = poll_meta["correct_index"]

    elapsed = 0.0
    if session.get("current_poll_started_at"):
        elapsed = max(0.0, time.time() - session["current_poll_started_at"])

    session["participants"][user.id]["time_spent"] += elapsed

    if selected_index is not None and selected_index == correct_index:
        session["participants"][user.id]["score"] += 1

    return True


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handled_group = await handle_group_poll_answer(update, context)
    if handled_group:
        return

    answer = update.poll_answer
    user_id = answer.user.id
    poll_id = answer.poll_id
    selected_option_ids = answer.option_ids

    sessions = get_sessions_store(context.application)
    poll_map = get_poll_map(context.application)

    owner_user_id = poll_map.get(poll_id)
    if owner_user_id != user_id:
        return

    session = sessions.get(user_id)
    if not session or not session["active"]:
        return
    if session["current_poll_id"] != poll_id:
        return
    if session["answered_current"]:
        return

    session["answered_current"] = True

    selected_index = selected_option_ids[0] if selected_option_ids else None
    correct_index = session["current_correct_option_index"]

    if selected_index is not None and selected_index == correct_index:
        session["score"] += 1

    session["current_index"] += 1

    try:
        if session.get("current_control_message_id"):
            await context.bot.delete_message(
                chat_id=session["chat_id"],
                message_id=session["current_control_message_id"]
            )
    except Exception:
        pass

    await send_next_question(context.application, user_id)


# =========================================================
# НАЗАД
# =========================================================

async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Выберите предмет для практики:",
        reply_markup=main_menu_kb(query.from_user.id),
        protect_content=protect_for_user(query.from_user.id),
    )


async def back_to_subject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    subject = query.data.split(":", 1)[1]
    await query.message.reply_text(
        "Выберите тему практики:",
        reply_markup=build_topics_keyboard(subject),
        protect_content=protect_for_user(query.from_user.id),
    )


# =========================================================
# АДМИНКА
# =========================================================

def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Создать тест", "Изменить тест"],
            ["Выдать доступ", "Убрать доступ"],
            ["Удалить тест", "Запустить тест в этом чате"],
            ["Назад в меню"],
        ],
        resize_keyboard=True
    )


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа.", protect_content=True)
        return ConversationHandler.END

    await update.message.reply_text(
        "Админ-панель.\nВыберите действие:",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )
    return ADMIN_MENU


async def admin_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text

    if text == "Создать тест":
        await update.message.reply_text(
            "Выберите раздел, куда сохранить тест:",
            reply_markup=create_subject_kb(),
            protect_content=False,
        )
        return CREATE_TEST_SUBJECT

    if text == "Выдать доступ":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=False)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест, к которому нужно выдать доступ:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("grant_test"),
            protect_content=False,
        )
        return GRANT_ACCESS_SELECT_TEST

    if text == "Убрать доступ":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=False)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест, у которого нужно убрать доступ:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("revoke_test"),
            protect_content=False,
        )
        return REVOKE_ACCESS_SELECT_TEST

    if text == "Изменить тест":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=False)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест для редактирования:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("edit_test"),
            protect_content=False,
        )
        return EDIT_TEST_SELECT

    if text == "Удалить тест":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=False)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест для удаления:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("delete_test"),
            protect_content=False,
        )
        return DELETE_TEST_SELECT

    if text == "Запустить тест в этом чате":
        if update.effective_chat.type == "private":
            await update.message.reply_text(
                "Эту функцию нужно запускать из группы.\n\nИли откройте тест в личке бота и нажмите «Отправить в группу».",
                protect_content=False,
            )
            return ADMIN_MENU

        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=False)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест для запуска в этом чате:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("group_launch_test"),
            protect_content=False,
        )
        return GROUP_LAUNCH_SELECT

    if text == "Назад в меню":
        await update.message.reply_text(
            "Главное меню:",
            reply_markup=main_menu_kb(update.effective_user.id),
            protect_content=False,
        )
        return ConversationHandler.END

    await update.message.reply_text("Выберите действие через кнопки.", protect_content=False)
    return ADMIN_MENU


# =========================================================
# СОЗДАНИЕ ТЕСТА
# =========================================================

def access_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Бесплатный", "Платный"],
            ["Назад в меню"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def create_subject_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["История Казахстана", "Биология"],
            ["Химия", "Математическая грамотность"],
            ["Назад в меню"],
        ],
        resize_keyboard=True
    )


def edit_test_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Изменить название", "Изменить доступ"],
            ["Изменить таймер", "Заменить вопросы"],
            ["Назад в админку"],
        ],
        resize_keyboard=True
    )


async def create_test_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "Назад в меню":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=False,
        )
        return ADMIN_MENU

    code = subject_code(text)
    if not code:
        await update.message.reply_text("Выберите раздел через кнопки.", protect_content=False)
        return CREATE_TEST_SUBJECT

    context.user_data["new_test_subject"] = code
    await update.message.reply_text("Введите название темы или теста:", protect_content=False)
    return CREATE_TEST_NAME


async def create_test_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_test_title"] = update.message.text.strip()
    await update.message.reply_text(
        "Выберите доступ:",
        reply_markup=access_kb(),
        protect_content=False,
    )
    return CREATE_TEST_ACCESS


async def create_test_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text == "назад в меню":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=False,
        )
        return ADMIN_MENU

    if text == "бесплатный":
        context.user_data["new_test_access"] = "free"
    elif text == "платный":
        context.user_data["new_test_access"] = "paid"
    else:
        await update.message.reply_text("Нажмите: Бесплатный или Платный", protect_content=False)
        return CREATE_TEST_ACCESS

    await update.message.reply_text(
        f"Теперь введите таймер на каждый вопрос в секундах.\n\nМинимум: {MIN_TIMER}\nМаксимум: {MAX_TIMER}\n\nНапример: 30",
        protect_content=False,
    )
    return CREATE_TEST_TIMER


async def create_test_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("Введите число в секундах. Например: 30", protect_content=False)
        return CREATE_TEST_TIMER

    timer = int(raw)
    if timer < MIN_TIMER or timer > MAX_TIMER:
        await update.message.reply_text(
            f"Таймер должен быть от {MIN_TIMER} до {MAX_TIMER} секунд.",
            protect_content=False,
        )
        return CREATE_TEST_TIMER

    context.user_data["new_test_timer"] = timer

    await update.message.reply_text(
        "Теперь отправьте вопросы одним сообщением.\n\n"
        "Каждый вопрос отделяйте пустой строкой.\n"
        "Правильный ответ отмечайте звездочкой *\n\n"
        "Пример:\n\n"
        "Абылай хан кто он:\n"
        "А) хан*\n"
        "Б) раб\n"
        "В) батыр\n"
        "Г) аксакал\n\n"
        "В каком году образовалось Казахское ханство?\n"
        "А) 1465*\n"
        "Б) 1219\n"
        "В) 1731\n"
        "Г) 1917",
        reply_markup=ReplyKeyboardRemove(),
        protect_content=False,
    )
    return CREATE_TEST_QUESTIONS


async def create_test_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()

    subject = context.user_data.get("new_test_subject")
    title = context.user_data.get("new_test_title")
    access_type = context.user_data.get("new_test_access")
    question_timer = context.user_data.get("new_test_timer", 30)

    if not subject or not title or not access_type:
        await update.message.reply_text("Ошибка данных. Начните заново.", protect_content=False)
        return ConversationHandler.END

    try:
        parsed_questions = parse_bulk_questions(raw)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка в формате вопросов:\n\n{e}\n\nПопробуйте отправить заново.",
            protect_content=False,
        )
        return CREATE_TEST_QUESTIONS

    test_id = create_test(
        subject=subject,
        title=title,
        access_type=access_type,
        created_by=update.effective_user.id,
        question_timer=question_timer,
    )

    for i, q in enumerate(parsed_questions, start=1):
        add_question(
            test_id=test_id,
            question_text=q["question_text"],
            options=q["options"],
            position=i
        )

    await update.message.reply_text(
        f"✅ Тест сохранён!\n\n"
        f"ID: {test_id}\n"
        f"Раздел: {subject_label(subject)}\n"
        f"Название: {title}\n"
        f"Доступ: {'Бесплатный' if access_type == 'free' else 'Платный'}\n"
        f"Таймер: {question_timer} сек\n"
        f"Вопросов: {len(parsed_questions)}",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )

    for key in ["new_test_subject", "new_test_title", "new_test_access", "new_test_timer"]:
        context.user_data.pop(key, None)

    return ADMIN_MENU


# =========================================================
# ВЫДАТЬ ДОСТУП
# =========================================================

async def grant_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("У вас нет доступа.")
        return ConversationHandler.END

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=False)
        return GRANT_ACCESS_SELECT_TEST

    context.user_data["grant_test_id"] = test_id

    await query.message.reply_text(
        f"Выбран тест:\n#{test['id']} | {test['title']}\n\nТеперь отправьте Telegram ID пользователя.",
        protect_content=False,
    )
    return GRANT_ACCESS_ENTER_USER_ID


async def grant_access_enter_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    test_id = context.user_data.get("grant_test_id")

    if not test_id:
        await update.message.reply_text("Ошибка. Начните заново.", protect_content=False)
        return ConversationHandler.END

    if not raw.isdigit():
        await update.message.reply_text("Отправьте только Telegram ID цифрами.", protect_content=False)
        return GRANT_ACCESS_ENTER_USER_ID

    telegram_id = int(raw)
    grant_access(telegram_id, test_id)

    test = get_test_by_id(test_id)

    await update.message.reply_text(
        f"✅ Доступ выдан.\n\nПользователь ID: {telegram_id}\nТест: {test['title']}",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )

    context.user_data.pop("grant_test_id", None)
    return ADMIN_MENU


# =========================================================
# УБРАТЬ ДОСТУП
# =========================================================

async def revoke_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("У вас нет доступа.")
        return ConversationHandler.END

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=False)
        return REVOKE_ACCESS_SELECT_TEST

    context.user_data["revoke_test_id"] = test_id

    await query.message.reply_text(
        f"Выбран тест:\n#{test['id']} | {test['title']}\n\nТеперь отправьте Telegram ID пользователя, у которого нужно убрать доступ.",
        protect_content=False,
    )
    return REVOKE_ACCESS_ENTER_USER_ID


async def revoke_access_enter_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    test_id = context.user_data.get("revoke_test_id")

    if not test_id:
        await update.message.reply_text("Ошибка. Начните заново.", protect_content=False)
        return ConversationHandler.END

    if not raw.isdigit():
        await update.message.reply_text("Отправьте только Telegram ID цифрами.", protect_content=False)
        return REVOKE_ACCESS_ENTER_USER_ID

    telegram_id = int(raw)
    revoke_access(telegram_id, test_id)

    test = get_test_by_id(test_id)

    await update.message.reply_text(
        f"✅ Доступ убран.\n\nПользователь ID: {telegram_id}\nТест: {test['title']}",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )

    context.user_data.pop("revoke_test_id", None)
    return ADMIN_MENU


# =========================================================
# РЕДАКТИРОВАНИЕ
# =========================================================

async def edit_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=False)
        return EDIT_TEST_SELECT

    context.user_data["edit_test_id"] = test_id

    await query.message.reply_text(
        f"Выбран тест:\n"
        f"#{test['id']} | {test['title']}\n"
        f"Раздел: {subject_label(test['subject'])}\n"
        f"Доступ: {'Бесплатный' if test['access_type'] == 'free' else 'Платный'}\n"
        f"Таймер: {test['question_timer']} сек\n\n"
        f"Что хотите изменить?",
        reply_markup=edit_test_menu_kb(),
        protect_content=False,
    )
    return EDIT_TEST_MENU


async def edit_test_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    test_id = context.user_data.get("edit_test_id")

    if not test_id:
        await update.message.reply_text("Ошибка. Начните заново.", protect_content=False)
        return ConversationHandler.END

    if text == "Изменить название":
        await update.message.reply_text("Введите новое название теста:", protect_content=False)
        return EDIT_TEST_NEW_TITLE

    if text == "Изменить доступ":
        await update.message.reply_text(
            "Выберите новый доступ:",
            reply_markup=access_kb(),
            protect_content=False,
        )
        return EDIT_TEST_NEW_ACCESS

    if text == "Изменить таймер":
        await update.message.reply_text(
            f"Введите новый таймер в секундах.\nМинимум: {MIN_TIMER}\nМаксимум: {MAX_TIMER}",
            protect_content=False,
        )
        return EDIT_TEST_NEW_TIMER

    if text == "Заменить вопросы":
        await update.message.reply_text(
            "Отправьте новый блок вопросов.\nСтарые вопросы будут удалены и заменены новыми.",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=False,
        )
        return EDIT_TEST_REPLACE_QUESTIONS

    if text == "Назад в админку":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=False,
        )
        context.user_data.pop("edit_test_id", None)
        return ADMIN_MENU

    await update.message.reply_text("Выберите действие через кнопки.", protect_content=False)
    return EDIT_TEST_MENU


async def edit_test_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    new_title = update.message.text.strip()

    update_test_title(test_id, new_title)

    await update.message.reply_text(
        f"✅ Название изменено на:\n{new_title}",
        reply_markup=edit_test_menu_kb(),
        protect_content=False,
    )
    return EDIT_TEST_MENU


async def edit_test_new_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    text = update.message.text.strip().lower()

    if text == "бесплатный":
        new_access = "free"
    elif text == "платный":
        new_access = "paid"
    else:
        await update.message.reply_text("Нажмите: Бесплатный или Платный", protect_content=False)
        return EDIT_TEST_NEW_ACCESS

    update_test_access(test_id, new_access)

    await update.message.reply_text(
        f"✅ Доступ изменён: {'Бесплатный' if new_access == 'free' else 'Платный'}",
        reply_markup=edit_test_menu_kb(),
        protect_content=False,
    )
    return EDIT_TEST_MENU


async def edit_test_new_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("Введите число в секундах.", protect_content=False)
        return EDIT_TEST_NEW_TIMER

    timer = int(raw)
    if timer < MIN_TIMER or timer > MAX_TIMER:
        await update.message.reply_text(
            f"Таймер должен быть от {MIN_TIMER} до {MAX_TIMER} секунд.",
            protect_content=False,
        )
        return EDIT_TEST_NEW_TIMER

    update_test_timer(test_id, timer)

    await update.message.reply_text(
        f"✅ Таймер изменён: {timer} сек",
        reply_markup=edit_test_menu_kb(),
        protect_content=False,
    )
    return EDIT_TEST_MENU


async def edit_test_replace_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    raw = update.message.text.strip()

    try:
        parsed_questions = parse_bulk_questions(raw)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка в формате вопросов:\n\n{e}\n\nОтправьте вопросы заново.",
            protect_content=False,
        )
        return EDIT_TEST_REPLACE_QUESTIONS

    replace_test_questions(test_id, parsed_questions)

    await update.message.reply_text(
        f"✅ Вопросы заменены.\nНовых вопросов: {len(parsed_questions)}",
        reply_markup=edit_test_menu_kb(),
        protect_content=False,
    )
    return EDIT_TEST_MENU


# =========================================================
# УДАЛЕНИЕ
# =========================================================

async def delete_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=False)
        return DELETE_TEST_SELECT

    context.user_data["delete_test_id"] = test_id

    await query.message.reply_text(
        f"Вы действительно хотите удалить тест?\n\n#{test['id']} | {test['title']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_delete_test")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_inline")],
        ]),
        protect_content=False,
    )
    return DELETE_TEST_CONFIRM


async def confirm_delete_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = context.user_data.get("delete_test_id")
    if not test_id:
        await query.message.reply_text("Ошибка удаления.", protect_content=False)
        return ConversationHandler.END

    test = get_test_by_id(test_id)
    title = test["title"] if test else f"#{test_id}"

    delete_test(test_id)

    context.user_data.pop("delete_test_id", None)

    await query.message.reply_text(
        f"✅ Тест удалён: {title}",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )
    return ADMIN_MENU


# =========================================================
# ОБЩИЕ CALLBACK ДЛЯ АДМИНКИ
# =========================================================

async def admin_back_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "Админ-панель:",
        reply_markup=admin_menu_kb(),
        protect_content=False,
    )

    for key in ["grant_test_id", "revoke_test_id", "edit_test_id", "delete_test_id"]:
        context.user_data.pop(key, None)

    return ADMIN_MENU


# =========================================================
# ДОП. КОМАНДЫ
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (
        "/start — начать\n"
        "/help — помощь\n"
        "/id — показать ваш Telegram ID\n"
        "/cancel — отмена текущего действия\n"
        "/stop — остановить групповой тест в чате"
    )
    await update.message.reply_text(
        text,
        protect_content=protect_for_user(user_id),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=main_menu_kb(user_id),
        protect_content=protect_for_user(user_id),
    )

    for key in [
        "new_test_subject",
        "new_test_title",
        "new_test_access",
        "new_test_timer",
        "grant_test_id",
        "revoke_test_id",
        "edit_test_id",
        "delete_test_id",
    ]:
        context.user_data.pop(key, None)

    return ConversationHandler.END


# =========================================================
# FALLBACK
# =========================================================

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    chat_type = update.effective_chat.type

    if is_group_chat(chat_type):
        return

    if text == "Админ-панель" and is_admin(user_id):
        await admin_entry(update, context)
        return

    if text in {"История Казахстана", "Биология", "Химия", "Математическая грамотность"}:
        await show_subject_topics(update, context)
        return

    await update.message.reply_text(
        "Пожалуйста, используйте кнопки меню или команду /start.",
        protect_content=protect_for_user(user_id),
    )


# =========================================================
# MAIN
# =========================================================

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_entry),
            MessageHandler(filters.Regex("^Админ-панель$"), admin_entry),
        ],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_router)],

            CREATE_TEST_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_subject)],
            CREATE_TEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_name)],
            CREATE_TEST_ACCESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_access)],
            CREATE_TEST_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_timer)],
            CREATE_TEST_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_questions)],

            GRANT_ACCESS_SELECT_TEST: [
                CallbackQueryHandler(grant_test_select_callback, pattern=r"^grant_test:\d+$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],
            GRANT_ACCESS_ENTER_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, grant_access_enter_user_id)],

            REVOKE_ACCESS_SELECT_TEST: [
                CallbackQueryHandler(revoke_test_select_callback, pattern=r"^revoke_test:\d+$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],
            REVOKE_ACCESS_ENTER_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, revoke_access_enter_user_id)],

            EDIT_TEST_SELECT: [
                CallbackQueryHandler(edit_test_select_callback, pattern=r"^edit_test:\d+$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],
            EDIT_TEST_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_test_menu_router)],
            EDIT_TEST_NEW_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_test_new_title)],
            EDIT_TEST_NEW_ACCESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_test_new_access)],
            EDIT_TEST_NEW_TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_test_new_timer)],
            EDIT_TEST_REPLACE_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_test_replace_questions)],

            DELETE_TEST_SELECT: [
                CallbackQueryHandler(delete_test_select_callback, pattern=r"^delete_test:\d+$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],
            DELETE_TEST_CONFIRM: [
                CallbackQueryHandler(confirm_delete_test_callback, pattern=r"^confirm_delete_test$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],

            GROUP_LAUNCH_SELECT: [
                CallbackQueryHandler(group_launch_test_select_callback, pattern=r"^group_launch_test:\d+$"),
                CallbackQueryHandler(admin_back_inline_callback, pattern=r"^admin_back_inline$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stop", stop_group_quiz))

    app.add_handler(admin_conv)

    app.add_handler(InlineQueryHandler(inline_query_handler))

    app.add_handler(CallbackQueryHandler(open_test_callback, pattern=r"^open_test:\d+$"))
    app.add_handler(CallbackQueryHandler(start_private_test_callback, pattern=r"^start_private_test:\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_main_callback, pattern=r"^back_to_main$"))
    app.add_handler(CallbackQueryHandler(back_to_subject_callback, pattern=r"^back_to_subject:.+$"))
    app.add_handler(CallbackQueryHandler(continue_test_callback, pattern=r"^continue_test$"))
    app.add_handler(CallbackQueryHandler(finish_test_now_callback, pattern=r"^finish_test_now$"))
    app.add_handler(CallbackQueryHandler(group_join_quiz_callback, pattern=r"^group_join_quiz:\d+$"))
    app.add_handler(CallbackQueryHandler(continue_group_quiz_callback, pattern=r"^group_continue:\-?\d+$"))
    app.add_handler(CallbackQueryHandler(finish_group_quiz_callback, pattern=r"^group_finish:\-?\d+$"))

    app.add_handler(PollAnswerHandler(handle_poll_answer))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        group_launch_from_message
    ))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        group_message_guard
    ))

    app.add_handler(MessageHandler(
        filters.Regex("^(История Казахстана|Биология|Химия|Математическая грамотность)$"),
        show_subject_topics
    ))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    return app


def main() -> None:
    init_db()
    application = build_application()
    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
