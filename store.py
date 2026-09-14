"""SQLite record. The HA board is a projection of this, not the other way round.

Nothing is ever deleted — items are archived. Age is measured from `touched_at`,
which only moves when something actually happens to a task, so a task does not
look fresh merely because the brain rewrote its score.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT", "/brain"))
DB_PATH = BRAIN_ROOT / "state" / "brain.db"

TRACKS = ("meta", "focus", "flow", "waiting", "upkeep", "someday")
LANE_FOR_TRACK = {
    "meta": "backlog", "focus": "backlog", "flow": "backlog",
    "waiting": "waiting", "upkeep": "upkeep", "someday": "backlog",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS captures (
    id          TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    local       TEXT,
    source      TEXT,
    text        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'new',   -- new | done | needs_human
    task_id     TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    track         TEXT NOT NULL,
    project       TEXT,
    consequence   TEXT,
    estimate_min  INTEGER,
    due           TEXT,
    blocked_on    TEXT,
    lane          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'needs_action',
    score         REAL NOT NULL DEFAULT 0,
    pinned_until  TEXT,
    snoozed_until TEXT,
    capture_id    TEXT REFERENCES captures(id),
    ha_entity     TEXT,
    ha_uid        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    touched_at    TEXT NOT NULL,
    completed_at  TEXT,
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_lane  ON tasks(lane, archived);
CREATE INDEX IF NOT EXISTS idx_tasks_track ON tasks(track, archived);
CREATE INDEX IF NOT EXISTS idx_tasks_hauid ON tasks(ha_uid);
CREATE INDEX IF NOT EXISTS idx_captures_state ON captures(state);

CREATE TABLE IF NOT EXISTS projects (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    deadline    TEXT,
    questions   TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    worker   TEXT NOT NULL,
    started  TEXT NOT NULL,
    finished TEXT,
    ok       INTEGER,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    alias   TEXT,
    model   TEXT,
    ms      INTEGER,
    ok      INTEGER,
    n_items INTEGER,
    error   TEXT
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_task_id() -> str:
    return "t_" + secrets.token_hex(6)


# Columns added after the first databases were created. SQLite has no
# ADD COLUMN IF NOT EXISTS, so check before adding.
MIGRATIONS = [
    ("tasks", "seq", "INTEGER"),
    ("tasks", "why", "TEXT"),
    ("projects", "rollup_uid", "TEXT"),
    ("projects", "rollup_entity", "TEXT"),
    ("tasks", "caldav_collection", "TEXT"),
    ("tasks", "caldav_etag", "TEXT"),
    ("tasks", "dismissed_until", "TEXT"),
    # Lets a generated task (a chore nudge) be found again next pass instead
    # of being created afresh every time.
    ("tasks", "source_key", "TEXT"),
    ("tasks", "escalations", "INTEGER"),
    ("tasks", "last_alarm_at", "TEXT"),
]


def _migrate(conn) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def upsert_project(conn, slug: str, name: str, summary: str = "",
                   deadline: str | None = None, questions: str = "[]") -> None:
    ts = now_iso()
    conn.execute(
        """INSERT INTO projects (slug,name,summary,deadline,questions,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(slug) DO UPDATE SET
             name=excluded.name, summary=excluded.summary,
             deadline=COALESCE(excluded.deadline, projects.deadline),
             questions=excluded.questions, updated_at=excluded.updated_at""",
        (slug, name, summary, deadline, questions, ts, ts),
    )


def get_project(conn, slug: str):
    return conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()


def project_tasks(conn, slug: str, include_done: bool = True):
    sql = "SELECT * FROM tasks WHERE archived=0 AND project=?"
    if not include_done:
        sql += " AND status!='completed'"
    sql += " ORDER BY CASE lane WHEN 'in_progress' THEN 0 WHEN 'backlog' THEN 1 ELSE 2 END, seq, score DESC"
    return conn.execute(sql, (slug,)).fetchall()


def in_progress_count(conn, slug: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE archived=0 AND status!='completed' "
        "AND project=? AND lane='in_progress'", (slug,)).fetchone()["c"]


# --- captures -------------------------------------------------------------

def ingest_capture(conn, rec: dict) -> bool:
    """Insert a capture read from the inbox file. Idempotent on id."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO captures (id, at, local, source, text) VALUES (?,?,?,?,?)",
        (rec["id"], rec["at"], rec.get("local"), rec.get("source"), rec["text"]),
    )
    return cur.rowcount > 0


def pending_captures(conn, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM captures WHERE state='new' AND attempts < 3 ORDER BY at LIMIT ?",
        (limit,),
    ).fetchall()


def mark_capture(conn, capture_id: str, state: str, task_id: str | None = None,
                 error: str | None = None) -> None:
    conn.execute(
        "UPDATE captures SET state=?, task_id=?, error=?, attempts=attempts+1 WHERE id=?",
        (state, task_id, error, capture_id),
    )


def bump_attempt(conn, capture_id: str, error: str) -> None:
    """Count a failed pass without burying the capture: 3 strikes -> needs_human."""
    conn.execute("UPDATE captures SET attempts=attempts+1, error=? WHERE id=?", (error, capture_id))
    conn.execute(
        "UPDATE captures SET state='needs_human' WHERE id=? AND attempts >= 3", (capture_id,)
    )


# --- tasks ----------------------------------------------------------------

def normalise_title(title: str) -> str:
    return " ".join((title or "").lower().split()).strip(" .!?")


def find_duplicate(conn, title: str, track: str) -> str | None:
    """An active task with the same normalised title on the same track.

    Capturing the same thought twice is normal with ADHD — you forget you
    already logged it. Two identical cards is the system amplifying the
    problem it exists to solve, so the second capture attaches to the first
    instead of creating a twin.
    """
    target = normalise_title(title)
    if not target:
        return None
    for row in conn.execute(
        "SELECT id, title FROM tasks WHERE archived=0 AND status!='completed' AND track=?",
        (track,),
    ):
        if normalise_title(row["title"]) == target:
            return row["id"]
    return None


def create_task(conn, **f) -> str:
    task_id = f.get("id") or new_task_id()
    ts = now_iso()
    track = f["track"]
    conn.execute(
        """INSERT INTO tasks (id,title,note,track,project,consequence,estimate_min,due,
                              blocked_on,lane,capture_id,created_at,updated_at,touched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, f["title"], f.get("note", "") or "", track, f.get("project"),
         f.get("consequence"), f.get("estimate_min"), f.get("due"), f.get("blocked_on"),
         f.get("lane") or LANE_FOR_TRACK.get(track, "backlog"), f.get("capture_id"),
         ts, ts, ts),
    )
    return task_id


def set_ha_link(conn, task_id: str, entity: str, uid: str) -> None:
    conn.execute("UPDATE tasks SET ha_entity=?, ha_uid=?, updated_at=? WHERE id=?",
                 (entity, uid, now_iso(), task_id))


def set_score(conn, task_id: str, value: float) -> None:
    """Scores churn hourly — deliberately does NOT move touched_at."""
    conn.execute("UPDATE tasks SET score=? WHERE id=?", (value, task_id))


def touch(conn, task_id: str) -> None:
    ts = now_iso()
    conn.execute("UPDATE tasks SET touched_at=?, updated_at=? WHERE id=?", (ts, ts, task_id))


def active_tasks(conn, lane: str | None = None) -> list[sqlite3.Row]:
    if lane:
        return conn.execute(
            "SELECT * FROM tasks WHERE archived=0 AND status!='completed' AND lane=? "
            "ORDER BY score DESC, created_at", (lane,)).fetchall()
    return conn.execute(
        "SELECT * FROM tasks WHERE archived=0 AND status!='completed' "
        "ORDER BY score DESC, created_at").fetchall()


def task_by_source(conn, source_key: str):
    return conn.execute(
        "SELECT * FROM tasks WHERE source_key=? AND archived=0", (source_key,)).fetchone()


def task_by_uid(conn, uid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE ha_uid=?", (uid,)).fetchone()


def age_days(row, now: datetime | None = None) -> float:
    now = now or datetime.now()
    try:
        t = datetime.fromisoformat(row["touched_at"])
    except (TypeError, ValueError):
        return 0.0
    return max((now - t).total_seconds() / 86400.0, 0.0)


# --- bookkeeping ----------------------------------------------------------

def start_run(conn, worker: str) -> int:
    cur = conn.execute("INSERT INTO runs (worker, started) VALUES (?,?)", (worker, now_iso()))
    return cur.lastrowid


def finish_run(conn, run_id: int, ok: bool, detail: str = "") -> None:
    conn.execute("UPDATE runs SET finished=?, ok=?, detail=? WHERE id=?",
                 (now_iso(), 1 if ok else 0, detail[:500], run_id))


def log_llm(conn, alias, model, ms, ok, n_items=None, error=None) -> None:
    conn.execute(
        "INSERT INTO llm_calls (at,alias,model,ms,ok,n_items,error) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), alias, model, ms, 1 if ok else 0, n_items, (error or "")[:300]),
    )
