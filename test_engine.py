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
