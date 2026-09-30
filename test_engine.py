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
