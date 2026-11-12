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
HOT = 10
GREEN = 100
YELLOW = 1000

# Рейтинг = суміш двох схожостей (див. Game._scores):
#   W_BASE  - вектор слова проти вектора загаданого;
#   W_ASSOC - вектор слова проти асоціацій із data/associations.txt.
W_BASE = 0.55
W_ASSOC = 0.45
# скільки найближчих асоціацій усереднюємо для одного слова
ASSOC_TOP = 3
# надбавка самим асоціаціям: перша у списку — найбільша, остання — найменша
ASSOC_BONUS = (0.16, 0.06)
# штраф слову, що лише римується із загаданим ("ковадло" / "повидло")
RHYME_PENALTY = 0.05
# з якої позиції починається перша підказка, коли здогадок ще немає
FIRST_HINT = 150


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


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    """{похідне: основне} із data/aliases.txt: "чемоданчик" -> "чемодан"."""
    pairs = {}
    for line in (DATA / "aliases.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            a, b = line.split()
            pairs[a] = b
    # "пиріжки" -> "пиріжок" -> "пиріг"
    for a, b in pairs.items():
        while b in pairs:
            b = pairs[b]
        pairs[a] = b
    return pairs


def base_word(word: str) -> str:
    """Основне слово для зменшувальної чи хибної форми, інакше саме слово."""
    return _aliases().get(word, word)


def is_known(word: str) -> bool:
    return word in _vectors()[0]


def _vector(word: str) -> np.ndarray:
    index, matrix = _vectors()
    return matrix[index[word]].astype(np.float32)


def _g_variants(word: str) -> list[str]:
    """"гудзик" -> ["ґудзик"]: гравці часто пишуть ґ як г."""
    return [word[:i] + "ґ" + word[i + 1:] for i, c in enumerate(word) if c == "г"]


@lru_cache(maxsize=1)
def _associations() -> dict[str, list[str]]:
    """{загадане: [асоціації, найближча перша]} із data/associations.txt."""
    out: dict[str, list[str]] = {}
    path = DATA / "associations.txt"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, rest = line.partition(":")
        out[_norm(key)] = [_norm(w) for w in rest.split()]
    return out


def anchors_for(secret: str) -> list[str]:
    """Асоціації до слова у формах, які знає гра, без повторів."""
    raw = _associations().get(secret, [])
    if not raw:
        return []
    out: list[str] = []
    for w, (lem, _) in zip(raw, lemmas(raw)):
        # слово як є важливіше за лему: "ковзани" pymorphy3 зводить до "ковзан"
        w = next((base_word(c) for c in (w, lem) if is_known(base_word(c))), None)
        if w and w != secret and w not in out:
            out.append(w)
    return out


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


def pick_secret(key: str, history: list[str]) -> str:
    """Слово для раунду key, якого ще не було в history (старі — першими).

    daily_secret() дивився лише на свій день, і за кілька тижнів слова
    почали повторюватись. Тепер обираємо з невживаних, а коли всі вже були,
    — з тих, що грались найдавніше (поза останньою половиною списку).
    """
    words = load_secret_words()
    used = set(history)
    if all(w in used for w in words):
        used = set(history[-(len(words) // 2):])
    left = [w for w in words if w not in used]
    salt = os.getenv("SECRET_SALT", "").encode()
    h = hmac.new(salt, key.encode(), hashlib.sha256).hexdigest()
    return left[int(h, 16) % len(left)]


@dataclass
class Guess:
    word: str          # як ввів гравець
    lemma: str         # початкова форма
    rank: int          # позиція (1 = вгадано)
    similarity: float  # косинусна схожість [-1, 1]

    @property
    def emoji(self) -> str:
        return rank_emoji(self.rank)

    @property
    def is_win(self) -> bool:
        return self.rank == 1


def rank_emoji(rank: int) -> str:
    if rank == 1:
        return "🐞"
    if rank <= HOT:
        return "🔥"
    if rank <= GREEN:
        return "🟢"
    if rank <= YELLOW:
        return "🟡"
    return "🔴"


class Game:
    """Рейтинг усіх слів словника відносно одного загаданого слова.

    Оцінка слова — суміш двох схожостей:
      * з вектором самого загаданого слова (fastText): знаходить слова того
        самого типу — "лікар" -> "хірург", "терапевт";
      * з асоціаціями з data/associations.txt: знаходить пов'язані за змістом
        — "брова" -> "око", "вія", "лоб", яких вектори поруч не ставлять.
    Самі асоціації отримують надбавку, тож стоять у верхній частині рейтингу.
    Слова без списку асоціацій ранжуються лише за вектором, як раніше.
    """

    def __init__(self, secret: str, vocab_path: Path | None = None):
        self.secret = lemma(secret)
        if not is_known(self.secret) and is_known(_norm(secret)):
            self.secret = _norm(secret)
        self._secret_vec = _vector(self.secret)

        self.anchors = anchors_for(self.secret)
        n = len(self.anchors)
        lo, hi = ASSOC_BONUS[1], ASSOC_BONUS[0]
        self._bonus = {
            w: hi - (hi - lo) * i / max(1, n - 1) for i, w in enumerate(self.anchors)
        }
        self._anchor_mat = (
            np.vstack([_vector(w) for w in self.anchors]) if n else None
        )

        vocab_path = vocab_path or (DATA / "vocab.txt")
        # dict.fromkeys — прибирає дублікати, зберігаючи порядок
        vocab = list(dict.fromkeys(
            w.strip()
            for w in vocab_path.read_text(encoding="utf-8").splitlines()
            if w.strip() and w.strip() != self.secret and is_known(w.strip())
            # "чемоданчик" не займає місця в рейтингу: це той самий "чемодан"
            and w.strip() not in _aliases()
        ))
        # асоціації поза частотним словником теж мають місце в рейтингу
        vocab += [w for w in self.anchors if w not in set(vocab)]
        self.vocab = vocab
        self._index = {w: i for i, w in enumerate(vocab)}

        index, vectors = _vectors()
        matrix = vectors[[index[w] for w in vocab]].astype(np.float32)
        self._vocab_sims = self._scores(vocab, matrix)
        # слова від найближчого до найдальшого
        self._order = np.argsort(-self._vocab_sims, kind="stable")

    def _scores(self, words: list[str], matrix: np.ndarray) -> np.ndarray:
        """Оцінка близькості кожного слова до загаданого (більше — ближче)."""
        base = matrix @ self._secret_vec
        if self._anchor_mat is None:
            return base
        sims = matrix @ self._anchor_mat.T
        k = min(ASSOC_TOP, sims.shape[1])
        # середнє з k найближчих асоціацій: слово має бути поруч із кількома
        assoc = np.sort(sims, axis=1)[:, -k:].mean(axis=1)
        score = W_BASE * base + W_ASSOC * assoc
        for i, w in enumerate(words):
            if w in self._bonus:
                score[i] += self._bonus[w]
            elif _rhymes(w, self.secret):
                score[i] -= RHYME_PENALTY
        return score

    def resolve(self, word: str) -> str | None:
        """Лема здогадки, яку знає гра, або None ("теремоек")."""
        w = _norm(word)
        # сама форма — для слів лише у множині: "сани" pymorphy3 зводить до "сан"
        candidates = [lemma(word), w]
        for v in _g_variants(w):
            candidates += [lemma(v), v]
        for c in candidates:
            c = base_word(c)
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
            sim = float(self._scores([lem], _vector(lem)[None, :])[0])
        # +2: 1 — саме загадане слово, 1 — переводимо кількість ближчих у позицію
        rank = int(np.sum(self._vocab_sims > sim)) + 2
        return Guess(word=word, lemma=lem, rank=rank, similarity=sim)

    @property
    def vocab_size(self) -> int:
        return len(self.vocab) + 1

    def top(self, n: int = 10) -> list[str]:
        """n найближчих слів (позиції 2, 3, ...) — показуємо після гри."""
        return [self.vocab[i] for i in self._order[:n]]

    def word_at(self, rank: int) -> str:
        """Слово на позиції rank (2 — найближче до загаданого)."""
        return self.vocab[self._order[rank - 2]]

    def hint(self, best_rank: int | None,
             guessed: set[str] | frozenset[str] = frozenset()) -> tuple[str, int] | None:
        """Слово, ближче за найкращу здогадку гравця (але не відповідь).

        Ціль — удвічі ближче за найкращу позицію. Слова, які гравець уже
        називав, і ті, що римуються із загаданим, пропускаємо. None — ближче
        вже нікуди: найкраща здогадка стоїть одразу за відповіддю.
        """
        limit = len(self.vocab) + 2 if best_rank is None else best_rank
        target = FIRST_HINT if best_rank is None else max(2, best_rank // 2)
        target = min(target, limit - 1)

        def ok(rank: int) -> bool:
            w = self.word_at(rank)
            return w not in guessed and (
                w in self._bonus or not _rhymes(w, self.secret)
            )

        # спершу від цілі в бік відповіді, потім — назад до найкращої здогадки
        for rank in [*range(target, 1, -1), *range(target + 1, limit)]:
            if ok(rank):
                return self.word_at(rank), rank
        return None
