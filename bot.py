"""Telegram-бот гри «Слово дня».

Налаштування через .env:
  TELEGRAM_BOT_TOKEN=...       # токен від @BotFather (обовʼязково)
  SECRET_SALT=...              # випадковий рядок, від нього залежить слово дня (обовʼязково)
  ALLOWED_USERS=111,222        # Telegram ID гравців через кому
  TIMEZONE=Europe/Kyiv        # часовий пояс
  WEBHOOK_URL=https://...      # адреса сервера; без неї бот сам опитує Telegram
                               # (на Render підставляється з RENDER_EXTERNAL_URL)

Запуск: python bot.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
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
# httpx на рівні INFO пише URL запитів, а в ньому токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("slovo")

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
}
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))
# Render сам задає RENDER_EXTERNAL_URL і PORT
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", "8080"))

# скільки гравців мають вгадати слово, щоб відкрився /newword
SOLVERS_FOR_NEW_ROUND = 2

# кеш готових ігор за раундами (побудова важка ~3с)
_games: dict[str, engine.Game] = {}
_games_lock = asyncio.Lock()
# щоб два одночасні /newword не перескочили через раунд
_round_lock = asyncio.Lock()


def today() -> date:
    return dt.datetime.now(TZ).date()


def current() -> tuple[date, int, str]:
    """(день, номер раунду, ключ раунду) для поточного слова."""
    day = today()
    n = storage.current_round(day)
    return day, n, storage.round_key(day, n)


async def get_game(day: date, n: int) -> engine.Game:
    key = storage.round_key(day, n)
    async with _games_lock:
        if key not in _games:
            secret = engine.daily_secret(day, n)
            log.info("Будую гру на %s", key)
            game = await asyncio.to_thread(engine.Game, secret)
            # лишаємо попередній раунд: хтось ще міг дописувати в нього
            for old in list(_games)[:-1]:
                del _games[old]
            _games[key] = game
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
    "Позиція <b>1</b> — слово вгадано. Чим менше число — тим ближче.\n"
    "🟢 ≤100   🟡 ≤1000   🔴 далі\n\n"
    "Команди:\n"
    "/hint — підказка\n"
    "/stats — статистика й таблиця\n"
    "/newword — нове слово, коли двоє вже вгадали\n"
    "/giveup — здатися й дізнатися слово\n"
    "/help — довідка"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.remember_user(update.effective_user.id, player_name(update))
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
            f"{medal} <b>{html.escape(s.name)}</b>: {s.wins} перемог, "
            f"серія {s.streak}🔥{best}"
        )
    await update.message.reply_html("\n".join(lines))


async def cmd_hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day, n, rnd = current()
    res = storage.get_result(uid, rnd)
    if res and (res["solved"] or res["gave_up"]):
        await update.message.reply_text("Ця гра вже завершена 🙂 Чекай нове слово.")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    game = await get_game(day, n)
    guesses = storage.get_day_guesses(uid, rnd)
    best_rank = guesses[0][1] if guesses else None
    word, rank = game.hint(best_rank)
    await update.message.reply_html(
        f"💡 Підказка: спробуй асоціації до <b>«{html.escape(word)}»</b> (це слово на позиції {rank})."
    )


async def cmd_giveup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day, n, rnd = current()
    res = storage.get_result(uid, rnd)
    if res and res["solved"]:
        await update.message.reply_text("Це слово вже вгадано 🐞")
        return
    game = await get_game(day, n)
    storage.set_result(
        uid, rnd, solved=False, gave_up=True,
        attempts=storage.attempts_count(uid, rnd),
    )
    await update.message.reply_html(
        f"Загадане слово було: <b>{html.escape(game.secret)}</b>. "
        "Нове слово — завтра або через /newword, коли двоє вгадають 🎯"
    )


async def cmd_newword(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with _round_lock:
        day, n, rnd = current()
        solvers = storage.solvers_count(rnd)
        if solvers < SOLVERS_FOR_NEW_ROUND:
            await update.message.reply_text(
                f"Нове слово відкриється, коли поточне вгадають "
                f"{SOLVERS_FOR_NEW_ROUND} гравці (зараз: {solvers})."
            )
            return
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        old = (await get_game(day, n)).secret
        await get_game(day, n + 1)  # будуємо до перемикання, щоб гравці не чекали
        storage.set_round(day, n + 1)

    text = (
        f"🆕 <b>Нове слово!</b> Раунд {n + 2} на сьогодні.\n"
        f"Попереднє було: <b>{html.escape(old)}</b>. Пиши здогадки 🎯"
    )
    await update.message.reply_html(text)
    # у приватних чатах інші гравці самі не побачать — повідомляємо їх
    if update.effective_chat.type == "private":
        caller = update.effective_user.id
        who = html.escape(player_name(update))
        for uid in storage.all_player_ids():
            if uid == caller or uid not in ALLOWED_USERS:
                continue
            try:
                await ctx.bot.send_message(
                    uid, f"{who}: нове слово відкрито.\n{text}", parse_mode="HTML"
                )
            except Exception as e:  # гравець міг заблокувати бота
                log.warning("Не вдалося повідомити %s: %s", uid, e)


async def on_guess(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    if len(text.split()) > 1:
        await update.message.reply_text("Введи одне слово 🙂")
        return

    uid = update.effective_user.id
    day, n, rnd = current()
    storage.remember_user(uid, player_name(update))

    res = storage.get_result(uid, rnd)
    if res and res["solved"]:
        await update.message.reply_text(
            f"Це слово вже вгадано за {res['attempts']} спроб 🐞 "
            "Чекай нове слово або /newword."
        )
        return
    if res and res["gave_up"]:
        await update.message.reply_text(
            "Це слово ти здав. Нове буде завтра або через /newword 🙂"
        )
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    game = await get_game(day, n)
    lem = engine.lemma(text)

    prev = storage.already_guessed_lemma(uid, rnd, lem)
    if prev is not None:
        await update.message.reply_text(f"«{lem}» уже було — позиція {prev}.")
        return

    g = game.guess(text)
    storage.record_guess(uid, rnd, text, g.lemma, g.rank)

    if g.is_win:
        attempts = storage.attempts_count(uid, rnd)
        storage.set_result(uid, rnd, solved=True, gave_up=False, attempts=attempts)
        stats = storage.get_stats(uid, day)
        msg = (
            f"{g.emoji} <b>{html.escape(g.lemma)}</b> — Вгадано! 🐞\n"
            f"Спроб: {attempts}. Серія: {stats.streak}🔥"
        )
        if storage.solvers_count(rnd) >= SOLVERS_FOR_NEW_ROUND:
            msg += "\nСлово вже вгадали двоє — /newword відкриє нове."
        await update.message.reply_html(msg)
        return

    note = f" (як «{html.escape(g.lemma)}»)" if g.lemma != text.lower() else ""
    guesses = storage.get_day_guesses(uid, rnd)
    best = "  ".join(f"{html.escape(w)}={r}" for w, r in guesses[:3])
    await update.message.reply_html(
        f"{g.emoji} <b>{html.escape(text)}</b>{note}: позиція {g.rank}\n"
        f"<i>найкращі:</i> {best}"
    )


async def _post_init(app: Application) -> None:
    log.info("Прогріваю модель...")
    day, n, _ = current()
    await get_game(day, n)
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
    app.add_handler(CommandHandler("newword", cmd_newword))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_guess))

    if WEBHOOK_URL:
        # хостинг, що засинає: Telegram сам будить сервер запитом на вебхук.
        # secret_token — щоб сторонні не могли слати боту фальшиві оновлення
        secret = hashlib.sha256(f"webhook:{TOKEN}".encode()).hexdigest()
        log.info("Бот запущений на вебхуку (%s).", TZ)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Бот запущений (%s).", TZ)
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
