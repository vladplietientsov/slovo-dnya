"""Консольна версія гри «Слово дня».

Запуск:
  python play.py           # слово дня (однакове для всіх сьогодні)
  python play.py --random  # випадкове слово
  python play.py --word кіт # конкретне слово (для тесту)
"""
from __future__ import annotations

import argparse
import random
import sys

import engine


def parse_args():
    ap = argparse.ArgumentParser(description="Слово дня — гра типу Contexto")
    ap.add_argument("--random", action="store_true", help="випадкове слово")
    ap.add_argument("--word", help="задати конкретне загадане слово")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.word:
        secret = args.word
    elif args.random:
        secret = random.choice(engine.load_secret_words())
    else:
        secret = engine.daily_secret()

    print("Завантажую модель...", flush=True)
    game = engine.Game(secret)

    print("\n=== СЛОВО ДНЯ ===")
    print(f"Словник для рейтингу: {game.vocab_size} слів.")
    print("Вгадай загадане слово. Пиши слово й тисни Enter.")
    print("Позиція 1 — вгадав. Чим менше число — тим ближче.")
    print("🟢 ≤100   🟡 ≤1000   🔴 далі")
    print("Команди: /здатися, /вихід\n")

    attempts = 0
    history: list[engine.Guess] = []
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо зустрічі!")
            return
        if not raw:
            continue
        if raw in ("/вихід", "/exit", "/quit"):
            print("До зустрічі!")
            return
        if raw in ("/здатися", "/surrender"):
            print(f"Загадане слово було: {game.secret}")
            return

        attempts += 1
        g = game.guess(raw)
        history.append(g)

        if g.is_win:
            print(f"{g.emoji}  {g.lemma} — ВГАДАВ! Спроб: {attempts}")
            return

        note = ""
        if g.lemma != raw.lower():
            note = f" (як «{g.lemma}»)"
        print(f"{g.emoji}  {raw}{note}: позиція {g.rank}")

        # топ-3 найкращі здогадки для орієнтиру
        best = sorted(history, key=lambda x: x.rank)[:3]
        tops = "  ".join(f"{b.lemma}={b.rank}" for b in best)
        print(f"    найкращі: {tops}")


if __name__ == "__main__":
    sys.exit(main())
