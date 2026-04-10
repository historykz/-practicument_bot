import hashlib
import logging
import os
import random
import re
import sqlite3
from typing import Any, Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Poll,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import PollType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()  # пример: "123456789,987654321"

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения")

if not ADMINS_RAW:
    raise ValueError("Не найден ADMINS в переменных окружения")

ADMINS = {int(x.strip()) for x in ADMINS_RAW.split(",") if x.strip().isdigit()}

DB_PATH = "ent_bot.db"
BUY_CONTACT = "@your_manager_username"  # замени на свой username
MIN_TIMER = 5
MAX_TIMER = 600

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

EDIT_TEST_SELECT = 40
EDIT_TEST_MENU = 41
EDIT_TEST_NEW_TITLE = 42
EDIT_TEST_NEW_ACCESS = 43
EDIT_TEST_NEW_TIMER = 44
EDIT_TEST_REPLACE_QUESTIONS = 45

DELETE_TEST_SELECT = 50
DELETE_TEST_CONFIRM = 51

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

OPTION_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁёІіҚқҢңҒғҮүҰұӨөҺһ]|[A-DА-Г])[\)\.\-:]\s*(.+)$")


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
# КЛАВИАТУРЫ
# =========================================================

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["История Казахстана", "Биология"],
            ["Химия", "Математическая грамотность"],
        ],
        resize_keyboard=True
    )


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Создать тест", "Изменить тест"],
            ["Выдать доступ", "Удалить тест"],
            ["Назад в меню"],
        ],
        resize_keyboard=True
    )


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


# =========================================================
# УТИЛИТЫ
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


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


def get_sessions_store(application: Application) -> Dict[int, Dict[str, Any]]:
    if "quiz_sessions" not in application.bot_data:
        application.bot_data["quiz_sessions"] = {}
    return application.bot_data["quiz_sessions"]


def get_poll_map(application: Application) -> Dict[str, int]:
    if "poll_to_user" not in application.bot_data:
        application.bot_data["poll_to_user"] = {}
    return application.bot_data["poll_to_user"]


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

    keyboard.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")
    ])

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

    keyboard.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_inline")
    ])

    return InlineKeyboardMarkup(keyboard)


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
        reply_markup=main_menu_kb(),
        protect_content=True,
    )

    if is_admin(user.id):
        await update.message.reply_text(
            "Вы администратор. Для управления используйте /admin",
            protect_content=True,
        )

    return ConversationHandler.END


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Ваш Telegram ID: {update.effective_user.id}",
        protect_content=True,
    )


async def show_subject_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    code = subject_code(text)

    if not code:
        await update.message.reply_text(
            "Пожалуйста, выберите предмет через кнопки.",
            protect_content=True,
        )
        return ConversationHandler.END

    tests = get_subject_tests(code)

    if not tests:
        await update.message.reply_text(
            "По этому предмету пока нет тем.\n\nВыберите другой предмет:",
            reply_markup=main_menu_kb(),
            protect_content=True,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Выберите тему практики:",
        reply_markup=build_topics_keyboard(code),
        protect_content=True,
    )
    return ConversationHandler.END


# =========================================================
# ЛОГИКА ВИКТОРИН
# =========================================================

async def open_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=True)
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

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"back_to_subject:{test['subject']}")])

        await query.message.reply_text(
            f"🔒 Этот раздел закрыт.\n\nДля получения доступа напишите: {BUY_CONTACT}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            protect_content=True,
        )
        return

    questions = get_questions_for_test(test_id)
    if not questions:
        await query.message.reply_text("В этом тесте пока нет вопросов.", protect_content=True)
        return

    sessions = get_sessions_store(context.application)

    if query.from_user.id in sessions:
        await finish_user_test(
            application=context.application,
            user_id=query.from_user.id,
            send_result=False,
        )

    sessions[query.from_user.id] = {
        "user_id": query.from_user.id,
        "chat_id": query.message.chat_id,
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

    await query.message.reply_text(
        f"▶️ Начинаем тест: {test['title']}\n"
        f"⏱ Таймер на каждый вопрос: {int(test['question_timer'])} сек",
        protect_content=True,
    )

    await send_next_question(context.application, query.from_user.id)


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
        protect_content=True,
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
        protect_content=True,
    )
    session["current_control_message_id"] = control_message.message_id

    application.job_queue.run_once(
        quiz_timeout_job,
        when=session["timer"] + 1,
        data={
            "user_id": user_id,
            "poll_id": poll_message.poll.id,
        },
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
        protect_content=True,
    )


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def continue_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    sessions = get_sessions_store(context.application)
    session = sessions.get(query.from_user.id)

    if not session or not session["active"]:
        await query.message.reply_text("Активный тест не найден.", protect_content=True)
        return

    session["paused"] = False
    await query.message.reply_text("▶️ Продолжаем тест.", protect_content=True)
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
    subject = session["subject"]
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
        save_result(chat_id, test_id, score, total)

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
        protect_content=True,
    )

    tests = get_subject_tests(subject)
    if tests:
        await application.bot.send_message(
            chat_id=chat_id,
            text="Выберите тему практики:",
            reply_markup=build_topics_keyboard(subject),
            protect_content=True,
        )
    else:
        await application.bot.send_message(
            chat_id=chat_id,
            text="Выберите предмет для практики:",
            reply_markup=main_menu_kb(),
            protect_content=True,
        )


# =========================================================
# НАЗАД
# =========================================================

async def back_to_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Выберите предмет для практики:",
        reply_markup=main_menu_kb(),
        protect_content=True,
    )


async def back_to_subject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    subject = query.data.split(":", 1)[1]
    await query.message.reply_text(
        "Выберите тему практики:",
        reply_markup=build_topics_keyboard(subject),
        protect_content=True,
    )


# =========================================================
# АДМИНКА
# =========================================================

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа.", protect_content=True)
        return ConversationHandler.END

    await update.message.reply_text(
        "Админ-панель.\nВыберите действие:",
        reply_markup=admin_menu_kb(),
        protect_content=True,
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
            protect_content=True,
        )
        return CREATE_TEST_SUBJECT

    if text == "Выдать доступ":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=True)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест, к которому нужно выдать доступ:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("grant_test"),
            protect_content=True,
        )
        return GRANT_ACCESS_SELECT_TEST

    if text == "Изменить тест":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=True)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест для редактирования:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("edit_test"),
            protect_content=True,
        )
        return EDIT_TEST_SELECT

    if text == "Удалить тест":
        tests = get_all_tests()
        if not tests:
            await update.message.reply_text("Сначала создайте тест.", protect_content=True)
            return ADMIN_MENU

        await update.message.reply_text(
            "Выберите тест для удаления:",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        await update.message.reply_text(
            "Список тестов ниже:",
            reply_markup=build_tests_inline_keyboard("delete_test"),
            protect_content=True,
        )
        return DELETE_TEST_SELECT

    if text == "Назад в меню":
        await update.message.reply_text(
            "Главное меню:",
            reply_markup=main_menu_kb(),
            protect_content=True,
        )
        return ConversationHandler.END

    await update.message.reply_text("Выберите действие через кнопки.", protect_content=True)
    return ADMIN_MENU


# =========================================================
# СОЗДАНИЕ ТЕСТА
# =========================================================

async def create_test_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "Назад в меню":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=True,
        )
        return ADMIN_MENU

    code = subject_code(text)
    if not code:
        await update.message.reply_text("Выберите раздел через кнопки.", protect_content=True)
        return CREATE_TEST_SUBJECT

    context.user_data["new_test_subject"] = code
    await update.message.reply_text("Введите название темы или теста:", protect_content=True)
    return CREATE_TEST_NAME


async def create_test_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_test_title"] = update.message.text.strip()
    await update.message.reply_text(
        "Выберите доступ:",
        reply_markup=access_kb(),
        protect_content=True,
    )
    return CREATE_TEST_ACCESS


async def create_test_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text == "назад в меню":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=True,
        )
        return ADMIN_MENU

    if text == "бесплатный":
        context.user_data["new_test_access"] = "free"
    elif text == "платный":
        context.user_data["new_test_access"] = "paid"
    else:
        await update.message.reply_text("Нажмите: Бесплатный или Платный", protect_content=True)
        return CREATE_TEST_ACCESS

    await update.message.reply_text(
        f"Теперь введите таймер на каждый вопрос в секундах.\n\nМинимум: {MIN_TIMER}\nМаксимум: {MAX_TIMER}\n\nНапример: 30",
        protect_content=True,
    )
    return CREATE_TEST_TIMER


async def create_test_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("Введите число в секундах. Например: 30", protect_content=True)
        return CREATE_TEST_TIMER

    timer = int(raw)
    if timer < MIN_TIMER or timer > MAX_TIMER:
        await update.message.reply_text(
            f"Таймер должен быть от {MIN_TIMER} до {MAX_TIMER} секунд.",
            protect_content=True,
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
        protect_content=True,
    )
    return CREATE_TEST_QUESTIONS


async def create_test_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()

    subject = context.user_data.get("new_test_subject")
    title = context.user_data.get("new_test_title")
    access_type = context.user_data.get("new_test_access")
    question_timer = context.user_data.get("new_test_timer", 30)

    if not subject or not title or not access_type:
        await update.message.reply_text("Ошибка данных. Начните заново через /admin.", protect_content=True)
        return ConversationHandler.END

    try:
        parsed_questions = parse_bulk_questions(raw)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка в формате вопросов:\n\n{e}\n\nПопробуйте отправить заново.",
            protect_content=True,
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
        protect_content=True,
    )

    for key in [
        "new_test_subject",
        "new_test_title",
        "new_test_access",
        "new_test_timer",
    ]:
        context.user_data.pop(key, None)

    return ADMIN_MENU


# =========================================================
# ВЫДАЧА ДОСТУПА
# =========================================================

async def grant_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=True)
        return GRANT_ACCESS_SELECT_TEST

    context.user_data["grant_test_id"] = test_id

    await query.message.reply_text(
        f"Выбран тест:\n#{test['id']} | {test['title']}\n\nТеперь отправьте Telegram ID пользователя.",
        protect_content=True,
    )
    return GRANT_ACCESS_ENTER_USER_ID


async def grant_access_enter_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    test_id = context.user_data.get("grant_test_id")

    if not test_id:
        await update.message.reply_text("Ошибка. Начните заново через /admin.", protect_content=True)
        return ConversationHandler.END

    if not raw.isdigit():
        await update.message.reply_text("Отправьте только Telegram ID цифрами.", protect_content=True)
        return GRANT_ACCESS_ENTER_USER_ID

    telegram_id = int(raw)
    grant_access(telegram_id, test_id)

    test = get_test_by_id(test_id)

    await update.message.reply_text(
        f"✅ Доступ выдан.\n\nПользователь ID: {telegram_id}\nТест: {test['title']}",
        reply_markup=admin_menu_kb(),
        protect_content=True,
    )

    context.user_data.pop("grant_test_id", None)
    return ADMIN_MENU


# =========================================================
# РЕДАКТИРОВАНИЕ ТЕСТА
# =========================================================

async def edit_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=True)
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
        protect_content=True,
    )
    return EDIT_TEST_MENU


async def edit_test_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    test_id = context.user_data.get("edit_test_id")

    if not test_id:
        await update.message.reply_text("Ошибка. Начните заново через /admin.", protect_content=True)
        return ConversationHandler.END

    if text == "Изменить название":
        await update.message.reply_text("Введите новое название теста:", protect_content=True)
        return EDIT_TEST_NEW_TITLE

    if text == "Изменить доступ":
        await update.message.reply_text(
            "Выберите новый доступ:",
            reply_markup=access_kb(),
            protect_content=True,
        )
        return EDIT_TEST_NEW_ACCESS

    if text == "Изменить таймер":
        await update.message.reply_text(
            f"Введите новый таймер в секундах.\nМинимум: {MIN_TIMER}\nМаксимум: {MAX_TIMER}",
            protect_content=True,
        )
        return EDIT_TEST_NEW_TIMER

    if text == "Заменить вопросы":
        await update.message.reply_text(
            "Отправьте новый блок вопросов.\nСтарые вопросы будут удалены и заменены новыми.",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        return EDIT_TEST_REPLACE_QUESTIONS

    if text == "Назад в админку":
        await update.message.reply_text(
            "Админ-панель:",
            reply_markup=admin_menu_kb(),
            protect_content=True,
        )
        context.user_data.pop("edit_test_id", None)
        return ADMIN_MENU

    await update.message.reply_text("Выберите действие через кнопки.", protect_content=True)
    return EDIT_TEST_MENU


async def edit_test_new_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    new_title = update.message.text.strip()

    update_test_title(test_id, new_title)

    await update.message.reply_text(
        f"✅ Название изменено на:\n{new_title}",
        reply_markup=edit_test_menu_kb(),
        protect_content=True,
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
        await update.message.reply_text("Нажмите: Бесплатный или Платный", protect_content=True)
        return EDIT_TEST_NEW_ACCESS

    update_test_access(test_id, new_access)

    await update.message.reply_text(
        f"✅ Доступ изменён: {'Бесплатный' if new_access == 'free' else 'Платный'}",
        reply_markup=edit_test_menu_kb(),
        protect_content=True,
    )
    return EDIT_TEST_MENU


async def edit_test_new_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    test_id = context.user_data.get("edit_test_id")
    raw = update.message.text.strip()

    if not raw.isdigit():
        await update.message.reply_text("Введите число в секундах.", protect_content=True)
        return EDIT_TEST_NEW_TIMER

    timer = int(raw)
    if timer < MIN_TIMER or timer > MAX_TIMER:
        await update.message.reply_text(
            f"Таймер должен быть от {MIN_TIMER} до {MAX_TIMER} секунд.",
            protect_content=True,
        )
        return EDIT_TEST_NEW_TIMER

    update_test_timer(test_id, timer)

    await update.message.reply_text(
        f"✅ Таймер изменён: {timer} сек",
        reply_markup=edit_test_menu_kb(),
        protect_content=True,
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
            protect_content=True,
        )
        return EDIT_TEST_REPLACE_QUESTIONS

    replace_test_questions(test_id, parsed_questions)

    await update.message.reply_text(
        f"✅ Вопросы заменены.\nНовых вопросов: {len(parsed_questions)}",
        reply_markup=edit_test_menu_kb(),
        protect_content=True,
    )
    return EDIT_TEST_MENU


# =========================================================
# УДАЛЕНИЕ ТЕСТА
# =========================================================

async def delete_test_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.", protect_content=True)
        return DELETE_TEST_SELECT

    context.user_data["delete_test_id"] = test_id

    await query.message.reply_text(
        f"Вы действительно хотите удалить тест?\n\n#{test['id']} | {test['title']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Да, удалить", callback_data="confirm_delete_test")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back_inline")],
        ]),
        protect_content=True,
    )
    return DELETE_TEST_CONFIRM


async def confirm_delete_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    test_id = context.user_data.get("delete_test_id")
    if not test_id:
        await query.message.reply_text("Ошибка удаления.", protect_content=True)
        return ConversationHandler.END

    test = get_test_by_id(test_id)
    title = test["title"] if test else f"#{test_id}"

    delete_test(test_id)

    context.user_data.pop("delete_test_id", None)

    await query.message.reply_text(
        f"✅ Тест удалён: {title}",
        reply_markup=admin_menu_kb(),
        protect_content=True,
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
        protect_content=True,
    )

    for key in ["grant_test_id", "edit_test_id", "delete_test_id"]:
        context.user_data.pop(key, None)

    return ADMIN_MENU


# =========================================================
# ДОП. КОМАНДЫ
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "/start — начать\n"
        "/help — помощь\n"
        "/id — показать ваш Telegram ID\n"
        "/admin — админ-панель\n"
        "/cancel — отмена текущего действия"
    )
    await update.message.reply_text(text, protect_content=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=main_menu_kb(),
        protect_content=True,
    )
    for key in [
        "new_test_subject",
        "new_test_title",
        "new_test_access",
        "new_test_timer",
        "grant_test_id",
        "edit_test_id",
        "delete_test_id",
    ]:
        context.user_data.pop(key, None)
    return ConversationHandler.END


# =========================================================
# FALLBACK
# =========================================================

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text in {"История Казахстана", "Биология", "Химия", "Математическая грамотность"}:
        await show_subject_topics(update, context)
        return

    await update.message.reply_text(
        "Пожалуйста, используйте кнопки меню или команду /start.",
        protect_content=True,
    )


# =========================================================
# MAIN
# =========================================================

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
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
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(admin_conv)

    app.add_handler(CallbackQueryHandler(open_test_callback, pattern=r"^open_test:\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_main_callback, pattern=r"^back_to_main$"))
    app.add_handler(CallbackQueryHandler(back_to_subject_callback, pattern=r"^back_to_subject:.+$"))
    app.add_handler(CallbackQueryHandler(continue_test_callback, pattern=r"^continue_test$"))
    app.add_handler(CallbackQueryHandler(finish_test_now_callback, pattern=r"^finish_test_now$"))

    app.add_handler(PollAnswerHandler(handle_poll_answer))

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
