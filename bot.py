"""Telegram-бот гри «Слово дня».

Налаштування через .env:
  TELEGRAM_BOT_TOKEN=...       # токен від @BotFather (обовʼязково)
  SECRET_SALT=...              # випадковий рядок, від нього залежить слово дня (обовʼязково)
  ALLOWED_USERS=111,222        # Telegram ID гравців через кому
  ANNOUNCE_HOUR=9              # о котрій оголошувати нове слово (0-23)
  TIMEZONE=Europe/Kyiv        # часовий пояс

Запуск: python bot.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
from datetime import date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import engine
import storage

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("slovo")

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
}
ANNOUNCE_HOUR = int(os.getenv("ANNOUNCE_HOUR", "9"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))

# кеш готових ігор за днями (побудова важка ~3с, тож раз на день)
_games: dict[str, engine.Game] = {}
_games_lock = asyncio.Lock()


def today() -> date:
    return dt.datetime.now(TZ).date()


async def get_game(day: date) -> engine.Game:
    key = day.isoformat()
    async with _games_lock:
        if key not in _games:
            secret = engine.daily_secret(day)
            log.info("Будую гру на %s", key)
            _games[key] = await asyncio.to_thread(engine.Game, secret)
    return _games[key]


def player_name(update: Update) -> str:
    u = update.effective_user
    return u.full_name or u.username or "Гравець"


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропускає далі лише гравців зі списку ALLOWED_USERS."""
    user = update.effective_user
    if user and user.id in ALLOWED_USERS:
        return
    if user:
        log.warning("Чужий користувач %s (%s)", user.id, user.full_name)
        if update.message and update.effective_chat.type == "private":
            await update.message.reply_html(
                f"🔒 Це приватна гра. Твій Telegram ID: <code>{user.id}</code>"
            )
    raise ApplicationHandlerStop


# ---------------------------------------------------------------- команди

WELCOME = (
    "🎯 <b>Слово дня</b> — вгадай загадане слово!\n\n"
    "Щодня одне спільне слово. Пиши будь-яке слово — я скажу, "
    "наскільки воно близьке за змістом.\n"
    "Позиція <b>1</b> — вгадав. Чим менше число — тим ближче.\n"
    "🟢 ≤100   🟡 ≤1000   🔴 далі\n\n"
    "Команди:\n"
    "/hint — підказка\n"
    "/stats — статистика й таблиця\n"
    "/giveup — здатися й дізнатися слово\n"
    "/help — довідка"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.remember_user(update.effective_user.id, player_name(update))
    storage.add_subscriber(
        update.effective_chat.id, update.effective_user.id, player_name(update)
    )
    await update.message.reply_html(WELCOME)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(WELCOME)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    board = storage.scoreboard(today())
    if not board:
        await update.message.reply_text("Ще ніхто не грав. Напиши слово, щоб почати!")
        return
    lines = ["📊 <b>Таблиця</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(board):
        medal = medals[i] if i < len(medals) else "  "
        best = f", краще {s.best_attempts} спроб" if s.best_attempts else ""
        lines.append(
            f"{medal} <b>{html.escape(s.name)}</b>:{s.wins} перемог, "
            f"серія {s.streak}🔥{best}"
        )
    await update.message.reply_html("\n".join(lines))


async def cmd_hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid, day = update.effective_user.id, today()
    res = storage.get_result(uid, day)
    if res and (res["solved"] or res["gave_up"]):
        await update.message.reply_text("Сьогоднішня гра вже завершена 🙂 Чекай нове слово.")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    game = await get_game(day)
    guesses = storage.get_day_guesses(uid, day)
    best_rank = guesses[0][1] if guesses else None
    word, rank = game.hint(best_rank)
    await update.message.reply_html(
        f"💡 Підказка: спробуй асоціації до <b>«{html.escape(word)}»</b> (це слово на позиції {rank})."
    )


async def cmd_giveup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid, day = update.effective_user.id, today()
    res = storage.get_result(uid, day)
    if res and res["solved"]:
        await update.message.reply_text("Ти вже розгадав сьогодні 🎉")
        return
    game = await get_game(day)
    storage.set_result(
        uid, day, solved=False, gave_up=True,
        attempts=storage.attempts_count(uid, day),
    )
    await update.message.reply_html(
        f"Загадане слово було: <b>{html.escape(game.secret)}</b>. Завтра нове! 🎯"
    )


async def on_guess(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    if len(text.split()) > 1:
        await update.message.reply_text("Введи одне слово 🙂")
        return

    uid, day = update.effective_user.id, today()
    storage.remember_user(uid, player_name(update))

    res = storage.get_result(uid, day)
    if res and res["solved"]:
        await update.message.reply_text(
            f"Ти вже розгадав сьогодні за {res['attempts']} спроб 🎉 Чекай нове слово."
        )
        return
    if res and res["gave_up"]:
        await update.message.reply_text("Сьогодні ти здався. Нове слово буде завтра 🙂")
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    game = await get_game(day)
    lem = engine.lemma(text)

    prev = storage.already_guessed_lemma(uid, day, lem)
    if prev is not None:
        await update.message.reply_text(f"«{lem}» ти вже пробував — позиція {prev}.")
        return

    g = game.guess(text)
    storage.record_guess(uid, day, text, g.lemma, g.rank)

    if g.is_win:
        attempts = storage.attempts_count(uid, day)
        storage.set_result(uid, day, solved=True, gave_up=False, attempts=attempts)
        stats = storage.get_stats(uid, day)
        await update.message.reply_html(
            f"{g.emoji} <b>{html.escape(g.lemma)}</b> — ВГАДАВ! 🎉\n"
            f"Спроб: {attempts}. Серія: {stats.streak}🔥"
        )
        return

    note = f" (як «{html.escape(g.lemma)}»)" if g.lemma != text.lower() else ""
    guesses = storage.get_day_guesses(uid, day)
    best = "  ".join(f"{html.escape(w)}={r}" for w, r in guesses[:3])
    await update.message.reply_html(
        f"{g.emoji} <b>{html.escape(text)}</b>{note}:позиція {g.rank}\n"
        f"<i>найкращі:</i> {best}"
    )


async def announce(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in storage.get_subscriber_chats():
        try:
            await ctx.bot.send_message(
                chat_id,
                "🎯 Нове слово дня загадане! Пишіть здогадки.",
            )
        except Exception as e:  # чат міг заблокувати бота
            log.warning("Не вдалося написати %s: %s", chat_id, e)


async def _post_init(app: Application) -> None:
    log.info("Прогріваю модель...")
    await get_game(today())
    log.info("Готово.")


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Немає TELEGRAM_BOT_TOKEN. Створи файл .env і поклади туди токен "
            "від @BotFather (див. .env.example)."
        )
    if not os.getenv("SECRET_SALT"):
        raise SystemExit(
            "Немає SECRET_SALT у .env — без неї слово дня можна вирахувати "
            "з публічного репозиторію. Згенеруй: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if not ALLOWED_USERS:
        log.warning(
            "ALLOWED_USERS порожній — бот нікого не пустить. Напиши боту "
            "/start, він покаже твій ID, і додай його в .env."
        )
    storage.init()

    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("hint", cmd_hint))
    app.add_handler(CommandHandler("giveup", cmd_giveup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_guess))

    app.job_queue.run_daily(
        announce, time=dt.time(hour=ANNOUNCE_HOUR, minute=0, tzinfo=TZ)
    )

    log.info("Бот запущений. Оголошення о %02d:00 (%s).", ANNOUNCE_HOUR, TZ)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
