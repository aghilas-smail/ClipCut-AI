"""
ClipCut AI — SQLite persistence layer
"""
import sqlite3, json
from datetime import datetime
from config import DB_PATH


def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id         TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        conn.commit()


def db_save(job_id: str, data: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs(id, data, created_at) "
            "VALUES(?, ?, COALESCE((SELECT created_at FROM jobs WHERE id=?), ?))",
            (job_id, json.dumps(data, ensure_ascii=False),
             job_id, datetime.utcnow().isoformat())
        )
        conn.commit()


def db_list(limit: int = 50) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, data, created_at FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [{"id": r[0], **json.loads(r[1]), "created_at": r[2]} for r in rows]


def db_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    return row[0] if row else 0
