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
order = np.argsort(-g._vocab_sims)
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
hint, _ = g.hint(400)
assert not engine._rhymes(hint, "брова"), hint
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

# кожне слово рейтингу має свою позицію; рідкісні іменники теж у рейтингу
g = engine.Game("цукерка")
ranks = [g.guess(w).rank for w in ["попільничка", "пачка", "обгортка"]]
assert len(set(ranks)) == 3, ranks
assert all(g.guess(w).exact for w in ["попільничка", "обгортка"])
nonnoun = g.guess("солодкий")
assert not nonnoun.exact and nonnoun.rank_text.startswith("≈"), nonnoun
assert g.rank_text("пачка", 5) == "5" and g.rank_text("солодкий", 5) == "≈5"
print("унікальні позиції: OK")
