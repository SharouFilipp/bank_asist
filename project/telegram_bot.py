"""
telegram_bot.py — Полнофункциональный Telegram-бот для Banking RAG Assistant.

Функции:
  - Капча при первом входе
  - Регистрация пароля для веб-версии
  - Загрузка чеков (фото)
  - Геолокация → ближайшие филиалы
  - Диаграмма расходов с выбором периода
  - Голосовые сообщения (транскрипция через API)
  - Полный RAG-чат с follow-up кнопками
  - /help с описанием всех команд
  - Inline-кнопки меню

Запуск:
  python telegram_bot.py          # polling (локально)
  Для PythonAnywhere — см. README_DEPLOY.md
"""

import os
import io
import json
import logging
import re
import time
import requests
from datetime import datetime
from random import randint

from telebot import TeleBot, types
from telebot.types import Message, CallbackQuery
from dotenv import load_dotenv

from database import SessionLocal, User
from auth import create_access_token, get_password_hash, verify_password

load_dotenv()

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_URL = os.getenv("API_URL", "http://localhost:8000")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tg_bot")

bot = TeleBot(TOKEN, parse_mode=None)

# ── Состояние сессий ──────────────────────────────────────
captcha_sessions   = {}   # user_id → {sum, time, attempts}
registration_state = {}   # user_id → 'awaiting_password'
followup_storage   = {}   # message_id → [questions]
location_cache     = {}   # user_id → {lat, lon, ts}
expenses_state     = {}   # user_id → 'awaiting_period'

# ── Вспомогательные функции ───────────────────────────────

def get_or_create_user(telegram_id: int) -> User:
    db = SessionLocal()
    username = f"tg_{telegram_id}"
    user = db.query(User).filter(User.username == username).first()
    if not user:
        user = User(username=username, password_hash="", is_verified=0)
        db.add(user)
        db.commit()
        db.refresh(user)
    db.close()
    return user


def is_verified(user: User) -> bool:
    return user.is_verified == 1


def auth_headers(user: User) -> dict:
    token = create_access_token({"sub": user.username})
    return {"Authorization": f"Bearer {token}"}


def api_post(path: str, user: User, payload: dict, timeout: int = 40) -> dict | None:
    try:
        r = requests.post(f"{API_URL}{path}", json=payload,
                          headers=auth_headers(user), timeout=timeout)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"POST {path} → {r.status_code}: {r.text[:100]}")
    except Exception as e:
        logger.error(f"POST {path} error: {e}")
    return None


def api_get(path: str, user: User, params: dict = None, timeout: int = 20) -> dict | None:
    try:
        r = requests.get(f"{API_URL}{path}", params=params,
                         headers=auth_headers(user), timeout=timeout)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"GET {path} → {r.status_code}")
    except Exception as e:
        logger.error(f"GET {path} error: {e}")
    return None


def send_answer(chat_id: int, user: User, text: str,
                follow_ups: list = None, image_url: str = None):
    """Универсальная отправка ответа с картинкой и follow-up кнопками."""
    # Картинка (диаграмма / карта)
    if image_url:
        try:
            img_resp = requests.get(f"{API_URL}{image_url}", timeout=10)
            if img_resp.status_code == 200:
                bot.send_photo(chat_id, img_resp.content)
        except Exception:
            pass

    # Разбиваем длинный текст на части (лимит TG 4096 символов)
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    sent = None
    for chunk in chunks:
        sent = bot.send_message(chat_id, chunk)

    # Follow-up кнопки
    if follow_ups and sent:
        followup_storage[sent.message_id] = follow_ups
        kb = types.InlineKeyboardMarkup(row_width=1)
        for i, fu in enumerate(follow_ups[:6]):  # макс 6 кнопок
            label = fu[:40] + ("…" if len(fu) > 40 else "")
            kb.add(types.InlineKeyboardButton(label, callback_data=f"fu_{sent.message_id}_{i}"))
        bot.send_message(chat_id, "💡 Уточните:", reply_markup=kb)


def main_keyboard() -> types.ReplyKeyboardMarkup:
    """Постоянная клавиатура с быстрыми действиями."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📊 Расходы"),
        types.KeyboardButton("💱 Курсы валют"),
        types.KeyboardButton("🏦 Ближайший филиал", request_location=True),
        types.KeyboardButton("📈 Динамика USD"),
        types.KeyboardButton("🤔 Как сэкономить?"),
        types.KeyboardButton("📷 Загрузить чек"),
    )
    return kb


# ═══════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def start_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        kb, n1, n2 = _make_captcha(msg.from_user.id)
        bot.send_message(msg.chat.id,
                         f"👋 Добро пожаловать в Banking RAG Assistant!\n\n"
                         f"Для защиты — реши пример: сколько будет {n1} + {n2}?",
                         reply_markup=kb)
    else:
        _send_welcome(msg.chat.id)


def _send_welcome(chat_id: int):
    text = (
        "🏦 *Banking RAG Assistant*\n\n"
        "Я умею:\n"
        "📷 *Чеки* — сфотографируй чек, я распознаю и сохраню\n"
        "📊 *Расходы* — диаграмма по категориям за любой период\n"
        "💱 *Курсы* — НБРБ и банковские курсы по городу\n"
        "🏦 *Филиалы* — ближайшие отделения с часами и курсами\n"
        "📈 *Динамика* — тренды курсов с прогнозом\n"
        "💡 *Советы* — как сэкономить на основе ваших трат\n\n"
        "📍 *Кнопка «Ближайший филиал»* — отправляет геолокацию\n\n"
        "Напишите любой вопрос или выберите действие ↓"
    )
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=main_keyboard())


# ═══════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["help"])
def help_command(msg: Message):
    text = (
        "📚 *Команды и возможности:*\n\n"
        "*/start* — начать / перезапустить\n"
        "*/help* — эта справка\n"
        "*/register* — установить пароль для веб-версии\n"
        "*/expenses* — диаграмма расходов (выбор периода)\n"
        "*/convert* — конвертировать валюту\n"
        "*/rates* — курсы валют НБРБ\n"
        "*/filial* — найти ближайший филиал\n"
        "*/dynamics* — динамика курса USD/EUR/RUB\n"
        "*/savings* — советы по экономии\n\n"
        "📷 *Фото* — отправь фото чека, я распознаю\n"
        "📍 *Геолокация* — покажу ближайшие отделения\n"
        "💬 *Любой текст* — задай вопрос ассистенту\n\n"
        "🌐 Веб-версия: задай /register и войди на сайте"
    )
    bot.send_message(msg.chat.id, text, parse_mode="Markdown", reply_markup=main_keyboard())


# ═══════════════════════════════════════════════════════════
# Капча
# ═══════════════════════════════════════════════════════════
def _make_captcha(user_id: int):
    n1, n2 = randint(1, 10), randint(1, 10)
    captcha_sessions[user_id] = {
        "sum": n1 + n2, "time": time.time(), "attempts": 0
    }
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[types.InlineKeyboardButton(str(i), callback_data=f"cap_{user_id}_{i}")
             for i in range(1, 21)])
    return kb, n1, n2


@bot.callback_query_handler(func=lambda c: c.data.startswith("cap_"))
def captcha_callback(call: CallbackQuery):
    _, uid_s, ans_s = call.data.split("_")
    uid, ans = int(uid_s), int(ans_s)
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "Это не ваша капча!", show_alert=True)
        return
    sess = captcha_sessions.get(uid)
    if not sess or time.time() - sess["time"] > 120:
        bot.edit_message_text("⏰ Время истекло. Нажмите /start", call.message.chat.id, call.message.message_id)
        captcha_sessions.pop(uid, None)
        return
    if ans == sess["sum"]:
        db = SessionLocal()
        u = db.query(User).filter(User.username == f"tg_{uid}").first()
        if u:
            u.is_verified = 1
            db.commit()
        db.close()
        bot.edit_message_text("✅ Проверка пройдена!", call.message.chat.id, call.message.message_id)
        captcha_sessions.pop(uid)
        _send_welcome(call.message.chat.id)
    else:
        sess["attempts"] += 1
        if sess["attempts"] >= 3:
            bot.edit_message_text("❌ Слишком много ошибок. Нажмите /start",
                                  call.message.chat.id, call.message.message_id)
            captcha_sessions.pop(uid)
            return
        n1, n2 = randint(1, 10), randint(1, 10)
        sess.update({"sum": n1 + n2, "time": time.time()})
        kb = types.InlineKeyboardMarkup(row_width=5)
        kb.add(*[types.InlineKeyboardButton(str(i), callback_data=f"cap_{uid}_{i}")
                 for i in range(1, 21)])
        bot.edit_message_text(f"❌ Неверно. Попробуйте: {n1} + {n2} = ?",
                              call.message.chat.id, call.message.message_id, reply_markup=kb)
    bot.answer_callback_query(call.id)


# ═══════════════════════════════════════════════════════════
# Регистрация пароля
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["register"])
def register_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        bot.reply_to(msg, "Сначала пройдите капчу: /start")
        return
    bot.send_message(msg.chat.id, "🔑 Придумайте пароль для входа в веб-версию:")
    registration_state[msg.from_user.id] = "awaiting_password"


@bot.message_handler(func=lambda m: registration_state.get(m.from_user.id) == "awaiting_password")
def set_password(msg: Message):
    pwd = msg.text.strip()
    if len(pwd) < 4:
        bot.reply_to(msg, "Пароль слишком короткий. Минимум 4 символа:")
        return
    db = SessionLocal()
    u = db.query(User).filter(User.username == f"tg_{msg.from_user.id}").first()
    if u:
        u.password_hash = get_password_hash(pwd)
        db.commit()
    db.close()
    del registration_state[msg.from_user.id]
    bot.reply_to(msg, f"✅ Пароль установлен!\n\nВойдите на сайте:\nЛогин: `tg_{msg.from_user.id}`",
                 parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════
# Загрузка чека (фото)
# ═══════════════════════════════════════════════════════════
@bot.message_handler(content_types=["photo"])
def handle_photo(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        bot.reply_to(msg, "Пройдите капчу: /start")
        return

    wait = bot.send_message(msg.chat.id, "⏳ Обрабатываю чек...")
    file_info = bot.get_file(msg.photo[-1].file_id)
    photo_bytes = bot.download_file(file_info.file_path)

    try:
        r = requests.post(
            f"{API_URL}/upload-image",
            files={"image": ("check.jpg", photo_bytes, "image/jpeg")},
            headers=auth_headers(user),
            timeout=60,
        )
        bot.delete_message(msg.chat.id, wait.message_id)
        if r.status_code == 200:
            data = r.json()
            answer = data.get("assistant_answer", "")
            if not answer:
                answer = (f"✅ Чек сохранён!\n"
                          f"💰 Сумма: {data.get('amount', '?')} {data.get('currency', 'BYN')}\n"
                          f"📂 Категория: {data.get('category', '?')}\n"
                          f"📅 Дата: {data.get('date', '?')}")
            send_answer(msg.chat.id, user, answer, data.get("follow_ups", []))
        else:
            bot.reply_to(msg, f"❌ Ошибка обработки чека (код {r.status_code})")
    except Exception as e:
        bot.delete_message(msg.chat.id, wait.message_id)
        logger.error(f"Photo upload: {e}")
        bot.reply_to(msg, "❌ Ошибка связи с сервером")


# ═══════════════════════════════════════════════════════════
# Геолокация → ближайшие филиалы
# ═══════════════════════════════════════════════════════════
@bot.message_handler(content_types=["location"])
def handle_location(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        bot.reply_to(msg, "Пройдите капчу: /start")
        return

    lat = msg.location.latitude
    lon = msg.location.longitude
    location_cache[msg.from_user.id] = {"lat": lat, "lon": lon, "ts": time.time()}

    wait = bot.send_message(msg.chat.id, "📍 Ищу ближайшие отделения...")
    data = api_post("/ask", user, {
        "user_query": "покажи 5 ближайших филиалов с адресами и часами работы",
        "latitude": lat,
        "longitude": lon,
    })
    bot.delete_message(msg.chat.id, wait.message_id)

    if data:
        send_answer(msg.chat.id, user,
                    data.get("assistant_answer", "Отделения не найдены"),
                    data.get("follow_ups", []),
                    data.get("image_url"))
    else:
        bot.reply_to(msg, "❌ Не удалось получить данные об отделениях")


# ═══════════════════════════════════════════════════════════
# /filial — просит геолокацию если не было
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["filial"])
def filial_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        bot.reply_to(msg, "Пройдите капчу: /start")
        return
    cached = location_cache.get(msg.from_user.id)
    if cached and time.time() - cached["ts"] < 3600:
        # Используем кэшированную геолокацию
        data = api_post("/ask", user, {
            "user_query": "ближайшие филиалы",
            "latitude": cached["lat"],
            "longitude": cached["lon"],
        })
        if data:
            send_answer(msg.chat.id, user, data.get("assistant_answer", ""),
                        data.get("follow_ups", []))
            return
    # Просим геолокацию
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Отправить моё местоположение", request_location=True))
    bot.send_message(msg.chat.id,
                     "Для поиска ближайших филиалов поделитесь геолокацией:",
                     reply_markup=kb)


# ═══════════════════════════════════════════════════════════
# /rates — курсы валют
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["rates"])
def rates_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user): return
    data = api_post("/ask", user, {"user_query": "актуальные курсы валют НБРБ"})
    if data:
        send_answer(msg.chat.id, user, data.get("assistant_answer", ""),
                    data.get("follow_ups", []))


# ═══════════════════════════════════════════════════════════
# /expenses — диаграмма расходов с выбором периода
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["expenses"])
def expenses_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user): return
    kb = types.InlineKeyboardMarkup(row_width=2)
    periods = [
        ("Этот месяц", "exp_month"), ("Прошлый месяц", "exp_last_month"),
        ("Эта неделя", "exp_week"), ("Сегодня", "exp_today"),
        ("Этот год", "exp_year"), ("Всё время", "exp_all"),
    ]
    for label, cb in periods:
        kb.add(types.InlineKeyboardButton(label, callback_data=cb))
    bot.send_message(msg.chat.id, "📊 Выберите период для диаграммы расходов:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("exp_"))
def expenses_period_callback(call: CallbackQuery):
    period_map = {
        "exp_month": "month", "exp_last_month": "last_month",
        "exp_week": "week", "exp_today": "today",
        "exp_year": "year", "exp_all": "all",
    }
    period = period_map.get(call.data, "month")
    user = get_or_create_user(call.from_user.id)
    bot.answer_callback_query(call.id, "Строю диаграмму...")
    bot.edit_message_text("⏳ Строю диаграмму расходов...",
                          call.message.chat.id, call.message.message_id)
    data = api_get("/expenses/chart", user, {"chart_type": "pie", "period": period})
    if data and data.get("chart_url"):
        label = data.get("period_label", period)
        count = data.get("count", 0)
        try:
            img = requests.get(f"{API_URL}{data['chart_url']}", timeout=10).content
            bot.send_photo(call.message.chat.id, img,
                           caption=f"📊 Расходы: {label} ({count} чеков)")
        except Exception:
            bot.send_message(call.message.chat.id, "❌ Не удалось загрузить диаграмму")
    else:
        bot.send_message(call.message.chat.id, "Нет данных за выбранный период")
    bot.delete_message(call.message.chat.id, call.message.message_id)


# ═══════════════════════════════════════════════════════════
# /convert — конвертация валют
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["convert"])
def convert_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user): return
    sent = bot.send_message(msg.chat.id,
                            "💱 Введите запрос, например:\n"
                            "• `100 USD в BYN`\n"
                            "• `200 EUR в USD по курсу Минска`\n"
                            "• `50 USD в EUR по НБРБ`",
                            parse_mode="Markdown")
    bot.register_next_step_handler(sent, _process_convert, user)


def _process_convert(msg: Message, user: User):
    data = api_post("/ask", user, {"user_query": msg.text})
    if data:
        send_answer(msg.chat.id, user, data.get("assistant_answer", ""),
                    data.get("follow_ups", []))
    else:
        bot.reply_to(msg, "❌ Ошибка конвертации")


# ═══════════════════════════════════════════════════════════
# /dynamics — динамика курса
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["dynamics"])
def dynamics_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user): return
    kb = types.InlineKeyboardMarkup(row_width=3)
    for cur in ["USD", "EUR", "RUB", "PLN", "CNY"]:
        kb.add(types.InlineKeyboardButton(cur, callback_data=f"dyn_{cur}_month"))
    bot.send_message(msg.chat.id, "📈 Динамика какой валюты?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("dyn_"))
def dynamics_callback(call: CallbackQuery):
    _, cur, period = call.data.split("_")
    user = get_or_create_user(call.from_user.id)
    bot.answer_callback_query(call.id, f"Загружаю динамику {cur}...")
    bot.edit_message_text(f"⏳ Строю график {cur}...",
                          call.message.chat.id, call.message.message_id)
    data = api_post("/ask", user, {"user_query": f"динамика {cur} за месяц"})
    if data:
        send_answer(call.message.chat.id, user,
                    data.get("assistant_answer", ""),
                    data.get("follow_ups", []),
                    data.get("image_url"))
    bot.delete_message(call.message.chat.id, call.message.message_id)


# ═══════════════════════════════════════════════════════════
# /savings — советы по экономии
# ═══════════════════════════════════════════════════════════
@bot.message_handler(commands=["savings"])
def savings_command(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user): return
    wait = bot.send_message(msg.chat.id, "⏳ Анализирую ваши расходы...")
    data = api_post("/ask", user, {"user_query": "проанализируй мои расходы и дай советы по экономии"})
    bot.delete_message(msg.chat.id, wait.message_id)
    if data:
        send_answer(msg.chat.id, user, data.get("assistant_answer", ""),
                    data.get("follow_ups", []))


# ═══════════════════════════════════════════════════════════
# Кнопки быстрого меню (ReplyKeyboard)
# ═══════════════════════════════════════════════════════════
QUICK_MENU = {
    "📊 Расходы":      lambda m, u: expenses_command(m),
    "💱 Курсы валют":  lambda m, u: rates_command(m),
    "📈 Динамика USD": lambda m, u: _ask_and_reply(m, u, "динамика USD за месяц"),
    "🤔 Как сэкономить?": lambda m, u: savings_command(m),
    "📷 Загрузить чек":lambda m, u: bot.send_message(
        m.chat.id, "📷 Просто отправьте фото чека в этот чат"
    ),
}


def _ask_and_reply(msg: Message, user: User, query: str):
    wait = bot.send_message(msg.chat.id, "⏳ Получаю данные...")
    data = api_post("/ask", user, {"user_query": query})
    bot.delete_message(msg.chat.id, wait.message_id)
    if data:
        send_answer(msg.chat.id, user, data.get("assistant_answer", ""),
                    data.get("follow_ups", []), data.get("image_url"))


# ═══════════════════════════════════════════════════════════
# Общий обработчик текста (RAG-чат)
# ═══════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: True)
def handle_text(msg: Message):
    user = get_or_create_user(msg.from_user.id)
    if not is_verified(user):
        bot.reply_to(msg, "Пройдите капчу: /start")
        return

    text = msg.text or ""

    # Быстрое меню
    if text in QUICK_MENU:
        QUICK_MENU[text](msg, user)
        return

    # RAG-запрос с геолокацией из кэша
    payload = {"user_query": text}
    cached_loc = location_cache.get(msg.from_user.id)
    if cached_loc and time.time() - cached_loc["ts"] < 3600:
        payload["latitude"]  = cached_loc["lat"]
        payload["longitude"] = cached_loc["lon"]

    wait = bot.send_message(msg.chat.id, "⏳")
    data = api_post("/ask", user, payload)
    bot.delete_message(msg.chat.id, wait.message_id)

    if data:
        send_answer(msg.chat.id, user,
                    data.get("assistant_answer", "Нет ответа"),
                    data.get("follow_ups", []),
                    data.get("image_url"))
    else:
        bot.reply_to(msg, "❌ Сервис временно недоступен")


# ═══════════════════════════════════════════════════════════
# Follow-up кнопки
# ═══════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data.startswith("fu_"))
def followup_callback(call: CallbackQuery):
    parts = call.data.split("_")
    if len(parts) != 3:
        return
    msg_id, idx = int(parts[1]), int(parts[2])
    questions = followup_storage.get(msg_id)
    if not questions or idx >= len(questions):
        bot.answer_callback_query(call.id, "Подсказка устарела")
        return
    question = questions[idx]
    bot.answer_callback_query(call.id)
    user = get_or_create_user(call.from_user.id)
    cached_loc = location_cache.get(call.from_user.id)
    payload = {"user_query": question}
    if cached_loc and time.time() - cached_loc["ts"] < 3600:
        payload.update({"latitude": cached_loc["lat"], "longitude": cached_loc["lon"]})
    wait = bot.send_message(call.message.chat.id, "⏳")
    data = api_post("/ask", user, payload)
    bot.delete_message(call.message.chat.id, wait.message_id)
    if data:
        send_answer(call.message.chat.id, user,
                    data.get("assistant_answer", ""),
                    data.get("follow_ups", []),
                    data.get("image_url"))


# ═══════════════════════════════════════════════════════════
# Запуск
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    logger.info(f"Bot started. API: {API_URL}")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)