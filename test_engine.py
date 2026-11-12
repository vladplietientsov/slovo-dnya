import time
import engine

t = time.time()
g = engine.Game("кіт")
print(f"load+rank: {time.time()-t:.1f}s, vocab={g.vocab_size}")
for w in ["кіт", "котів", "кошеня", "собака", "тигр", "миша",
          "тварина", "молоко", "стіл", "податок", "демократія", "банан"]:
    r = g.guess(w)
    print(f"{r.emoji} {w:12} -> поз {r.rank:6}  sim {r.similarity:.3f}  лема={r.lemma}")
print("слово дня:", engine.daily_secret())

# позиція слова зі словника = його місце за схожістю (+1 за саме загадане)
import numpy as np
g = engine.Game("шоколад")
order = g._order
for k, i in enumerate(order[:200]):
    w = g.vocab[i]
    r = g.guess(w)
    assert r.lemma == w, f"{w} лематизується в {r.lemma}"
    assert r.rank == k + 2, f"{w}: позиція {r.rank}, очікували {k + 2}"
print("рейтинг узгоджений: OK")

# регресії: рейтинг за змістом, а не за римою; невідомі слова; лематизація
g = engine.Game("брова")
top = [g.vocab[i] for i in np.argsort(-g._vocab_sims)[:10]]
assert not any(engine._rhymes(w, "брова") for w in top), top
assert g.guess("обличчя").rank < 100 and g.guess("вдова").rank > 500
hint, hint_rank = g.hint(400)
assert not engine._rhymes(hint, "брова"), hint
assert hint_rank < 400
assert g.guess("теремоек") is None, "неіснуюче слово не має позиції"
assert g.guess("гудзик").rank == g.guess("ґудзик").rank
assert g.guess("пам’ять").lemma == g.guess("памʼять").lemma == "пам'ять"
for word, lem in [("сани", "сани"), ("окуляри", "окуляри"), ("коти", "кіт"),
                  ("люди", "людина"), ("ніж", "ніж"), ("брови", "брова")]:
    assert engine.lemma(word) == lem, f"{word} -> {engine.lemma(word)}, очікували {lem}"
assert engine.Game("сани").guess("сани").is_win
for w in engine.load_secret_words():
    assert engine.is_known(engine.lemma(w)), f"загадане «{w}» без вектора"
print("регресії: OK")

# зменшувальні й хибні форми — це основне слово, і в рейтингу їх немає
g = engine.Game("чемодан")
assert g.guess("чемоданчик").is_win
assert g.guess("валізка").lemma == "валіза"
assert engine.Game("кіт").guess("котик").is_win
assert g.guess("острови").lemma == "острів"
assert "чемоданчик" not in g.vocab and "котик" not in g.vocab
top = [g.vocab[i] for i in np.argsort(-g._vocab_sims)[:100]]
assert not {"чемоданчик", "валізка", "пальчик", "стільчик"} & set(top), top
print("зменшувальні: OK")

# асоціації: поруч мають стояти слова, які назвала б людина, а не лише
# слова того самого типу ("око" -> "брова", "вія"; "школа" -> "учень", "урок")
assoc = engine._associations()
for w in engine.load_secret_words():
    assert len(engine.anchors_for(engine.lemma(w)) or engine.anchors_for(w)) >= 8, \
        f"замало асоціацій до «{w}»"
for secret, near in [
    ("око", ["брова", "зір", "повіка", "сльоза"]),
    ("школа", ["учень", "вчитель", "урок", "парта"]),
    ("вікно", ["шибка", "підвіконня", "штора"]),
    ("ключ", ["замок", "двері"]),
    ("весна", ["квітень", "березень", "пролісок"]),
    ("гроші", ["гаманець", "зарплата", "монета"]),
]:
    g = engine.Game(secret)
    for w in near:
        assert g.guess(w).rank <= 30, f"{secret}: «{w}» на позиції {g.guess(w).rank}"
    assert g.guess("податок" if secret != "гроші" else "ведмідь").rank > 1000
    # топ-10 після гри: без відповіді, без повторів, без рим
    top = g.top(10)
    assert len(set(top)) == 10 and secret not in top
    assert [g.guess(w).rank for w in top] == list(range(2, 12)), top
print("асоціації: OK")

# підказка завжди ближча за найкращу здогадку, не повторюється і не видає відповідь
g = engine.Game("око")
for best in [None, 5000, 400, 50, 10, 4, 3]:
    w, r = g.hint(best)
    assert w != g.secret and r >= 2 and (best is None or r < best), (best, w, r)
assert g.hint(2) is None, "ближче за позицію 2 лише відповідь"
seen: set[str] = set()
best = None
for _ in range(12):
    h = g.hint(best, seen)
    if h is None:
        break
    assert h[0] not in seen and (best is None or h[1] < best), h
    seen.add(h[0])
    best = h[1]
assert best == 2, f"ланцюжок підказок має дійти до позиції 2, а не {best}"
# коли найближчі слова вже названі, підказка бере наступне вільне
assert g.hint(4, {g.word_at(2)})[0] == g.word_at(3)
assert g.hint(3, {g.word_at(2)}) is None
print("підказки: OK")
