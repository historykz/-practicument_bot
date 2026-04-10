import os
import sqlite3
from telegram import *
from telegram.ext import *

BOT_TOKEN = os.getenv("BOT_TOKEN")

conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS tests(
id INTEGER PRIMARY KEY,
subject TEXT,
title TEXT,
timer INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS questions(
id INTEGER PRIMARY KEY,
test_id INTEGER,
question TEXT,
a TEXT,
b TEXT,
c TEXT,
d TEXT,
correct INTEGER
)
""")

conn.commit()

main_menu = ReplyKeyboardMarkup(
[
["История Казахстана","Биология"],
["Химия","Математическая грамотность"],
["Войти"]
],
resize_keyboard=True
)

back_menu = ReplyKeyboardMarkup(
[
["Назад"]
],
resize_keyboard=True
)

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
    "Здравствуйте!\n\nВыберите предмет для практики",
    reply_markup=main_menu
    )

async def login(update:Update,context:ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
    "Введите логин и пароль\n\nпример:\nlogin,1234",
    reply_markup=back_menu
    )

async def subjects(update:Update,context:ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "Назад":

        await update.message.reply_text(
        "Выберите предмет",
        reply_markup=main_menu
        )
        return

    if text == "Войти":
        await login(update,context)
        return

    tests = cur.execute(
    "SELECT * FROM tests WHERE subject=?",
    (text,)
    ).fetchall()

    if not tests:
        await update.message.reply_text("Тестов пока нет")
        return

    keyboard = []

    for t in tests:
        keyboard.append([
        InlineKeyboardButton(
        t[2],
        callback_data=f"test_{t[0]}"
        )
        ])

    keyboard.append([
    InlineKeyboardButton("⬅️ Назад","back_subject")
    ])

    await update.message.reply_text(
    "Выберите тему",
    reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def back_subject(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
    "Выберите предмет",
    reply_markup=main_menu
    )

async def start_test(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    test_id = int(query.data.split("_")[1])

    test = cur.execute(
    "SELECT * FROM tests WHERE id=?",
    (test_id,)
    ).fetchone()

    questions = cur.execute(
    "SELECT * FROM questions WHERE test_id=?",
    (test_id,)
    ).fetchall()

    context.user_data["test"] = {
    "questions":questions,
    "index":0,
    "score":0,
    "timer":test[3],
    "test_id":test_id
    }

    await send_question(query.message.chat_id,context)

async def send_question(chat_id,context):

    data = context.user_data["test"]

    if data["index"] >= len(data["questions"]):

        score = data["score"]
        total = len(data["questions"])

        await context.bot.send_message(
        chat_id,
        f"Тест завершен\n\nРезультат {score}/{total}",
        reply_markup=main_menu
        )

        context.user_data.clear()
        return

    q = data["questions"][data["index"]]

    poll = await context.bot.send_poll(
    chat_id,
    q[2],
    [q[3],q[4],q[5],q[6]],
    type="quiz",
    correct_option_id=q[7],
    open_period=data["timer"]
    )

    context.user_data["poll"] = poll.poll.id

    keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("⏹ Завершить тест","stop_test")]
    ])

    await context.bot.send_message(
    chat_id,
    f"⏱ {data['timer']} секунд",
    reply_markup=keyboard
    )

async def poll_answer(update:Update,context:ContextTypes.DEFAULT_TYPE):

    answer = update.poll_answer

    if "test" not in context.user_data:
        return

    data = context.user_data["test"]

    question = data["questions"][data["index"]]

    if answer.option_ids[0] == question[7]:
        data["score"] += 1

    data["index"] += 1

    await send_question(answer.user.id,context)

async def stop_test(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = context.user_data["test"]

    score = data["score"]
    total = len(data["questions"])

    keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ Продолжить","continue_test")],
    [InlineKeyboardButton("Завершить","finish_test")]
    ])

    await query.message.reply_text(
    f"Текущий результат {score}/{total}",
    reply_markup=keyboard
    )

async def continue_test(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    await send_question(query.message.chat_id,context)

async def finish_test(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data = context.user_data["test"]

    score = data["score"]
    total = len(data["questions"])

    await query.message.reply_text(
    f"Тест завершен\n\n{score}/{total}",
    reply_markup=main_menu
    )

    context.user_data.clear()

app = Application.builder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start",start))

app.add_handler(MessageHandler(
filters.TEXT &
~filters.COMMAND,
subjects
))

app.add_handler(CallbackQueryHandler(start_test,pattern="test_"))
app.add_handler(CallbackQueryHandler(stop_test,pattern="stop_test"))
app.add_handler(CallbackQueryHandler(continue_test,pattern="continue_test"))
app.add_handler(CallbackQueryHandler(finish_test,pattern="finish_test"))
app.add_handler(CallbackQueryHandler(back_subject,pattern="back_subject"))

app.add_handler(PollAnswerHandler(poll_answer))

print("BOT STARTED")

app.run_polling()
