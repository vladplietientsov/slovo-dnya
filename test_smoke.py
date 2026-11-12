"""Швидка перевірка логіки бота без Telegram: сховище, серії, підказка, імпорт."""
import pathlib
import tempfile
from datetime import date, timedelta

import storage

# ізольована тимчасова БД, щоб не чіпати справжню
storage.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
storage.init()

td_date = date.today()
td = storage.round_key(td_date, 0)
yd = storage.round_key(td_date - timedelta(days=1), 0)

# Влад: розгадав учора і сьогодні -> серія 2
storage.remember_user(1, "Влад")
storage.record_guess(1, td, "кіт", "кіт", 1)
storage.set_result(1, td, solved=True, gave_up=False, attempts=4)
storage.set_result(1, yd, solved=True, gave_up=False, attempts=7)

# Віка: розгадала тільки сьогодні -> серія 1
storage.remember_user(2, "Віка")
storage.set_result(2, td, solved=True, gave_up=False, attempts=3)

# раунди: двоє вгадали -> можна відкрити наступний
assert storage.current_round(td_date) == 0
assert storage.finished_count(td) == 2
storage.set_round(td_date, 1)
r1 = storage.round_key(td_date, 1)
assert storage.current_round(td_date) == 1 and r1 == f"{td_date.isoformat()}#1"
assert storage.finished_count(r1) == 0 and storage.get_result(1, r1) is None
# той, хто здався, теж рахується як завершив
storage.set_result(1, r1, solved=False, gave_up=True, attempts=2)
assert storage.finished_count(r1) == 1
# перемога в другому раунді додає перемогу, але не подовжує серію
storage.set_result(2, r1, solved=True, gave_up=False, attempts=9)
s2r = storage.get_stats(2, td_date)
assert s2r.wins == 2 and s2r.streak == 1, s2r
storage._query("DELETE FROM results WHERE day=?", (r1,))

# слова раундів одного дня різні, а раунд 0 — те саме слово дня, що й раніше
import engine
words = [engine.daily_secret(td_date, k) for k in range(5)]
assert len(set(words)) == 5, words
assert words[0] == engine.daily_secret(td_date)

s1 = storage.get_stats(1, td_date)
s2 = storage.get_stats(2, td_date)
print("Влад:", s1)
print("Віка:", s2)
assert s1.wins == 2 and s1.streak == 2 and s1.best_attempts == 4, s1
assert s2.wins == 1 and s2.streak == 1 and s2.best_attempts == 3, s2

board = storage.scoreboard(td_date)
print("Таблиця:", [(b.name, b.wins) for b in board])
assert board[0].name == "Влад"  # більше перемог

# дублікат-леми
assert storage.already_guessed_lemma(1, td, "кіт") == 1
assert storage.already_guessed_lemma(1, td, "пес") is None

# підказка з рушія
g = engine.Game("кіт")
print("hint(None):", g.hint(None))
print("hint(200):", g.hint(200))
w, r = g.hint(200)
assert 2 <= r <= 200

# підказка — не спроба: у лік не йде, але видно серед слів гравця
storage.remember_user(3, "Тест")
storage.record_guess(3, td, "стіл", "стіл", 900)
storage.record_guess(3, td, "зір", "зір", 3, hint=True)
storage.record_guess(3, td, "вухо", "вухо", 40)
assert storage.attempts_count(3, td) == 2 and storage.hints_count(3, td) == 1
assert storage.last_rank(3, td) == 40
assert storage.get_round_guesses(3, td) == [
    ("зір", 3, True), ("вухо", 40, False), ("стіл", 900, False)]
storage._query("DELETE FROM guesses WHERE user_id=3")
storage._query("DELETE FROM users WHERE user_id=3")

# стара база без колонки hint оновлюється сама
old_db = storage.DB_PATH
storage.DB_PATH = old_db.with_name("old.db")
storage._query("CREATE TABLE guesses (user_id INTEGER, day TEXT, word TEXT, "
               "lemma TEXT, rank INTEGER, ts TEXT)")
storage._query("INSERT INTO guesses VALUES (1, 'd', 'кіт', 'кіт', 5, '')")
storage.init()
assert storage.get_round_guesses(1, "d") == [("кіт", 5, False)]
storage.DB_PATH = old_db

# імпорт бота не має падати без токена
import bot
print("bot.today():", bot.today())

# вигляд повідомлень
assert bot.bar(2, 12000) == "▰" * 9 + "▱" and bot.bar(12000, 12000) == "▱" * 10
assert bot.trend(40, 900) == "  ⬆️ тепліше" and bot.trend(900, 40) == "  ⬇️ холодніше"
assert bot.trend(5, None) == ""
assert [bot.attempts_word(k) for k in (1, 2, 5, 11, 21, 22)] == [
    "1 спроба", "2 спроби", "5 спроб", "11 спроб", "21 спроба", "22 спроби"]
share = bot.share_block(date(2026, 11, 12), 0,
                        [("око", 1, False), ("зір", 3, True), ("вухо", 5, False),
                         ("ніс", 40, False), ("стіл", 900, False)], solved=True)
print(share)
assert share == "Слово дня 12.11 — 4 спроби, підказок: 1\n🔥1  🟢1  🟡1"
assert "око" not in share.split("—")[1]

# уже зіграні раунди зберігають своє слово, а нові його не повторюють
assert storage.played_rounds() == [yd, td, r1], storage.played_rounds()
bot.remember_played_secrets()
assert storage.played_rounds() == []
assert storage.get_secret(td) == engine.daily_secret(td_date)
assert storage.get_secret(r1) == engine.daily_secret(td_date, 1)
assert bot.secret_for(td_date, 0) == engine.daily_secret(td_date)

# цілий список слів без жодного повтору, далі — лише найдавніші
all_words = engine.load_secret_words()
start = td_date + timedelta(days=1)
for k in range(len(all_words) - 3):
    bot.secret_for(start + timedelta(days=k), 0)
old, new = storage.secret_history()[:3], storage.secret_history()[3:]
assert len(new) == len(set(new)) and not set(old) & set(new)
for k in range(len(all_words), len(all_words) + 20):
    w = bot.secret_for(start + timedelta(days=k), 0)
    assert w not in storage.secret_history()[-len(all_words) // 2 - 1:-1], w
print("слова не повторюються: OK")

print("\n✅ Усі перевірки пройдено")
