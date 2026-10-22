"""Готує словник для рейтингу з частотних лем fastText (data/words.txt).

Створює:
  data/vocab.txt - словник для рейтингу: найчастіші іменники, без
                   зменшувальних форм ("чемоданчик") і хибних лем ("острови")

Рейтинг будуємо лише з іменників: загадані слова — іменники, і поруч мають
стояти пов'язані за змістом речі ("годинник" -> "циферблат", "будильник"),
а не слова, що просто часто трапляються поряд ("кишеньковий", "точний").
Здогадка-прикметник чи дієслово однаково отримує позицію.

Список слів для загадування (data/secret_words.txt) вивірений вручну.

Запуск: python build_data.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"

# скільки іменників беремо у словник для рейтингу
VOCAB_SIZE = 12000

# українські літери + апостроф
UK_RE = re.compile(r"^[абвгґдеєжзиіїйклмнопрстуфхцчшщьюя'’]+$")

# лишаємо іменники, дієслова, прикметники (теги pymorphy3). Прислівники
# відкидаємо: серед частотних це здебільшого "так/тут/дуже" і русизми
# ("конечно", "сюда"), які pymorphy3 не позначає
KEEP_POS = {"NOUN", "VERB", "INFN", "ADJF", "ADJS"}
# Dist — русизми/суржик, Slng — сленг, решта — імена власні
BAD_TAGS = {"Dist", "Slng", "Name", "Surn", "Patr", "Abbr"}

# зменшувальні суфікси (називний відмінок), довші — першими
DIM_SUFFIXES = sorted({
    "ечко", "ечка", "ечок", "очок", "очка", "ичок", "ичка", "ятко", "атко",
    "енько", "енька", "онька", "чик", "чок", "чка", "чко", "ик", "ок", "ко",
    "ка", "ець", "це", "ця", "еня", "еня", "ятко", "ище", "ара", "яра", "ька",
    "уся", "юся", "усь", "юсь", "уня",
}, key=len, reverse=True)
# чергування в кінці основи: книжка -> книга, вушко -> вухо
DIM_ALTERNATIONS = [("ч", "к"), ("ч", "ц"), ("ж", "г"), ("ш", "х"), ("ш", "с")]
# закінчення основного слова; "ка", "ко", "це" — для подвійних суфіксів:
# коробочка -> коробка, ліжечко -> ліжко, дзеркальце -> дзеркало
DIM_ENDINGS = ["", "а", "о", "я", "е", "ь", "ка", "ко", "ок", "це", "ець"]
# наскільки схожими мають бути вектори слова й основи: відсіює
# випадкові збіги ("податок" — не "подать"). Хибне спрацювання лише
# прибирає слово з рейтингу, тож поріг невисокий
DIM_MIN_SIM = 0.45
# відмінкова форма іншого слова, яку словник вважає окремим словом:
# "оленя" -> "олень", "кури" -> "курка"
FORM_MIN_SIM = 0.55
# самостійні слова, які pymorphy3 вважає ще й формою частішого
# ("рот" — родовий від "рота") і які фільтри помилково відкидають
KEEP_WORDS = {"рот", "пані", "ставка", "струмок", "корінь", "білок"}
# слова, що стоять близько до всього підряд і засмічують рейтинг
# ("грудка" була в топ-100 у кожного десятого загаданого)
NOISE_WORDS = {"грудка", "ступка", "горнятко"}


def _more_frequent(a: str, b: str) -> bool:
    """a трапляється частіше за b: data/words.txt іде в частотному порядку."""
    import engine

    index = engine._vectors()[0]
    return a in index and index[a] < index[b]


def is_content_lemma(word: str) -> bool:
    """Слово — початкова форма іменника, дієслова чи прикметника.

    Дивимось усі розбори: "ніж" pymorphy3 першим вважає сполучником,
    а "свято" прислівником, але це й іменники.
    """
    import engine

    return any(
        p.normal_form == word and p.tag.POS in KEEP_POS
        and not BAD_TAGS & p.tag.grammemes
        for p in engine._morph().parse(word)
    )


def is_plain_noun(word: str) -> bool:
    """Іменник, а не прикметник у ролі іменника ("рудий", "скажений")."""
    import engine

    parses = engine._morph().parse(word)
    return (
        any(p.normal_form == word and p.tag.POS == "NOUN"
            and not BAD_TAGS & p.tag.grammemes for p in parses)
        # і не форма частішого прикметника: "солона", "хижа" (але "дорога")
        and not any(p.tag.POS in {"ADJF", "ADJS"}
                    and (p.normal_form == word or _more_frequent(p.normal_form, word))
                    for p in parses)
    )


def is_other_form(word: str) -> bool:
    """Форма іншого слова з тим самим змістом: "оленя" (олень), "кури" (курка)."""
    import engine

    vec = engine._vector(word)
    return any(
        p.normal_form != word and p.tag.POS == "NOUN" and _more_frequent(p.normal_form, word)
        and not BAD_TAGS & p.tag.grammemes
        and float(vec @ engine._vector(p.normal_form)) >= FORM_MIN_SIM
        for p in engine._morph().parse(word)
    )


def dim_bases(word: str) -> set[str]:
    """Можливі основні слова для зменшувальної форми: "котик" -> {"кот", "кіт", ...}."""
    out: set[str] = set()
    for suf in DIM_SUFFIXES:
        if not word.endswith(suf) or len(word) - len(suf) < 2:
            continue
        stem = word[:-len(suf)]
        stems = {stem}
        stems |= {stem[:-1] + b for a, b in DIM_ALTERNATIONS if stem.endswith(a)}
        # о/е в закритому складі основи: кот-ик -> кіт, пес-ик -> пес
        for st in list(stems):
            for i in range(max(len(st) - 3, 0), len(st)):
                if st[i] in "ое":
                    stems.add(st[:i] + "і" + st[i + 1:])
        # випадний голосний: пензл-ик -> пензель, корабл-ик -> корабель
        if len(stem) >= 3:
            stems |= {stem[:-1] + v + stem[-1] for v in "еоі"}
        out |= {st + e for st in stems for e in DIM_ENDINGS}
    out.discard(word)
    return out


def is_diminutive(word: str) -> bool:
    """"чемоданчик", "котик": є відоме основне слово з близьким вектором."""
    import engine

    vec = engine._vector(word)
    return any(
        len(b) >= 3 and _more_frequent(b, word)
        and float(vec @ engine._vector(b)) >= DIM_MIN_SIM
        for b in dim_bases(word)
    )


def build_vocab() -> list[str]:
    import engine

    aliases = engine._aliases()
    vocab: list[str] = []
    # data/words.txt — леми з векторами в частотному порядку
    for w in (DATA / "words.txt").read_text(encoding="utf-8").splitlines():
        if len(w) < 3 or w in aliases or w in NOISE_WORDS or not is_plain_noun(w):
            continue
        if w not in KEEP_WORDS and (is_diminutive(w) or is_other_form(w)):
            continue
        vocab.append(w)
        if len(vocab) >= VOCAB_SIZE:
            break
    return vocab


def main() -> None:
    vocab = build_vocab()
    (DATA / "vocab.txt").write_text("\n".join(vocab), encoding="utf-8")
    print(f"Словник для рейтингу: {len(vocab)} -> data/vocab.txt")


if __name__ == "__main__":
    main()
