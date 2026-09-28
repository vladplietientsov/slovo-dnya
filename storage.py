"""Сховище стану гри на SQLite: підписники, здогадки, результати, серії."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "game.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id    INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    name       TEXT,
    created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name    TEXT
);
CREATE TABLE IF NOT EXISTS guesses (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    word    TEXT    NOT NULL,
    lemma   TEXT    NOT NULL,
    rank    INTEGER NOT NULL,
    ts      TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS results (
    user_id  INTEGER NOT NULL,
    day      TEXT    NOT NULL,
    solved   INTEGER NOT NULL DEFAULT 0,
    gave_up  INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""


@dataclass
class Stats:
    name: str
    wins: int
    played: int
    streak: int
    best_attempts: int | None


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def remember_user(user_id: int, name: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO users(user_id, name) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
            (user_id, name),
        )


def add_subscriber(chat_id: int, user_id: int, name: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO subscribers(chat_id, user_id, name) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name",
            (chat_id, user_id, name),
        )


def get_subscriber_chats() -> list[int]:
    with _conn() as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM subscribers")]


def record_guess(user_id: int, day: date, word: str, lemma: str, rank: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO guesses(user_id, day, word, lemma, rank) VALUES(?, ?, ?, ?, ?)",
            (user_id, day.isoformat(), word, lemma, rank),
        )


def already_guessed_lemma(user_id: int, day: date, lemma: str) -> int | None:
    """Позиція, якщо гравець уже пробував цю лему сьогодні, інакше None."""
    with _conn() as c:
        row = c.execute(
            "SELECT rank FROM guesses WHERE user_id=? AND day=? AND lemma=? LIMIT 1",
            (user_id, day.isoformat(), lemma),
        ).fetchone()
        return row["rank"] if row else None


def get_day_guesses(user_id: int, day: date) -> list[tuple[str, int]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT lemma, rank FROM guesses WHERE user_id=? AND day=? ORDER BY rank",
            (user_id, day.isoformat()),
        ).fetchall()
        return [(r["lemma"], r["rank"]) for r in rows]


def attempts_count(user_id: int, day: date) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM guesses WHERE user_id=? AND day=?",
            (user_id, day.isoformat()),
        ).fetchone()
        return row["n"]


def get_result(user_id: int, day: date) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM results WHERE user_id=? AND day=?",
            (user_id, day.isoformat()),
        ).fetchone()


def set_result(user_id: int, day: date, *, solved: bool, gave_up: bool,
               attempts: int) -> None:
    with _conn() as c:
        c.execute(
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
    with _conn() as c:
        name_row = c.execute(
            "SELECT name FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        name = name_row["name"] if name_row else "Гравець"
        rows = c.execute(
            "SELECT day, solved, attempts FROM results WHERE user_id=?",
            (user_id,),
        ).fetchall()
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
    with _conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users")]


def scoreboard(today: date) -> list[Stats]:
    return sorted(
        (get_stats(uid, today) for uid in all_player_ids()),
        key=lambda s: (s.wins, s.streak),
        reverse=True,
    )
