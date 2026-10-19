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

# імпорт бота не має падати без токена
import bot
print("bot.today():", bot.today())

print("\n✅ Усі перевірки пройдено")
