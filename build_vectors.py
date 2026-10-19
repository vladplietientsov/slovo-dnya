"""Готує вектори слів із fastText cc.uk.300 (Common Crawl + Вікіпедія).

floret-вектори spaCy будуються з символьних n-грамів, тож для рідкісних слів
схожість визначає написання, а не зміст: у «брова» найближчі були «сова»,
«вдова», «корова». А ще вектор мало будь-яке набране слово ("теремоек").
Тут беремо готові вектори цілих слів, і лише для початкових форм.

Створює:
  data/words.txt   - відомі гравцеві слова (леми), у частотному порядку
  data/vectors.npy - їхні одиничні вектори, float16, рядок на слово

Обидва файли в репозиторії: модель важить ~1.2 ГБ, качати її на хостингу
не треба. Запуск (лише щоб перебудувати):
  python build_vectors.py шлях/до/cc.uk.300.vec.gz
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

import numpy as np

from build_data import UK_RE, is_content_lemma

ROOT = Path(__file__).parent
DATA = ROOT / "data"
VEC_URL = "https://dl.fbaipublicfiles.com/fasttext/vectors-crawl/cc.uk.300.vec.gz"

# скільки рядків моделі (за частотою) переглядаємо
SCAN = 1_000_000
# скільки лем лишаємо: це всі слова, які гра "знає"
KEEP = 80_000


def read_vec(path: Path) -> tuple[list[str], np.ndarray]:
    words: list[str] = []
    rows: list[np.ndarray] = []
    seen: set[str] = set()
    with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
        next(f)  # заголовок: кількість слів і розмірність
        for n, line in enumerate(f):
            if n >= SCAN:
                break
            w, _, rest = line.rstrip().partition(" ")
            # лише слова, записані малими: "Київ" чи "НАТО" — не наша гра
            if w != w.lower() or not UK_RE.match(w) or len(w) < 2:
                continue
            w = w.replace("’", "'")
            if w in seen:
                continue
            seen.add(w)
            words.append(w)
            rows.append(np.array(rest.split(), dtype=np.float32))
    return words, np.vstack(rows)


def restore_apostrophe(word: str, known) -> str:
    """fastText викидає апостроф: "память" -> "пам'ять".

    Варіант з апострофом — першим: "память" pymorphy3 теж знає, як русизм.
    """
    for i in range(1, len(word)):
        if word[i] in "яюєї" and word[i - 1] in "бпвмфрк":
            w = word[:i] + "'" + word[i:]
            if known(w):
                return w
    return word


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"Вкажи шлях до cc.uk.300.vec.gz (завантажити: {VEC_URL})")
    import engine

    words, matrix = read_vec(Path(sys.argv[1]))
    print(f"Прочитано слів: {len(words)}")
    known = engine._morph().word_is_known
    words = [restore_apostrophe(w, known) for w in words]

    keep: list[int] = []
    kept_set: set[str] = set()  # "память" і "пам'ять" стають одним словом
    for i, (w, (lem, tag)) in enumerate(zip(words, engine.lemmas(words))):
        # лишаємо слово, лише якщо воно — сама початкова форма: вектор
        # "котів" не має заступати вектор "кіт"
        if tag is None or lem != w or w in kept_set:
            continue
        if not is_content_lemma(w):
            continue
        kept_set.add(w)
        keep.append(i)
        if len(keep) >= KEEP:
            break

    kept = [words[i] for i in keep]
    m = matrix[keep]
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    DATA.mkdir(exist_ok=True)
    (DATA / "words.txt").write_text("\n".join(kept), encoding="utf-8")
    np.save(DATA / "vectors.npy", m.astype(np.float16))
    print(f"Лем із векторами: {len(kept)} -> data/words.txt, data/vectors.npy")


if __name__ == "__main__":
    main()
