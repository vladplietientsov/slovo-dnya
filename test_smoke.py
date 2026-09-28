"""Швидка перевірка логіки бота без Telegram: сховище, серії, підказка, імпорт."""
import pathlib
import tempfile
from datetime import date, timedelta

import storage

# ізольована тимчасова БД, щоб не чіпати справжню
storage.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
storage.init()

td = date.today()
yd = td - timedelta(days=1)

# Влад: розгадав учора і сьогодні -> серія 2
storage.remember_user(1, "Влад")
storage.record_guess(1, td, "кіт", "кіт", 1)
storage.set_result(1, td, solved=True, gave_up=False, attempts=4)
storage.set_result(1, yd, solved=True, gave_up=False, attempts=7)

# Віка: розгадала тільки сьогодні -> серія 1
storage.remember_user(2, "Віка")
storage.set_result(2, td, solved=True, gave_up=False, attempts=3)

s1 = storage.get_stats(1, td)
s2 = storage.get_stats(2, td)
print("Влад:", s1)
print("Віка:", s2)
assert s1.wins == 2 and s1.streak == 2 and s1.best_attempts == 4, s1
assert s2.wins == 1 and s2.streak == 1 and s2.best_attempts == 3, s2

board = storage.scoreboard(td)
print("Таблиця:", [(b.name, b.wins) for b in board])
assert board[0].name == "Влад"  # більше перемог

# дублікат-леми
assert storage.already_guessed_lemma(1, td, "кіт") == 1
assert storage.already_guessed_lemma(1, td, "пес") is None

# підказка з рушія
import engine
g = engine.Game("кіт")
print("hint(None):", g.hint(None))
print("hint(200):", g.hint(200))
w, r = g.hint(200)
assert 2 <= r <= 200

# імпорт бота не має падати без токена
import bot
print("bot.today():", bot.today())

print("\n✅ Усі перевірки пройдено")
