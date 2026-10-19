"""
bot.py — Telegram-бот расписания МГЭИ им. А.Д. Сахарова БГУ (aiogram 3.x).

Читает schedules.json (его создаёт parser.py) и показывает расписание выбранной группы.
Команды:
    /start            — выбор группы (инлайн-кнопки)
    /myid             — узнать свой Telegram ID (нужен для списка админов)
    /update_schedule  — (только админ) обновить расписание с сайта
"""

import asyncio
import html
import json
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonWebApp, Message, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web  # маленький веб-сервер (aiohttp уже установлен вместе с aiogram)

from parser import update_schedules  # функция обновления из нашего parser.py

# ============================================================
# Настройки
# ============================================================

# Токен лучше хранить в переменной окружения BOT_TOKEN; запасной вариант — вписать сюда.
BOT_TOKEN = ("BOT_TOKEN", "8850941894:AAERZNJhV8I3gvZ1dhxWYgy3oBBrpNJ8F3k")

# Telegram ID админов, которым разрешён /update_schedule. Свой ID покажет команда /myid.
ADMIN_IDS: set[int] = {123456789}

SCHEDULE_FILE = Path(__file__).with_name("schedules.json")
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MAX_MESSAGE_LEN = 3800  # лимит Telegram — 4096 символов, оставляем запас

# --- Mini App ---
# Публичный HTTPS-адрес, по которому доступен этот бот-сервер (например, адрес туннеля или хостинга).
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WEBAPP_DIR = Path(__file__).with_name("webapp")  # папка с index.html
PORT = int(os.getenv("PORT", "8080"))            # порт локального веб-сервера

router = Router()
update_lock = asyncio.Lock()  # чтобы два админа не запустили обновление одновременно


# ============================================================
# Работа с данными
# ============================================================

def load_schedules() -> dict:
    """Читает schedules.json. Файл читаем при каждом запросе, чтобы обновления подхватывались сразу."""
    try:
        with SCHEDULE_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def esc(value: str) -> str:
    """Экранирует спецсимволы HTML (<, >, &), чтобы Telegram не сломал разметку."""
    return html.escape(value or "")


# ============================================================
# Клавиатуры
# ============================================================

def groups_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура со списком групп (по 3 кнопки в ряд)."""
    builder = InlineKeyboardBuilder()
    for group in groups[:96]:  # у Telegram лимит — 100 кнопок на сообщение
        # callback_data — «скрытое» значение кнопки; по нему узнаем, какую группу выбрали
        builder.button(text=group, callback_data=f"group:{group}")
    builder.adjust(3)
    # Отдельной строкой — кнопка, открывающая Mini App (работает только с https-адресом)
    if WEBAPP_URL.startswith("https://"):
        builder.row(InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL)))
    return builder.as_markup()


def back_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «К списку групп» под расписанием."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К списку групп", callback_data="back")
    return builder.as_markup()


# ============================================================
# Форматирование расписания
# ============================================================

def format_day(day_index: int, lessons: list[dict]) -> str:
    """Оформляет один день недели."""
    lines = [f"<b>📌 {DAY_NAMES[day_index]}</b>"]
    if not lessons:
        lines.append("    — занятий нет")
    for number, lesson in enumerate(lessons, start=1):
        lines.append(f"{number}. 🕒 {esc(lesson.get('time'))}")
        lines.append(f"    <b>{esc(lesson.get('subject'))}</b>")
        details = []
        if lesson.get("room"):
            details.append(f"🚪 {esc(lesson['room'])}")
        if lesson.get("teacher"):
            details.append(f"👤 {esc(lesson['teacher'])}")
        if details:
            lines.append("    " + "  ·  ".join(details))
    return "\n".join(lines)


def format_week(group: str, week: dict) -> list[str]:
    """
    Собирает расписание всей недели и режет на сообщения не длиннее MAX_MESSAGE_LEN
    (резать можно только между днями, чтобы день не разорвался пополам).
    """
    blocks = [f"📚 <b>Группа {esc(group)}</b>"]
    for day_index in range(7):
        blocks.append(format_day(day_index, week.get(str(day_index), [])))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    chunks.append(current)
    return chunks


# ============================================================
# Обработчики
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    schedules = load_schedules()
    if not schedules:
        await message.answer(
            "Расписание пока не загружено 😔\n"
            "Администратор может выполнить команду /update_schedule."
        )
        return
    await message.answer(
        f"Привет, {esc(message.from_user.first_name)}! 👋\nВыбери свою группу:",
        reply_markup=groups_keyboard(sorted(schedules)),
    )


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.callback_query(F.data.startswith("group:"))
async def on_group_selected(callback: CallbackQuery):
    group = callback.data.removeprefix("group:")
    week = load_schedules().get(group)
    if week is None:
        await callback.answer("Группа не найдена. Нажми /start", show_alert=True)
        return

    await callback.answer()  # убирает «часики» на кнопке
    chunks = format_week(group, week)
    for i, chunk in enumerate(chunks):
        # Кнопку «назад» цепляем только к последнему сообщению
        markup = back_keyboard() if i == len(chunks) - 1 else None
        await callback.message.answer(chunk, reply_markup=markup)


@router.callback_query(F.data == "back")
async def on_back(callback: CallbackQuery):
    await callback.answer()
    schedules = load_schedules()
    await callback.message.answer("Выбери группу:", reply_markup=groups_keyboard(sorted(schedules)))


@router.message(Command("update_schedule"))
async def cmd_update_schedule(message: Message):
    # Проверка прав: только ID из ADMIN_IDS
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Эта команда доступна только администраторам.")
        return

    if update_lock.locked():
        await message.answer("⏳ Обновление уже выполняется, подожди немного.")
        return

    async with update_lock:
        status = await message.answer("⏳ Обновляю расписание, это может занять несколько минут...")
        try:
            # update_schedules() блокирующая (requests), поэтому запускаем её в отдельном потоке,
            # иначе бот «замёрзнет» и перестанет отвечать другим пользователям.
            result = await asyncio.to_thread(update_schedules)
        except Exception as exc:
            logging.exception("Ошибка обновления расписания")
            await status.edit_text(f"❌ Ошибка обновления:\n<code>{esc(str(exc)[:500])}</code>")
            return
        await status.edit_text(f"✅ Расписание обновлено. Загружено групп: {len(result)}")


# ============================================================
# Запуск
# ============================================================

async def handle_index(request: web.Request) -> web.StreamResponse:
    """Отдаёт страницу Mini App."""
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def handle_api(request: web.Request) -> web.Response:
    """Отдаёт расписание в JSON — его запрашивает страница Mini App."""
    return web.json_response(load_schedules(), headers={"Cache-Control": "no-store"})


async def start_web_server() -> web.AppRunner:
    """Запускает веб-сервер рядом с ботом (в том же процессе)."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/schedules", handle_api)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Веб-сервер Mini App запущен на порту %s", PORT)
    return runner


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    runner = await start_web_server()
    if WEBAPP_URL.startswith("https://"):
        # Кнопка «Расписание» рядом с полем ввода в чате с ботом
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="📅 Расписание", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    else:
        logging.warning("WEBAPP_URL не задан (нужен https://...) — кнопки Mini App не появятся")
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
