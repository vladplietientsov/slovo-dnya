"""Рушій гри «Слово дня» (тип Contexto українською).

Лематизація здогадок ("котів" -> "кіт") — spaCy uk_core_news_md і pymorphy3.
Вектори — fastText cc.uk.300, лише для початкових форм (data/vectors.npy,
див. build_vectors.py). Слово без вектора гра не знає.

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
    return word.strip().lower().replace("’", "'").replace("ʼ", "'")


def lemma(word: str) -> str:
    """Початкова форма слова, у нижньому регістрі."""
    return lemmas([word])[0][0]


_NAME_TAGS = {"Name", "Surn", "Patr"}


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
        # імена й прізвища pymorphy3 часто ставить першими ("кіт", "король",
        # "люди" -> "Люда"), і spaCy з ним погоджується; звичайне слово важливіше
        common = [p for p in parses if not _NAME_TAGS & p.tag.grammemes]
        parses = common or parses
        by_form = {p.normal_form: p for p in reversed(parses)}
        own = next((p for p in parses if p.normal_form == w), None)
        alt = by_form.get(spacy_lem) if spacy_lem != w else None
        if own is not None and "Pltm" in own.tag and not (alt and "anim" in alt.tag):
            # лише множина: "сани", а не "сан"; але множина істот —
            # звичайна: "коти" -> "кіт", а не рідкісне взуття "коти"
            p = own
        elif spacy_lem != w and spacy_lem in by_form:
            p = by_form[spacy_lem]   # spaCy щось змінив, і pymorphy3 згоден
        elif w in by_form:
            p = by_form[w]           # слово вже в початковій формі
        else:
            p = parses[0]
        out.append((p.normal_form, p.tag))
    return out


@lru_cache(maxsize=1)
def _vectors() -> tuple[dict[str, int], np.ndarray]:
    """{слово: рядок} і матриця одиничних векторів (float16)."""
    words = (DATA / "words.txt").read_text(encoding="utf-8").splitlines()
    matrix = np.load(DATA / "vectors.npy")
    return {w: i for i, w in enumerate(words)}, matrix


def is_known(word: str) -> bool:
    return word in _vectors()[0]


def _vector(word: str) -> np.ndarray:
    index, matrix = _vectors()
    return matrix[index[word]].astype(np.float32)


def _g_variants(word: str) -> list[str]:
    """"гудзик" -> ["ґудзик"]: гравці часто пишуть ґ як г."""
    return [word[:i] + "ґ" + word[i + 1:] for i, c in enumerate(word) if c == "г"]


def _rhymes(a: str, b: str) -> bool:
    """Спільне закінчення з 3 літер: "брова" / "корова"."""
    return len(a) >= 3 and len(b) >= 3 and a[-3:] == b[-3:]


def load_secret_words() -> list[str]:
    return [
        w.strip()
        for w in (DATA / "secret_words.txt").read_text(encoding="utf-8").splitlines()
        if w.strip()
    ]


def daily_secret(day: date | None = None, round_no: int = 0) -> str:
    """Детерміноване слово дня — обидва гравці отримують однакове.

    round_no — номер раунду за день (0 — перше слово, далі /newword).
    Слова раундів одного дня не повторюються.

    Залежить від SECRET_SALT із .env: без неї слово можна вирахувати
    з публічного репозиторію.
    """
    day = day or date.today()
    words = load_secret_words()
    salt = os.getenv("SECRET_SALT", "").encode()
    used: set[str] = set()
    for n in range(round_no + 1):
        key = day.isoformat() if n == 0 else f"{day.isoformat()}#{n}"
        h = hmac.new(salt, key.encode(), hashlib.sha256).hexdigest()
        i = int(h, 16) % len(words)
        while words[i] in used:
            i = (i + 1) % len(words)
        used.add(words[i])
    return words[i]


@dataclass
class Guess:
    word: str          # як ввів гравець
    lemma: str         # початкова форма
    rank: int          # позиція (1 = вгадано)
    similarity: float  # косинусна схожість [-1, 1]

    @property
    def emoji(self) -> str:
        if self.rank == 1:
            return "🐞"
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
        self._secret_vec = _vector(self.secret)

        vocab_path = vocab_path or (DATA / "vocab.txt")
        # dict.fromkeys — прибирає дублікати, зберігаючи порядок
        vocab = list(dict.fromkeys(
            w.strip()
            for w in vocab_path.read_text(encoding="utf-8").splitlines()
            if w.strip() and w.strip() != self.secret and is_known(w.strip())
        ))
        self.vocab = vocab
        self._index = {w: i for i, w in enumerate(vocab)}

        index, vectors = _vectors()
        matrix = vectors[[index[w] for w in vocab]].astype(np.float32)
        # схожість кожного слова словника із загаданим
        self._vocab_sims = matrix @ self._secret_vec

    def resolve(self, word: str) -> str | None:
        """Лема здогадки, яку знає гра, або None ("теремоек")."""
        w = _norm(word)
        # сама форма — для слів лише у множині: "сани" pymorphy3 зводить до "сан"
        candidates = [lemma(word), w]
        for v in _g_variants(w):
            candidates += [lemma(v), v]
        for c in candidates:
            if c == self.secret or is_known(c):
                return c
        return None

    def guess(self, word: str) -> Guess | None:
        """None — слова немає у словнику гри."""
        lem = self.resolve(word)
        if lem is None:
            return None
        if lem == self.secret:
            return Guess(word=word, lemma=self.secret, rank=1, similarity=1.0)
        idx = self._index.get(lem)
        if idx is not None:
            # беремо вже пораховане число: окремий розрахунок відрізняється
            # в останніх знаках, і слово могло "обігнати саме себе"
            sim = float(self._vocab_sims[idx])
        else:
            sim = float(_vector(lem) @ self._secret_vec)
        # +2: 1 — саме загадане слово, 1 — переводимо кількість ближчих у позицію
        rank = int(np.sum(self._vocab_sims > sim)) + 2
        return Guess(word=word, lemma=lem, rank=rank, similarity=sim)

    @property
    def vocab_size(self) -> int:
        return len(self.vocab) + 1

    def hint(self, best_rank: int | None) -> tuple[str, int]:
        """Слово, ближче за найкращу здогадку гравця (але не відповідь).

        Пропускає слова, що римуються із загаданим: вони не допомагають.
        """
        if best_rank is None or best_rank <= 3:
            target = 50
        else:
            target = max(2, best_rank // 2)
        order = np.argsort(-self._vocab_sims)
        start = max(0, min(len(self.vocab) - 1, target - 2))
        # спершу шукаємо ближче до відповіді, потім далі
        for idx in [*range(start, -1, -1), *range(start + 1, len(order))]:
            w = self.vocab[order[idx]]
            if not _rhymes(w, self.secret):
                return w, idx + 2
        return self.vocab[order[start]], start + 2
