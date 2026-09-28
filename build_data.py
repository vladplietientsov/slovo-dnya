"""Готує словник для рейтингу зі частотного списку uk_50k.txt.

Створює:
  data/vocab.txt - словник для рейтингу (top-N частотних слів)

Список слів для загадування (data/secret_words.txt) вивірений вручну:
частотний список зібраний із субтитрів і містить багато російських та
сміттєвих слів, тож для щоденного слова він не годиться.

Запуск (один раз): python build_data.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
FREQ_FILE = ROOT / "uk_50k.txt"
FREQ_URL = (
    "https://raw.githubusercontent.com/hermitdave/FrequencyWords/"
    "master/content/2018/uk/uk_50k.txt"
)

# скільки слів беремо у словник для рейтингу
VOCAB_SIZE = 15000

# українські літери + апостроф
UK_RE = re.compile(r"^[абвгґдеєжзиіїйклмнопрстуфхцчшщьюя'’]+$")


def ensure_freq_file() -> None:
    if FREQ_FILE.exists():
        return
    import urllib.request

    print(f"Завантажую частотний список: {FREQ_URL}")
    urllib.request.urlretrieve(FREQ_URL, FREQ_FILE)
    print(f"Збережено -> {FREQ_FILE.name}")


def clean_words() -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for line in FREQ_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        w = parts[0].strip().lower().replace("’", "'")
        if len(w) < 3 or len(w) > 18:
            continue
        if not UK_RE.match(w):
            continue
        if w in seen:
            continue
        seen.add(w)
        words.append(w)
    return words


def main() -> None:
    DATA.mkdir(exist_ok=True)
    ensure_freq_file()
    words = clean_words()
    print(f"Очищено слів: {len(words)}")

    vocab = words[:VOCAB_SIZE]
    (DATA / "vocab.txt").write_text("\n".join(vocab), encoding="utf-8")
    print(f"Словник для рейтингу: {len(vocab)} -> data/vocab.txt")


if __name__ == "__main__":
    main()
