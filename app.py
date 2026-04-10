import logging
import os
import re
import sqlite3
import hashlib
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
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
BUY_CONTACT = "@your_manager_username"  # сюда поставь свой username/номер
BUY_TEXT = (
    "🔒 Этот раздел доступен только после оплаты.\n\n"
    f"Для покупки напишите: {BUY_CONTACT}"
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# СОСТОЯНИЯ
# =========================================================

WAITING_LOGIN = 1

ADMIN_MENU = 10
CREATE_ACCOUNT_LOGIN = 11
CREATE_ACCOUNT_PASSWORD = 12

CREATE_TEST_SUBJECT = 20
CREATE_TEST_NAME = 21
CREATE_TEST_ACCESS = 22
CREATE_TEST_QUESTIONS = 23

# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS student_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT,
        is_active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS telegram_users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        student_account_id INTEGER,
        is_logged_in INTEGER DEFAULT 0,
        FOREIGN KEY(student_account_id) REFERENCES student_accounts(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        title TEXT NOT NULL,
        access_type TEXT NOT NULL CHECK(access_type IN ('free', 'paid')),
        is_final INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        explanation TEXT,
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


# =========================================================
# DB HELPERS
# =========================================================

def upsert_telegram_user(user) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_users (telegram_id, username, first_name, is_logged_in)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user.id, user.username, user.first_name))
    conn.commit()
    conn.close()


def create_student_account(login: str, password: str, full_name: str = "") -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO student_accounts (login, password_hash, full_name)
            VALUES (?, ?, ?)
        """, (login.strip(), hash_password(password.strip()), full_name.strip()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def verify_student_account(login: str, password: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM student_accounts
        WHERE login = ? AND password_hash = ? AND is_active = 1
    """, (login.strip(), hash_password(password.strip())))
    row = cur.fetchone()
    conn.close()
    return row


def login_telegram_user(telegram_id: int, student_account_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE telegram_users
        SET student_account_id = ?, is_logged_in = 1
        WHERE telegram_id = ?
    """, (student_account_id, telegram_id))
    conn.commit()
    conn.close()


def is_logged_in(telegram_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT is_logged_in FROM telegram_users
        WHERE telegram_id = ?
    """, (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row["is_logged_in"]) if row else False


def create_test(subject: str, title: str, access_type: str, is_final: int, created_by: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tests (subject, title, access_type, is_final, created_by)
        VALUES (?, ?, ?, ?, ?)
    """, (subject, title, access_type, is_final, created_by))
    test_id = cur.lastrowid
    conn.commit()
    conn.close()
    return test_id


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


def get_subject_tests(subject: str, finals_only: bool = False) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tests
        WHERE subject = ? AND is_final = ?
        ORDER BY id DESC
    """, (subject, 1 if finals_only else 0))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_final_tests() -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM tests
        WHERE is_final = 1
        ORDER BY id DESC
    """)
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
# Формат:
#
# Абылай хан кто он:
# А) хан*
# Б) раб
# В) батыр
# Г) аксакал
#
# Следующий вопрос:
# А) ...
# ...
# =========================================================

OPTION_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁёІіҚқҢңҒғҮүҰұӨөҺһ]|[A-DА-Г])[\)\.\-:]\s*(.+)$")


def parse_bulk_questions(raw_text: str) -> List[Dict[str, Any]]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw_text.strip()) if b.strip()]
    result = []

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

        if sum(1 for x in options if x["is_correct"]) != 1:
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
            ["Войти"],
        ],
        resize_keyboard=True,
    )


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Создать аккаунт", "Создать тест"],
            ["Назад в меню"],
        ],
        resize_keyboard=True,
    )


def access_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Бесплатный", "Платный"],
            ["Назад в меню"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
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
        "finals": "Итоговые тесты",
    }
    return mapping.get(subject_code_value, subject_code_value)


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
        "Выберите предмет для практики:"
    )

    await update.message.reply_text(text, reply_markup=main_menu_kb())

    if is_admin(user.id):
        await update.message.reply_text(
            "Вы администратор. Для управления ботом используйте команду /admin"
        )

    return ConversationHandler.END


async def show_subject_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    code = subject_code(text)

    if not code:
        await update.message.reply_text("Пожалуйста, выберите предмет через кнопки.")
        return ConversationHandler.END

    tests = get_subject_tests(code, finals_only=False)

    if not tests:
        await update.message.reply_text(
            "По этому предмету пока нет тем. Админ ещё не добавил тесты.",
            reply_markup=main_menu_kb()
        )
        return ConversationHandler.END

    keyboard = []
    for t in tests:
        lock = "🔓" if t["access_type"] == "free" else "🔒"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{t['title']} {lock}",
                callback_data=f"open_test:{t['id']}"
            )
        ])

    await update.message.reply_text(
        "Выберите тему практики:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


async def ask_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Введите свой логин и пароль в формате:\n\nлогин,пароль",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_LOGIN


async def process_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    user = update.effective_user

    if "," not in raw:
        await update.message.reply_text(
            "Неверный формат.\nВведите так:\nлогин,пароль"
        )
        return WAITING_LOGIN

    login, password = [x.strip() for x in raw.split(",", 1)]
    account = verify_student_account(login, password)

    if not account:
        await update.message.reply_text(
            "❌ Неверный логин или пароль.\nПопробуйте ещё раз:"
        )
        return WAITING_LOGIN

    login_telegram_user(user.id, account["id"])

    finals = get_final_tests()
    if finals:
        keyboard = []
        for t in finals:
            keyboard.append([
                InlineKeyboardButton(
                    text=t["title"],
                    callback_data=f"open_test:{t['id']}"
                )
            ])
        await update.message.reply_text(
            f"Привет, {get_user_tag(user)}!\n\nВыберите итоговый тест:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"Привет, {get_user_tag(user)}!\n\nИтоговые тесты пока не добавлены.",
            reply_markup=main_menu_kb()
        )

    return ConversationHandler.END


async def open_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    test_id = int(data.split(":")[1])
    test = get_test_by_id(test_id)

    if not test:
        await query.message.reply_text("Тест не найден.")
        return

    # Платный раздел
    if test["access_type"] == "paid" and not is_logged_in(query.from_user.id):
        await query.message.reply_text(BUY_TEXT)
        return

    questions = get_questions_for_test(test_id)
    if not questions:
        await query.message.reply_text("В этом тесте пока нет вопросов.")
        return

    context.user_data["quiz"] = {
        "test_id": test_id,
        "title": test["title"],
        "questions": [dict(q) for q in questions],
        "current_index": 0,
        "score": 0,
        "answered": set(),
    }

    await send_current_question(query.message.chat_id, context)


async def send_current_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    quiz = context.user_data.get("quiz")
    if not quiz:
        return

    idx = quiz["current_index"]
    questions = quiz["questions"]

    if idx >= len(questions):
        score = quiz["score"]
        total = len(questions)
        save_result(chat_id, quiz["test_id"], score, total)

        percent = round((score / total) * 100, 1) if total else 0

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Тест завершён!\n\n"
                f"📚 Тест: {quiz['title']}\n"
                f"🏆 Результат: {score}/{total}\n"
                f"📈 Процент: {percent}%"
            ),
            reply_markup=main_menu_kb()
        )
        context.user_data.pop("quiz", None)
        return

    q = questions[idx]
    options = get_options_for_question(q["id"])

    keyboard = []
    for opt in options:
        keyboard.append([
            InlineKeyboardButton(
                text=opt["option_text"],
                callback_data=f"answer:{q['id']}:{opt['id']}"
            )
        ])

    text = (
        f"Вопрос {idx + 1} из {len(questions)}\n\n"
        f"{q['question_text']}"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    quiz = context.user_data.get("quiz")
    if not quiz:
        await query.message.reply_text("Сначала откройте тест.")
        return

    _, qid_str, oid_str = query.data.split(":")
    question_id = int(qid_str)
    option_id = int(oid_str)

    key = f"{question_id}"
    if key in quiz["answered"]:
        await query.message.reply_text("Вы уже ответили на этот вопрос.")
        return

    options = get_options_for_question(question_id)
    chosen = next((x for x in options if x["id"] == option_id), None)
    correct = next((x for x in options if x["is_correct"] == 1), None)

    if not chosen or not correct:
        await query.message.reply_text("Ошибка обработки ответа.")
        return

    quiz["answered"].add(key)

    if chosen["id"] == correct["id"]:
        quiz["score"] += 1
        await query.message.reply_text("✅ Верно!")
    else:
        await query.message.reply_text(
            f"❌ Неверно.\nПравильный ответ: {correct['option_text']}"
        )

    quiz["current_index"] += 1
    await send_current_question(query.message.chat_id, context)


# =========================================================
# АДМИНКА
# =========================================================

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Админ-панель.\nВыберите действие:",
        reply_markup=admin_menu_kb()
    )
    return ADMIN_MENU


async def admin_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text

    if text == "Создать аккаунт":
        await update.message.reply_text(
            "Введите логин для ученика:",
            reply_markup=ReplyKeyboardRemove()
        )
        return CREATE_ACCOUNT_LOGIN

    if text == "Создать тест":
        keyboard = ReplyKeyboardMarkup(
            [
                ["История Казахстана", "Биология"],
                ["Химия", "Математическая грамотность"],
                ["Итоговые тесты"],
                ["Назад в меню"],
            ],
            resize_keyboard=True
        )
        await update.message.reply_text(
            "Выберите раздел, куда сохранить тест:",
            reply_markup=keyboard
        )
        return CREATE_TEST_SUBJECT

    if text == "Назад в меню":
        await update.message.reply_text("Главное меню:", reply_markup=main_menu_kb())
        return ConversationHandler.END

    await update.message.reply_text("Выберите действие через кнопки.")
    return ADMIN_MENU


async def create_account_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_student_login"] = update.message.text.strip()
    await update.message.reply_text("Теперь введите пароль для этого ученика:")
    return CREATE_ACCOUNT_PASSWORD


async def create_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    login = context.user_data.get("new_student_login")
    password = update.message.text.strip()

    if not login or not password:
        await update.message.reply_text("Ошибка. Попробуйте заново через /admin.")
        return ConversationHandler.END

    ok = create_student_account(login, password)

    if ok:
        await update.message.reply_text(
            f"✅ Аккаунт создан.\n\nЛогин: {login}\nПароль: {password}",
            reply_markup=admin_menu_kb()
        )
    else:
        await update.message.reply_text(
            "❌ Такой логин уже существует.",
            reply_markup=admin_menu_kb()
        )

    context.user_data.pop("new_student_login", None)
    return ADMIN_MENU


async def create_test_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "Назад в меню":
        await update.message.reply_text("Админ-панель:", reply_markup=admin_menu_kb())
        return ADMIN_MENU

    if text == "Итоговые тесты":
        context.user_data["new_test_subject"] = "finals"
        context.user_data["new_test_is_final"] = 1
    else:
        code = subject_code(text)
        if not code:
            await update.message.reply_text("Выберите раздел через кнопки.")
            return CREATE_TEST_SUBJECT
        context.user_data["new_test_subject"] = code
        context.user_data["new_test_is_final"] = 0

    await update.message.reply_text("Введите название темы или теста:")
    return CREATE_TEST_NAME


async def create_test_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_test_title"] = update.message.text.strip()
    await update.message.reply_text(
        "Выберите доступ:",
        reply_markup=access_kb()
    )
    return CREATE_TEST_ACCESS


async def create_test_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()

    if text == "бесплатный":
        context.user_data["new_test_access"] = "free"
    elif text == "платный":
        context.user_data["new_test_access"] = "paid"
    else:
        await update.message.reply_text("Нажмите: Бесплатный или Платный")
        return CREATE_TEST_ACCESS

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
        reply_markup=ReplyKeyboardRemove()
    )
    return CREATE_TEST_QUESTIONS


async def create_test_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()

    subject = context.user_data.get("new_test_subject")
    title = context.user_data.get("new_test_title")
    access_type = context.user_data.get("new_test_access")
    is_final = context.user_data.get("new_test_is_final", 0)

    if not subject or not title or not access_type:
        await update.message.reply_text("Ошибка данных. Начните заново через /admin.")
        return ConversationHandler.END

    try:
        parsed_questions = parse_bulk_questions(raw)
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка в формате вопросов:\n\n{e}\n\nПопробуйте отправить заново."
        )
        return CREATE_TEST_QUESTIONS

    real_subject = "finals" if is_final else subject
    test_id = create_test(
        subject=real_subject,
        title=title,
        access_type=access_type,
        is_final=is_final,
        created_by=update.effective_user.id
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
        f"Раздел: {subject_label(real_subject)}\n"
        f"Название: {title}\n"
        f"Доступ: {'Бесплатный' if access_type == 'free' else 'Платный'}\n"
        f"Вопросов: {len(parsed_questions)}",
        reply_markup=admin_menu_kb()
    )

    for key in [
        "new_test_subject",
        "new_test_title",
        "new_test_access",
        "new_test_is_final",
    ]:
        context.user_data.pop(key, None)

    return ADMIN_MENU


async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=main_menu_kb()
    )
    return ConversationHandler.END


# =========================================================
# ДОП КОМАНДЫ
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "/start — начать\n"
        "/help — помощь\n"
        "/admin — админ-панель\n"
        "/cancel — отмена текущего действия"
    )
    await update.message.reply_text(text)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=main_menu_kb()
    )
    return ConversationHandler.END


# =========================================================
# ОБРАБОТКА ПРОЧИХ СООБЩЕНИЙ
# =========================================================

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text in {"История Казахстана", "Биология", "Химия", "Математическая грамотность"}:
        await show_subject_topics(update, context)
        return

    if text == "Войти":
        await ask_login(update, context)
        return

    await update.message.reply_text(
        "Пожалуйста, используйте кнопки меню или команду /start."
    )


# =========================================================
# MAIN
# =========================================================

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Логин пользователя
    login_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^Войти$"), ask_login)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_login)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    # Админ-панель
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={
            ADMIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_router)],
            CREATE_ACCOUNT_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_account_login)],
            CREATE_ACCOUNT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_account_password)],
            CREATE_TEST_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_subject)],
            CREATE_TEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_name)],
            CREATE_TEST_ACCESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_access)],
            CREATE_TEST_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_test_questions)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_admin),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(admin_conv)
    app.add_handler(login_conv)

    app.add_handler(CallbackQueryHandler(open_test_callback, pattern=r"^open_test:\d+$"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^answer:\d+:\d+$"))

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
