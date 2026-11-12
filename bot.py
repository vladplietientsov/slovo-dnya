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

# скільки гравців мають завершити слово (вгадати чи здатися), щоб відкрився /newword
FINISHED_FOR_NEW_ROUND = 2

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


def secret_for(day: date, n: int) -> str:
    """Слово раунду: збережене в БД, а нове — з тих, яких ще не було."""
    key = storage.round_key(day, n)
    word = storage.get_secret(key)
    if word is None:
        word = storage.save_secret(
            key, engine.pick_secret(key, storage.secret_history())
        )
    return word


def remember_played_secrets() -> None:
    """Записує слова раундів, зіграних до таблиці secrets (старим способом),
    щоб нові раунди їх не повторювали, а поточний не змінився."""
    for key in storage.played_rounds():
        day, _, n = key.partition("#")
        storage.save_secret(
            key, engine.daily_secret(date.fromisoformat(day), int(n or 0))
        )


async def get_game(day: date, n: int) -> engine.Game:
    key = storage.round_key(day, n)
    async with _games_lock:
        if key not in _games:
            secret = secret_for(day, n)
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


# ---------------------------------------------------------------- вигляд

BAR_LEN = 10
# скільки найкращих слів показуємо після кожної здогадки
BEST_SHOWN = 5
TOP_SHOWN = 10


def bar(rank: int, total: int) -> str:
    """Смужка близькості: позиція 2 — повна, остання у словнику — порожня.

    Шкала логарифмічна: різниця між 5 і 50 важить більше, ніж між 5000 і 5050.
    """
    import math

    closeness = 1 - math.log(max(rank, 1)) / math.log(max(total, 2))
    filled = max(0, min(BAR_LEN, round(closeness * BAR_LEN)))
    return "▰" * filled + "▱" * (BAR_LEN - filled)


def trend(rank: int, prev: int | None) -> str:
    """Порівняння з попередньою здогадкою гравця."""
    if prev is None or rank == prev:
        return ""
    return "  ⬆️ тепліше" if rank < prev else "  ⬇️ холодніше"


def attempts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} спроба"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} спроби"
    return f"{n} спроб"


def best_lines(guesses: list[tuple[str, int, bool]], total: int,
               mark: str | None = None) -> str:
    """Найкращі слова гравця стовпчиком; 💡 — слово з підказки."""
    lines = []
    for lem, rank, is_hint in guesses[:BEST_SHOWN]:
        name = html.escape(lem) + (" 💡" if is_hint else "")
        if lem == mark:
            name = f"<b>{name}</b>"
        lines.append(f"{engine.rank_emoji(rank)} {bar(rank, total)} {rank} · {name}")
    return "\n".join(lines)


def spoiler(update: Update, text: str) -> str:
    """У групі ховаємо відповідь під спойлер: інший гравець ще може грати."""
    if update.effective_chat.type == "private":
        return text
    return f"<tg-spoiler>{text}</tg-spoiler>"


def top_block(game: engine.Game, guessed: set[str]) -> str:
    """Найближчі до загаданого слова; ✅ — ті, що гравець називав сам."""
    lines = [
        f"{i}. {html.escape(w)}" + (" ✅" if w in guessed else "")
        for i, w in enumerate(game.top(TOP_SHOWN), start=2)
    ]
    return "\n".join(lines)


def share_block(day: date, n: int, guesses: list[tuple[str, int, bool]],
                solved: bool) -> str:
    """Підсумок без спойлерів — його можна переслати."""
    own = [r for _, r, h in guesses if not h]
    hints = sum(1 for _, _, h in guesses if h)
    tiers = [
        ("🔥", sum(1 for r in own if 1 < r <= engine.HOT)),
        ("🟢", sum(1 for r in own if engine.HOT < r <= engine.GREEN)),
        ("🟡", sum(1 for r in own if engine.GREEN < r <= engine.YELLOW)),
        ("🔴", sum(1 for r in own if r > engine.YELLOW)),
    ]
    title = f"Слово дня {day:%d.%m}" + (f", раунд {n + 1}" if n else "")
    result = attempts_word(len(own)) if solved else "здався"
    if hints:
        result += f", підказок: {hints}"
    return (
        f"{title} — {result}\n"
        + "  ".join(f"{e}{c}" for e, c in tiers if c)
    ).strip()


def finish_text(update: Update, game: engine.Game, day: date, n: int,
                guesses: list[tuple[str, int, bool]], solved: bool) -> str:
    guessed = {lem for lem, _, h in guesses if not h}
    return (
        "\n\n<b>Найближчі слова</b>\n"
        + spoiler(update, top_block(game, guessed))
        + "\n\n<b>Результат для друзів</b>\n<code>"
        + html.escape(share_block(day, n, guesses, solved))
        + "</code>"
    )


# ---------------------------------------------------------------- команди

WELCOME = (
    "🎯 <b>Слово дня</b> — вгадай загадане слово!\n\n"
    "Щодня одне спільне слово. Пиши будь-яке слово — я скажу, "
    "наскільки воно близьке за змістом.\n"
    "Позиція <b>1</b> — слово вгадано. Чим менше число — тим ближче.\n"
    "🔥 ≤10   🟢 ≤100   🟡 ≤1000   🔴 далі\n\n"
    "Команди:\n"
    "/hint — підказка: слово, ближче за твоє найкраще\n"
    "/words — усі мої слова в цьому раунді\n"
    "/top — найближчі слова (після завершення раунду)\n"
    "/stats — статистика й таблиця\n"
    "/newword — нове слово, коли двоє вже вгадали чи здались\n"
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
        best = f", рекорд {attempts_word(s.best_attempts)}" if s.best_attempts else ""
        lines.append(
            f"{medal} <b>{html.escape(s.name)}</b>: перемог {s.wins} з {s.played}, "
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
    storage.remember_user(uid, player_name(update))
    game = await get_game(day, n)
    guesses = storage.get_round_guesses(uid, rnd)
    best_rank = guesses[0][1] if guesses else None
    hint = game.hint(best_rank, {lem for lem, _, _ in guesses})
    if hint is None:
        await update.message.reply_html(
            f"💡 Ближче вже нікуди: «{html.escape(guesses[0][0])}» стоїть одразу "
            "за відповіддю. Подумай, що з ним найтісніше пов'язане."
        )
        return
    word, rank = hint
    # підказка стає словом гравця: наступна буде ще ближчою
    storage.record_guess(uid, rnd, word, word, rank, hint=True)
    guesses = storage.get_round_guesses(uid, rnd)
    await update.message.reply_html(
        f"💡 Підказка: <b>{html.escape(word)}</b> · позиція {rank}\n\n"
        + best_lines(guesses, game.vocab_size, mark=word)
    )


async def cmd_words(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day, n, rnd = current()
    guesses = storage.get_round_guesses(uid, rnd)
    if not guesses:
        await update.message.reply_text("У цьому раунді ще немає слів. Напиши перше!")
        return
    shown = guesses[:40]
    lines = [
        f"{engine.rank_emoji(r)} {r} · {html.escape(lem)}" + (" 💡" if h else "")
        for lem, r, h in shown
    ]
    more = f"\n…і ще {len(guesses) - len(shown)}" if len(guesses) > len(shown) else ""
    own = sum(1 for _, _, h in guesses if not h)
    await update.message.reply_html(
        f"📝 <b>Мої слова</b> ({attempts_word(own)})\n"
        + spoiler(update, "\n".join(lines)) + more
    )


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day, n, rnd = current()
    res = storage.get_result(uid, rnd)
    if not res or not (res["solved"] or res["gave_up"]):
        await update.message.reply_text(
            "Найближчі слова відкриються, коли вгадаєш слово або здасися 🙂"
        )
        return
    game = await get_game(day, n)
    guesses = storage.get_round_guesses(uid, rnd)
    guessed = {lem for lem, _, h in guesses if not h}
    await update.message.reply_html(
        "<b>Найближчі слова до «"
        + spoiler(update, html.escape(game.secret)) + "»</b>\n"
        + spoiler(update, top_block(game, guessed))
    )


async def cmd_giveup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day, n, rnd = current()
    res = storage.get_result(uid, rnd)
    if res and res["solved"]:
        await update.message.reply_text("Це слово вже вгадано 🐞")
        return
    if res and res["gave_up"]:
        await update.message.reply_text("Ти вже здався в цьому раунді. /top покаже найближчі слова.")
        return
    game = await get_game(day, n)
    storage.remember_user(uid, player_name(update))
    storage.set_result(
        uid, rnd, solved=False, gave_up=True,
        attempts=storage.attempts_count(uid, rnd),
    )
    guesses = storage.get_round_guesses(uid, rnd)
    msg = (
        "Загадане слово було: "
        + spoiler(update, f"<b>{html.escape(game.secret)}</b>")
        + finish_text(update, game, day, n, guesses, solved=False)
        + "\n\nНове слово — завтра або через /newword, коли двоє завершать 🎯"
    )
    await update.message.reply_html(msg)


async def cmd_newword(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with _round_lock:
        day, n, rnd = current()
        finished = storage.finished_count(rnd)
        if finished < FINISHED_FOR_NEW_ROUND:
            await update.message.reply_text(
                f"Нове слово відкриється, коли поточне вгадають або здадуться "
                f"{FINISHED_FOR_NEW_ROUND} гравці (зараз: {finished})."
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
            f"Це слово вже вгадано ({attempts_word(res['attempts'])}) 🐞 "
            "Чекай нове слово або /newword. /top покаже найближчі слова."
        )
        return
    if res and res["gave_up"]:
        await update.message.reply_text(
            "Це слово ти здав. Нове буде завтра або через /newword 🙂"
        )
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    game = await get_game(day, n)
    g = game.guess(text)
    if g is None:
        await update.message.reply_text(
            f"Не знаю слова «{text}» 🤔 Спробу не зараховано. "
            "Перевір написання або спробуй інше слово."
        )
        return

    prev = storage.already_guessed_lemma(uid, rnd, g.lemma)
    if prev is not None and not g.is_win:
        await update.message.reply_text(f"«{g.lemma}» уже було — позиція {prev}.")
        return

    last = storage.last_rank(uid, rnd)
    storage.record_guess(uid, rnd, text, g.lemma, g.rank)
    guesses = storage.get_round_guesses(uid, rnd)

    if g.is_win:
        attempts = storage.attempts_count(uid, rnd)
        hints = storage.hints_count(uid, rnd)
        storage.set_result(uid, rnd, solved=True, gave_up=False, attempts=attempts)
        stats = storage.get_stats(uid, day)
        used = f" · підказок: {hints}" if hints else ""
        msg = (
            f"🐞 {spoiler(update, '<b>' + html.escape(g.lemma) + '</b>')} — вгадано!\n"
            f"{attempts_word(attempts).capitalize()}{used} · серія {stats.streak}🔥"
            + finish_text(update, game, day, n, guesses, solved=True)
        )
        if storage.finished_count(rnd) >= FINISHED_FOR_NEW_ROUND:
            msg += "\n\nДвоє вже завершили це слово — /newword відкриє нове."
        await update.message.reply_html(msg)
        return

    note = f" (як «{html.escape(g.lemma)}»)" if g.lemma != text.lower() else ""
    await update.message.reply_html(
        f"{g.emoji} <b>{html.escape(text)}</b>{note} · позиція {g.rank}"
        f"{trend(g.rank, last)}\n\n"
        + best_lines(guesses, game.vocab_size, mark=g.lemma)
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
    remember_played_secrets()

    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("hint", cmd_hint))
    app.add_handler(CommandHandler("words", cmd_words))
    app.add_handler(CommandHandler("top", cmd_top))
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
