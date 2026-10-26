"""Сховище стану гри: гравці, здогадки, результати, серії.

Два режими:
  * локально — файл SQLite data/game.db;
  * на хостингу — база Turso (та сама SQLite, але в хмарі), якщо в .env
    задані TURSO_DATABASE_URL і TURSO_AUTH_TOKEN. Диск безкоштовного Render
    стирається при кожному перезапуску, тож локальний файл там не вижив би.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).parent / "data" / "game.db"
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name    TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS guesses (
        user_id INTEGER NOT NULL,
        day     TEXT    NOT NULL,
        word    TEXT    NOT NULL,
        lemma   TEXT    NOT NULL,
        rank    INTEGER NOT NULL,
        ts      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS results (
        user_id  INTEGER NOT NULL,
        day      TEXT    NOT NULL,
        solved   INTEGER NOT NULL DEFAULT 0,
        gave_up  INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )""",
    # номер поточного раунду дня (0 — перше слово, далі /newword)
    """CREATE TABLE IF NOT EXISTS rounds (
        day TEXT PRIMARY KEY,
        n   INTEGER NOT NULL
    )""",
    # загадане слово кожного раунду: щоб не повторювати вже зіграні
    """CREATE TABLE IF NOT EXISTS secrets (
        day  TEXT PRIMARY KEY,
        word TEXT NOT NULL,
        ts   TEXT DEFAULT (datetime('now'))
    )""",
]


@dataclass
class Stats:
    name: str
    wins: int
    played: int
    streak: int
    best_attempts: int | None


# ---------------------------------------------------------------- доступ до БД

def _sqlite_query(sql: str, params: tuple) -> list[dict]:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


_turso_client: httpx.Client | None = None


def _turso_arg(v) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}  # Turso хоче цілі як рядок
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _turso_value(v: dict):
    if v["type"] == "integer":
        return int(v["value"])
    if v["type"] == "null":
        return None
    return v["value"]


def _turso_query(sql: str, params: tuple) -> list[dict]:
    """Запит до Turso через HTTP API (https://docs.turso.tech/sdk/http)."""
    global _turso_client
    if _turso_client is None:
        _turso_client = httpx.Client(
            base_url=TURSO_URL,
            headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
            timeout=15,
        )
    body = {"requests": [
        {"type": "execute",
         "stmt": {"sql": sql, "args": [_turso_arg(p) for p in params]}},
        {"type": "close"},
    ]}
    resp = _turso_client.post("/v2/pipeline", json=body)
    resp.raise_for_status()
    first = resp.json()["results"][0]
    if first["type"] != "ok":
        raise RuntimeError(f"Turso: {first.get('error')}")
    result = first["response"]["result"]
    cols = [c["name"] for c in result["cols"]]
    return [
        {c: _turso_value(v) for c, v in zip(cols, row)}
        for row in result["rows"]
    ]


def _query(sql: str, params: tuple = ()) -> list[dict]:
    if TURSO_URL:
        return _turso_query(sql, params)
    return _sqlite_query(sql, params)


def _one(sql: str, params: tuple = ()) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- операції
#
# Стан гравця рахується за раундом. Ключ раунду (колонка day) — дата
# для першого слова дня і "дата#n" для наступних: старі записи лишаються чинними.

def init() -> None:
    for stmt in SCHEMA:
        _query(stmt)


def round_key(day: date, n: int) -> str:
    return day.isoformat() if n == 0 else f"{day.isoformat()}#{n}"


def current_round(day: date) -> int:
    row = _one("SELECT n FROM rounds WHERE day=?", (day.isoformat(),))
    return row["n"] if row else 0


def set_round(day: date, n: int) -> None:
    _query(
        "INSERT INTO rounds(day, n) VALUES(?, ?) "
        "ON CONFLICT(day) DO UPDATE SET n=excluded.n",
        (day.isoformat(), n),
    )


def get_secret(rnd: str) -> str | None:
    row = _one("SELECT word FROM secrets WHERE day=?", (rnd,))
    return row["word"] if row else None


def save_secret(rnd: str, word: str) -> str:
    """Записує слово раунду, якщо його ще немає; повертає збережене."""
    _query(
        "INSERT INTO secrets(day, word) VALUES(?, ?) ON CONFLICT(day) DO NOTHING",
        (rnd, word),
    )
    return get_secret(rnd)


def secret_history() -> list[str]:
    """Загадані слова від найдавнішого до найновішого."""
    rows = _query("SELECT word FROM secrets ORDER BY ts, rowid")
    return [r["word"] for r in rows]


def played_rounds() -> list[str]:
    """Ключі раундів, у які вже грали, але без запису в secrets."""
    keys = {r["day"] for r in _query(
        "SELECT day FROM guesses UNION SELECT day FROM results"
    )}
    for r in _query("SELECT day, n FROM rounds"):
        d = date.fromisoformat(r["day"])
        keys.update(round_key(d, k) for k in range(r["n"] + 1))
    known = {r["day"] for r in _query("SELECT day FROM secrets")}
    return sorted(keys - known, key=lambda k: (k[:10], int(k[11:] or 0)))


def finished_count(rnd: str) -> int:
    """Скільки гравців завершили раунд: вгадали або здалися."""
    row = _one(
        "SELECT COUNT(*) n FROM results WHERE day=? AND (solved=1 OR gave_up=1)",
        (rnd,),
    )
    return row["n"]


def remember_user(user_id: int, name: str) -> None:
    _query(
        "INSERT INTO users(user_id, name) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
        (user_id, name),
    )


def record_guess(user_id: int, rnd: str, word: str, lemma: str, rank: int) -> None:
    _query(
        "INSERT INTO guesses(user_id, day, word, lemma, rank) VALUES(?, ?, ?, ?, ?)",
        (user_id, rnd, word, lemma, rank),
    )


def already_guessed_lemma(user_id: int, rnd: str, lemma: str) -> int | None:
    """Позиція, якщо гравець уже пробував цю лему в раунді, інакше None."""
    row = _one(
        "SELECT rank FROM guesses WHERE user_id=? AND day=? AND lemma=? LIMIT 1",
        (user_id, rnd, lemma),
    )
    return row["rank"] if row else None


def get_day_guesses(user_id: int, rnd: str) -> list[tuple[str, int]]:
    rows = _query(
        "SELECT lemma, rank FROM guesses WHERE user_id=? AND day=? ORDER BY rank",
        (user_id, rnd),
    )
    return [(r["lemma"], r["rank"]) for r in rows]


def attempts_count(user_id: int, rnd: str) -> int:
    row = _one(
        "SELECT COUNT(*) n FROM guesses WHERE user_id=? AND day=?",
        (user_id, rnd),
    )
    return row["n"]


def get_result(user_id: int, rnd: str) -> dict | None:
    return _one(
        "SELECT * FROM results WHERE user_id=? AND day=?",
        (user_id, rnd),
    )


def set_result(user_id: int, rnd: str, *, solved: bool, gave_up: bool,
               attempts: int) -> None:
    _query(
        "INSERT INTO results(user_id, day, solved, gave_up, attempts) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET "
        "solved=excluded.solved, gave_up=excluded.gave_up, attempts=excluded.attempts",
        (user_id, rnd, int(solved), int(gave_up), attempts),
    )


def _streak(solved_days: set[str], today: date) -> int:
    """Кількість днів поспіль із розгадкою, рахуючи від сьогодні чи вчора."""
    streak = 0
    cur = today
    if today.isoformat() not in solved_days:
        cur = today - timedelta(days=1)  # серія ще жива, якщо сьогодні не грали
    while cur.isoformat() in solved_days:
        streak += 1
        cur -= timedelta(days=1)
    return streak


def get_stats(user_id: int, today: date) -> Stats:
    name_row = _one("SELECT name FROM users WHERE user_id=?", (user_id,))
    name = name_row["name"] if name_row else "Гравець"
    rows = _query(
        "SELECT day, solved, attempts FROM results WHERE user_id=?",
        (user_id,),
    )
    solved = [r for r in rows if r["solved"]]
    # серія — за днями, тож відрізаємо номер раунду ("2026-10-18#2" -> дата)
    solved_days = {r["day"][:10] for r in solved}
    best = min((r["attempts"] for r in solved), default=None)
    return Stats(
        name=name,
        wins=len(solved),
        played=len(rows),
        streak=_streak(solved_days, today),
        best_attempts=best,
    )


def all_player_ids() -> list[int]:
    return [r["user_id"] for r in _query("SELECT user_id FROM users")]


def scoreboard(today: date) -> list[Stats]:
    return sorted(
        (get_stats(uid, today) for uid in all_player_ids()),
        key=lambda s: (s.wins, s.streak),
        reverse=True,
    )
