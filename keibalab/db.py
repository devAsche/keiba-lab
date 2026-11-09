"""SQLite の保存先。複数の処理が同時に動いても壊れないよう WAL モードで開く。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS months (
    ym TEXT PRIMARY KEY,            -- '2024-11'
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY,    -- YYTTKKDD
    date TEXT NOT NULL,
    kai INTEGER, track TEXT, nichi INTEGER,
    complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS races (
    race_id TEXT PRIMARY KEY,       -- YYTTKKDDRR
    meeting_id TEXT, date TEXT, track TEXT, race_no INTEGER, post_time TEXT,
    name TEXT, grade TEXT, surface TEXT, course_detail TEXT, distance INTEGER,
    weather TEXT, going TEXT, class_text TEXT, class_rank REAL, age_cond TEXT,
    handicap INTEGER, prize1 INTEGER, n_runners INTEGER,
    has_result INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(date);
CREATE TABLE IF NOT EXISTS results (
    race_id TEXT NOT NULL, horse_no INTEGER NOT NULL,
    finish INTEGER, finish_text TEXT, frame INTEGER,
    horse_id TEXT, horse_name TEXT, sex TEXT, age INTEGER,
    weight INTEGER, weight_diff INTEGER, blinker INTEGER,
    time_sec REAL, margin TEXT, passing TEXT, last3f REAL,
    jockey_id TEXT, jockey TEXT, carried REAL,
    popularity INTEGER, win_odds REAL,
    trainer_id TEXT, trainer TEXT,
    PRIMARY KEY (race_id, horse_no)
);
CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);
CREATE TABLE IF NOT EXISTS payouts (
    race_id TEXT NOT NULL, bet_type TEXT NOT NULL, combo TEXT NOT NULL,
    payout INTEGER, popularity INTEGER,
    PRIMARY KEY (race_id, bet_type, combo)
);
CREATE TABLE IF NOT EXISTS entries (
    race_id TEXT NOT NULL, horse_no INTEGER NOT NULL,
    frame INTEGER, horse_id TEXT, horse_name TEXT, sex TEXT, age INTEGER,
    jockey_id TEXT, jockey TEXT, carried REAL, trainer_id TEXT, trainer TEXT,
    sire TEXT, damsire TEXT, weight INTEGER, weight_diff INTEGER,
    odds REAL, scratched INTEGER, fetched_at TEXT,
    PRIMARY KEY (race_id, horse_no)
);
CREATE TABLE IF NOT EXISTS schedule (
    race_id TEXT PRIMARY KEY, date TEXT NOT NULL, post_time TEXT NOT NULL,
    meeting_id TEXT, race_no INTEGER
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
    race_id TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,      -- 取得した時刻（JST, ISO形式）
    minutes_to_post REAL,
    status TEXT, official_at TEXT,  -- official_at は相手側のオッズ更新時刻
    bet_type TEXT NOT NULL,         -- win / place
    horse_no INTEGER NOT NULL,
    odds REAL, odds_max REAL, popularity INTEGER,
    source TEXT,                    -- netkeiba / yahoo
    PRIMARY KEY (race_id, snapshot_at, bet_type, horse_no)
);
CREATE INDEX IF NOT EXISTS idx_snap_race ON odds_snapshots(race_id);
CREATE TABLE IF NOT EXISTS paper_bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at TEXT NOT NULL,       -- 買い目を決めた時刻（発走前であることが大事）
    race_id TEXT NOT NULL, post_time TEXT, minutes_to_post REAL,
    horse_no INTEGER NOT NULL, horse_name TEXT,
    bet_type TEXT NOT NULL DEFAULT 'win',
    odds_at_decision REAL, p_model REAL, p_market REAL, p_final REAL, ev REAL,
    stake REAL NOT NULL, model_version TEXT,
    settled INTEGER NOT NULL DEFAULT 0, final_odds REAL, payout REAL,
    UNIQUE (race_id, horse_no, bet_type, model_version)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


MIGRATIONS = [
    ("races", "cancelled", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """古いDBに後から足した列を追加する。"""
    for table, column, decl in MIGRATIONS:
        if column not in table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, rows: Iterable[dict], columns: list[str] | None = None) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = columns or list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(row.get(c) for c in cols) for row in rows])
    return len(rows)


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
