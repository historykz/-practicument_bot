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
MIN_GROUP_PLAYERS = 2

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

GROUP_LAUNCH_SELECT = 60

# =========================================================
# REGEX
# =========================================================

OPTION_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁёІіҚқҢңҒғҮүҰұӨөҺһ]|[A-DА-Г])[\)\.\-:]\s*(.+)$")

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


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    mins = total // 60
    secs = total % 60
    return f"{mins} мин {secs} сек"


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


def build_group_lobby_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пройти тест", callback_data="group_join_quiz")]
    ])


def build_private_card_keyboard(test_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пройти тест", callback_data=f"start_private_test:{test_id}")],
    ])


def build_group_lobby_text(title: str, total_questions: int, timer: int, ready_count: int = 0) -> str:
    ready_part = f"\n👥 Готовы участников: {ready_count}\n" if ready_count else "\n"
    return (
        f"🎲 Приготовьтесь пройти тест «{title}»\n\n"
        f"🖊 {total_questions} вопросов\n"
        f"⏱ {timer} секунд на вопрос\n"
        f"📰 Ответы видны участникам группы и автору теста"
        f"{ready_part}\n"
        f"🏁 Вопросы появятся, когда хотя бы {MIN_GROUP_PLAYERS} человека будут готовы отвечать.\n"
        f"Чтобы остановить тест, отправьте /stop"
    )


# =========================================================
# КЛАВИАТУРЫ
# =========================================================

def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        ["История Казахстана", "Биология"],
        ["Химия", "Математическая грамотность"],
    ]
    if is_admin(user_id):
        rows.append(["Админ-панель"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


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
# ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

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
    if not update.message:
        return
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"Ваш Telegram ID: {user_id}",
        protect_content=protect_for_user(user_id),
    )


async def show_subject_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

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
# ЛИЧНЫЙ ТЕСТ
# =========================================================

async def open_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.")
        return

    if test["access_type"] == "paid" and not has_access(query.from_user.id, test_id):
        kb = []
        if BUY_CONTACT.startswith("@"):
            kb.append([
                InlineKeyboardButton(
                    "💬 Получить доступ",
                    url=f"https://t.me/{BUY_CONTACT.replace('@', '')}"
                )
            ])
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"back_to_subject:{test['subject']}")])

        await query.message.reply_text(
            f"🔒 Этот раздел закрыт.\n\nДля получения доступа напишите: {BUY_CONTACT}",
            reply_markup=InlineKeyboardMarkup(kb),
            protect_content=protect_for_user(query.from_user.id),
        )
        return

    questions = get_questions_for_test(test_id)
    if not questions:
        await query.message.reply_text("В этом тесте пока нет вопросов.")
        return

    if query.message.chat.type == "private":
        text = (
            f"📚 Тест: {test['title']}\n"
            f"🖊 Вопросов: {len(questions)}\n"
            f"⏱ {int(test['question_timer'])} сек на вопрос\n"
                        f"\nГотовы начать?"
        )

        await query.message.reply_text(
            text,
            reply_markup=build_private_card_keyboard(test_id),
        )

    else:
        await start_group_quiz(update, context, test_id)


async def start_private_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split(":")[1])

    questions = get_questions_for_test(test_id)

    session_store = get_sessions_store(context.application)

    session_store[query.from_user.id] = {
        "test_id": test_id,
        "questions": questions,
        "current": 0,
        "score": 0,
        "start_time": time.time(),
    }

    await send_private_question(query.from_user.id, context)


async def send_private_question(user_id, context):

    sessions = get_sessions_store(context.application)
    poll_map = get_poll_map(context.application)

    session = sessions[user_id]

    if session["current"] >= len(session["questions"]):
        await finish_private_test(user_id, context)
        return

    question = session["questions"][session["current"]]
    options = get_options_for_question(question["id"])

    option_texts = [x["option_text"] for x in options]
    correct_index = next(i for i,x in enumerate(options) if x["is_correct"])

    poll = await context.bot.send_poll(
        chat_id=user_id,
        question=f"Вопрос {session['current']+1}/{len(session['questions'])}\n\n{question['question_text']}",
        options=option_texts,
        type=PollType.QUIZ,
        correct_option_id=correct_index,
        is_anonymous=False,
    )

    poll_map[poll.poll.id] = user_id


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    poll_map = get_poll_map(context.application)
    sessions = get_sessions_store(context.application)

    poll_id = update.poll_answer.poll_id

    if poll_id not in poll_map:
        return

    user_id = poll_map[poll_id]

    if user_id not in sessions:
        return

    session = sessions[user_id]

    selected = update.poll_answer.option_ids[0]

    question = session["questions"][session["current"]]
    options = get_options_for_question(question["id"])

    correct = next(i for i,x in enumerate(options) if x["is_correct"])

    if selected == correct:
        session["score"] += 1

    session["current"] += 1

    await asyncio.sleep(1)

    await send_private_question(user_id, context)


async def finish_private_test(user_id, context):

    sessions = get_sessions_store(context.application)
    session = sessions[user_id]

    score = session["score"]
    total = len(session["questions"])
    test_id = session["test_id"]

    elapsed = time.time() - session["start_time"]

    save_result(user_id, test_id, score, total)

    await context.bot.send_message(
        chat_id=user_id,
        text=f"🏁 Тест завершен\n\nПравильных ответов: {score}/{total}\nВремя: {format_elapsed(elapsed)}"
    )

    del sessions[user_id]


async def start_group_quiz(update, context, test_id):

    chat_id = update.callback_query.message.chat.id

    group_store = get_group_quiz_store(context.application)

    test = get_test_by_id(test_id)
    questions = get_questions_for_test(test_id)

    group_store[chat_id] = {
        "test_id": test_id,
        "questions": questions,
        "current": 0,
        "players": set(),
        "scores": {},
        "no_answers": 0,
        "started": False
    }

    await context.bot.send_message(
        chat_id=chat_id,
        text=build_group_lobby_text(test["title"], len(questions), test["question_timer"]),
        reply_markup=build_group_lobby_keyboard()
    )


async def join_group_quiz(update, context):

    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    user_id = query.from_user.id

    group_store = get_group_quiz_store(context.application)

    if chat_id not in group_store:
        return

    session = group_store[chat_id]

    session["players"].add(user_id)
    session["scores"].setdefault(user_id, 0)

    if len(session["players"]) >= MIN_GROUP_PLAYERS and not session["started"]:
        session["started"] = True
        await countdown_and_start(chat_id, context)


async def countdown_and_start(chat_id, context):

    for i in [3,2,1]:
        await context.bot.send_message(chat_id, str(i))
        await asyncio.sleep(1)

    await context.bot.send_message(chat_id, "🚀 Тест начался!")

    await send_group_question(chat_id, context)


async def send_group_question(chat_id, context):

    group_store = get_group_quiz_store(context.application)
    poll_map = get_group_poll_map(context.application)

    session = group_store[chat_id]

    if session["current"] >= len(session["questions"]):
        await finish_group_quiz(chat_id, context)
        return

    question = session["questions"][session["current"]]
    options = get_options_for_question(question["id"])

    option_texts = [x["option_text"] for x in options]
    correct = next(i for i,x in enumerate(options) if x["is_correct"])

    poll = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Вопрос {session['current']+1}",
        options=option_texts,
        type=PollType.QUIZ,
        correct_option_id=correct,
        is_anonymous=False,
    )

    poll_map[poll.poll.id] = {
        "chat_id": chat_id,
        "correct": correct
    }


async def finish_group_quiz(chat_id, context):

    group_store = get_group_quiz_store(context.application)

    session = group_store[chat_id]

    scores = session["scores"]

    ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    text = "🏆 Результаты\n\n"

    for i,(user,score) in enumerate(ranking,1):
        text += f"{i}. {user} — {score}\n"

    await context.bot.send_message(chat_id, text)

    del group_store[chat_id]
