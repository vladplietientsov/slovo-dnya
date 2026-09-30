"""Рушій гри «Слово дня» (тип Contexto українською).

Використовує spaCy-модель uk_core_news_md:
  * лематизація здогадок ("котів" -> "кіт");
  * floret-вектори (будь-яке слово має вектор, немає "невідомих").

Рейтинг: для загаданого слова рахуємо косинусну схожість усіх слів
словника, сортуємо. Позиція здогадки = скільки слів ближчі за неї.
Загадане слово = 1, найближче інше = 2, і далі.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
import spacy
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
DATA = ROOT / "data"
MODEL = "uk_core_news_md"

# пороги для кольорів за позицією
GREEN = 100
YELLOW = 1000


@lru_cache(maxsize=1)
def _nlp():
    return spacy.load(MODEL)


def _norm(word: str) -> str:
    return word.strip().lower().replace("’", "'")


def lemma(word: str) -> str:
    """Початкова форма слова, у нижньому регістрі."""
    return lemmas([word])[0][0]


@lru_cache(maxsize=1)
def _morph():
    import pymorphy3

    return pymorphy3.MorphAnalyzer(lang="uk")


def lemmas(words: list[str]) -> list[tuple[str, object]]:
    """(лема, тег pymorphy3 або None) для кожного слова — пакетом, для словника.

    Та сама логіка, що й у lemma(), тож лема здогадки й лема у словнику
    завжди збігаються.

    spaCy на окремому слові без контексту часто помиляється з частиною мови
    й лишає слово як є ("коктейлю", "болоті"), тому основа — pymorphy3,
    а лема від spaCy лише обирає між кількома розборами ("котів" -> "кіт",
    а не "коти").
    """
    morph = _morph()
    normed = [_norm(w) for w in words]
    out = []
    for w, doc in zip(normed, _nlp().pipe(normed, batch_size=1000)):
        spacy_lem = _norm(doc[0].lemma_) if doc else w
        parses = [p for p in morph.parse(w) if morph.word_is_known(p.normal_form)]
        if not parses:
            out.append((spacy_lem, None))
            continue
        by_form = {p.normal_form: p for p in reversed(parses)}
        if spacy_lem != w and spacy_lem in by_form:
            p = by_form[spacy_lem]   # spaCy щось змінив, і pymorphy3 згоден
        elif w in by_form:
            p = by_form[w]           # слово вже в початковій формі
        else:
            p = parses[0]
        out.append((p.normal_form, p.tag))
    return out


def _vector(word: str) -> np.ndarray:
    v = _nlp().vocab.get_vector(word)
    return np.asarray(v, dtype=np.float32)


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def load_secret_words() -> list[str]:
    return [
        w.strip()
        for w in (DATA / "secret_words.txt").read_text(encoding="utf-8").splitlines()
        if w.strip()
    ]


def daily_secret(day: date | None = None) -> str:
    """Детерміноване слово дня — обидва гравці отримують однакове.

    Залежить від SECRET_SALT із .env: без неї слово можна вирахувати
    з публічного репозиторію.
    """
    day = day or date.today()
    words = load_secret_words()
    salt = os.getenv("SECRET_SALT", "").encode()
    h = hmac.new(salt, day.isoformat().encode(), hashlib.sha256).hexdigest()
    return words[int(h, 16) % len(words)]


@dataclass
class Guess:
    word: str          # як ввів гравець
    lemma: str         # початкова форма
    rank: int          # позиція (1 = вгадано)
    similarity: float  # косинусна схожість [-1, 1]

    @property
    def emoji(self) -> str:
        if self.rank == 1:
            return "🎉"
        if self.rank <= GREEN:
            return "🟢"
        if self.rank <= YELLOW:
            return "🟡"
        return "🔴"

    @property
    def is_win(self) -> bool:
        return self.rank == 1


class Game:
    def __init__(self, secret: str, vocab_path: Path | None = None):
        self.secret = lemma(secret)
        self._secret_vec = _unit(_vector(self.secret))

        vocab_path = vocab_path or (DATA / "vocab.txt")
        # dict.fromkeys — прибирає дублікати, зберігаючи порядок
        vocab = list(dict.fromkeys(
            w.strip()
            for w in vocab_path.read_text(encoding="utf-8").splitlines()
            if w.strip() and w.strip() != self.secret
        ))
        self.vocab = vocab
        self._index = {w: i for i, w in enumerate(vocab)}

        matrix = np.vstack([_unit(_vector(w)) for w in vocab])
        # схожість кожного слова словника із загаданим
        self._vocab_sims = matrix @ self._secret_vec

    def guess(self, word: str) -> Guess:
        lem = lemma(word)
        # друга умова — для слів лише у множині: "сани" pymorphy3 зводить до "сан"
        if lem == self.secret or _norm(word) == self.secret:
            return Guess(word=word, lemma=self.secret, rank=1, similarity=1.0)
        idx = self._index.get(lem)
        if idx is not None:
            # беремо вже пораховане число: окремий розрахунок відрізняється
            # в останніх знаках, і слово могло "обігнати саме себе"
            sim = float(self._vocab_sims[idx])
        else:
            sim = float(_unit(_vector(lem)) @ self._secret_vec)
        # +2: 1 — саме загадане слово, 1 — переводимо кількість ближчих у позицію
        rank = int(np.sum(self._vocab_sims > sim)) + 2
        return Guess(word=word, lemma=lem, rank=rank, similarity=sim)

    @property
    def vocab_size(self) -> int:
        return len(self.vocab) + 1

    def hint(self, best_rank: int | None) -> tuple[str, int]:
        """Слово, ближче за найкращу здогадку гравця (але не відповідь)."""
        if best_rank is None or best_rank <= 3:
            target = 50
        else:
            target = max(2, best_rank // 2)
        order = np.argsort(-self._vocab_sims)
        idx = max(0, min(len(self.vocab) - 1, target - 2))
        return self.vocab[order[idx]], idx + 2
