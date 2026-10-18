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

def init() -> None:
    for stmt in SCHEMA:
        _query(stmt)


def remember_user(user_id: int, name: str) -> None:
    _query(
        "INSERT INTO users(user_id, name) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
        (user_id, name),
    )


def record_guess(user_id: int, day: date, word: str, lemma: str, rank: int) -> None:
    _query(
        "INSERT INTO guesses(user_id, day, word, lemma, rank) VALUES(?, ?, ?, ?, ?)",
        (user_id, day.isoformat(), word, lemma, rank),
    )


def already_guessed_lemma(user_id: int, day: date, lemma: str) -> int | None:
    """Позиція, якщо гравець уже пробував цю лему сьогодні, інакше None."""
    row = _one(
        "SELECT rank FROM guesses WHERE user_id=? AND day=? AND lemma=? LIMIT 1",
        (user_id, day.isoformat(), lemma),
    )
    return row["rank"] if row else None


def get_day_guesses(user_id: int, day: date) -> list[tuple[str, int]]:
    rows = _query(
        "SELECT lemma, rank FROM guesses WHERE user_id=? AND day=? ORDER BY rank",
        (user_id, day.isoformat()),
    )
    return [(r["lemma"], r["rank"]) for r in rows]


def attempts_count(user_id: int, day: date) -> int:
    row = _one(
        "SELECT COUNT(*) n FROM guesses WHERE user_id=? AND day=?",
        (user_id, day.isoformat()),
    )
    return row["n"]


def get_result(user_id: int, day: date) -> dict | None:
    return _one(
        "SELECT * FROM results WHERE user_id=? AND day=?",
        (user_id, day.isoformat()),
    )


def set_result(user_id: int, day: date, *, solved: bool, gave_up: bool,
               attempts: int) -> None:
    _query(
        "INSERT INTO results(user_id, day, solved, gave_up, attempts) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET "
        "solved=excluded.solved, gave_up=excluded.gave_up, attempts=excluded.attempts",
        (user_id, day.isoformat(), int(solved), int(gave_up), attempts),
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
    solved_days = {r["day"] for r in rows if r["solved"]}
    wins = len(solved_days)
    played = len(rows)
    best = min((r["attempts"] for r in rows if r["solved"]), default=None)
    return Stats(
        name=name,
        wins=wins,
        played=played,
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
