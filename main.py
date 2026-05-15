 import asyncio
import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, 
                           InlineKeyboardButton, PollAnswer)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
TOKEN = "ВАШ_ТОКЕН_БОТА"
ADMIN_ID = 123456789  # Твой ID (узнай в @userinfobot)
MANAGER_USER = "@твой_менеджер"

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Имитация базы данных (в реальном проекте используй SQLite/PostgreSQL)
db = {
    "quizzes": {}, # id: {title, questions, timer, is_private}
    "active_tests": {}, # chat_id: {quiz_id, current_step, participants, scores}
    "user_access": set() # id пользователей с платным доступом
}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def parse_text_to_quiz(text):
    blocks = text.strip().split('\n\n')
    questions = []
    for block in blocks:
        lines = block.split('\n')
        q_text = lines[0].replace(':', '').strip()
        options = []
        correct_id = 0
        for i, line in enumerate(lines[1:]):
            if '*' in line:
                correct_id = i
            clean_opt = re.sub(r'^[A-ДA-D]\)\s*', '', line).replace('*', '').strip()
            options.append(clean_opt)
        questions.append({"q": q_text, "opts": options, "corr": correct_id})
    return questions

# --- ХЭНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_ref = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    text = (f"Здравствуйте, {user_ref}\n"
            f"Рад приветствовать! Вижу, вы усиленно готовитесь к ЕНТ и желаете практиковаться.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Выбрать тему подготоки", callback_query_data="menu_themes")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "menu_themes")
async def show_themes(callback: CallbackQuery):
    # Пример списка тем
    kb_list = []
    for q_id, q_data in db["quizzes"].items():
        lock = "🔒 " if q_data['is_private'] else ""
        kb_list.append([InlineKeyboardButton(text=f"{lock}{q_data['title']}", callback_query_data=f"info_{q_id}")])
    
    if not kb_list:
        await callback.answer("Тестов пока нет. Админ должен их добавить.")
        return

    await callback.message.edit_text("Выберите тему для практики:", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_list))

@dp.callback_query(F.data.startswith("info_"))
async def quiz_info(callback: CallbackQuery):
    q_id = callback.data.split("_")[1]
    quiz = db["quizzes"][q_id]

    if quiz['is_private'] and callback.from_user.id not in db["user_access"] and callback.from_user.id != ADMIN_ID:
        await callback.message.edit_text(f"⛔ Этот тест платный/закрытый.\nДля доступа пишите: {MANAGER_USER}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ Назад", callback_query_data="menu_themes")]
                                         ]))
        return

    text = f"Тема: {quiz['title']}\nВопросов: {len(quiz['questions'])}\nТаймер: {quiz['timer']} сек."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить в этом чате", callback_query_data=f"lobby_{q_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_query_data="menu_themes")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

# --- ЛОГИКА ТЕСТА (ГРУППА) ---

@dp.callback_query(F.data.startswith("lobby_"))
async def start_lobby(callback: CallbackQuery):
    q_id = callback.data.split("_")[1]
    chat_id = callback.message.chat.id
    
    db["active_tests"][chat_id] = {
        "quiz_id": q_id,
        "participants": set(),
        "scores": {}, # user_id: name
        "current_q": 0
    }
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Пройти тест", callback_query_data=f"join_{chat_id}")],
        [InlineKeyboardButton(text="🛑 Остановить", callback_query_data="stop_test")]
    ])
    
    await callback.message.answer(f"🏁 Сбор участников на тест по теме: {db['quizzes'][q_id]['title']}\n"
                                  f"Нужно минимум 2 человека!", reply_markup=kb)

@dp.callback_query(F.data.startswith("join_"))
async def join_test(callback: CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    if chat_id not in db["active_tests"]: return
    
    user_id = callback.from_user.id
    db["active_tests"][chat_id]["participants"].add(user_id)
    db["active_tests"][chat_id]["scores"][user_id] = {"name": callback.from_user.first_name, "points": 0}
    
    count = len(db["active_tests"][chat_id]["participants"])
    await callback.answer(f"Вы в игре! Участников: {count}")
    
    if count >= 2: # Если набралось 2 человека - стартуем через 5 секунд
        await callback.message.answer("🔥 Минимальное кол-во набрано! Начинаем через 5 секунд...")
        scheduler.add_job(send_question, 'date', 
                          run_date=datetime.now() + timedelta(seconds=5), 
                          args=[chat_id])

async def send_question(chat_id):
    test = db["active_tests"].get(chat_id)
    if not test: return
    
    quiz = db["quizzes"][test["quiz_id"]]
    q_idx = test["current_q"]
    
    if q_idx >= len(quiz["questions"]):
        # Конец теста
        res_text = "🏆 **Итоги теста:**\n"
        for uid, data in test["scores"].items():
            res_text += f"👤 {data['name']}: {data['points']} баллов\n"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Продолжить", callback_query_data="menu_themes")]])
        await bot.send_message(chat_id, res_text, reply_markup=kb)
        del db["active_tests"][chat_id]
        return

    q = quiz["questions"][q_idx]
    poll = await bot.send_poll(
        chat_id=chat_id,
        question=f"Вопрос {q_idx+1}/{len(quiz['questions'])}: {q['q']}",
        options=q['opts'],
        type='quiz',
        correct_option_id=q['corr'],
        open_period=quiz['timer'],
        is_anonymous=False,
        protect_content=True
    )
    
    test["current_q"] += 1
    # Планируем следующий вопрос
    scheduler.add_job(send_question, 'date', 
                      run_date=datetime.now() + timedelta(seconds=quiz['timer'] + 3), 
                      args=[chat_id])

@dp.poll_answer()
async def handle_poll_answer(answer: PollAnswer):
    # Логика начисления очков
    for chat_id, test in db["active_tests"].items():
        if answer.user.id in test["participants"]:
            quiz = db["quizzes"][test["quiz_id"]]
            curr_q = test["current_q"] - 1
            if answer.option_ids[0] == quiz["questions"][curr_q]["corr"]:
                test["scores"][answer.user.id]["points"] += 1

# --- АДМИН ПАНЕЛЬ ---

@dp.message(F.text.startswith("ДОБАВИТЬ_ТЕСТ"))
async def admin_add_test(message: Message):
    if message.from_user.id != ADMIN_ID: return
    
    try:
        # Формат: ДОБАВИТЬ_ТЕСТ | Название | Таймер | Приватный(0/1)
        # Далее сам текст теста
        header, content = message.text.split('\n', 1)
        _, title, timer, is_priv = header.split('|')
        
        q_id = str(len(db["quizzes"]) + 1)
        db["quizzes"][q_id] = {
            "title": title.strip(),
            "timer": int(timer),
            "is_private": bool(int(is_priv)),
            "questions": parse_text_to_quiz(content)
        }
        await message.answer(f"✅ Тест '{title}' успешно добавлен! ID: {q_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка в формате: {e}")

async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
