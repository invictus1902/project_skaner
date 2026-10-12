from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS employees (
                    badge TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    badge TEXT NOT NULL,
                    name TEXT,
                    event_type TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    error TEXT,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_badge ON events(badge, id);
                CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, id);
                CREATE TABLE IF NOT EXISTS directory_cache (
                    kind TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_ops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    local_key TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session_ops_status ON session_ops(status, id);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_employee(self, badge: str, name: str, source: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO employees(badge, name, updated_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(badge) DO UPDATE SET
                    name=excluded.name,
                    updated_at=excluded.updated_at,
                    source=excluded.source
                """,
                (badge, name, now, source),
            )
            self._conn.commit()

    def get_employee(self, badge: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT badge, name, updated_at, source FROM employees WHERE badge = ?",
                (badge,),
            ).fetchone()
        return dict(row) if row else None

    def list_employees(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT badge, name, updated_at, source FROM employees ORDER BY name COLLATE NOCASE, badge"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_employee(self, badge: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM employees WHERE badge = ?", (badge,))
            self._conn.commit()

    def insert_event(
        self,
        badge: str,
        name: str,
        event_type: str,
        scanned_at: datetime,
        payload: dict,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events(badge, name, event_type, scanned_at, status, payload)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    badge,
                    name,
                    event_type,
                    scanned_at.isoformat(timespec="seconds"),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def mark_sent(self, event_id: int, name: str, event_type: str) -> None:
        sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """
                UPDATE events
                SET status='sent', sent_at=?, error=NULL, name=?, event_type=?
                WHERE id=?
                """,
                (sent_at, name, event_type, event_id),
            )
            self._conn.commit()

    def mark_rejected(self, event_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status='rejected', error=? WHERE id=?",
                (error[:500], event_id),
            )
            self._conn.commit()

    def mark_retry(self, event_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status='pending', error=? WHERE id=?",
                (error[:500], event_id),
            )
            self._conn.commit()

    def last_counted_event(self, badge: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, badge, name, event_type, scanned_at, status
                FROM events
                WHERE badge = ? AND status IN ('pending', 'sent')
                ORDER BY id DESC
                LIMIT 1
                """,
                (badge,),
            ).fetchone()
        return dict(row) if row else None

    def recent_events(self, limit: int = 8) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, badge, name, event_type, scanned_at, status, error
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, badge, name, event_type, scanned_at, status, error, payload
                FROM events
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_pending(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE status = 'pending'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def save_directory(self, users: list, sessions: list) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        users_text = json.dumps(_public_rows(users), ensure_ascii=False)
        sessions_text = json.dumps(_public_rows(sessions), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO directory_cache(kind, payload, saved_at)
                VALUES ('users', ?, ?)
                ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload, saved_at=excluded.saved_at
                """,
                (users_text, now),
            )
            self._conn.execute(
                """
                INSERT INTO directory_cache(kind, payload, saved_at)
                VALUES ('sessions', ?, ?)
                ON CONFLICT(kind) DO UPDATE SET payload=excluded.payload, saved_at=excluded.saved_at
                """,
                (sessions_text, now),
            )
            self._conn.commit()

    def load_directory(self) -> tuple[list, list]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, payload FROM directory_cache").fetchall()
        found = {str(row["kind"]): row["payload"] for row in rows}
        return _load_rows(found.get("users")), _load_rows(found.get("sessions"))

    def insert_session_op(self, op: str, user_id: str, session_id: str, local_key: str, payload: dict) -> int:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO session_ops(op, user_id, session_id, local_key, payload, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    op,
                    str(user_id or ""),
                    str(session_id or ""),
                    str(local_key or ""),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def replace_session_payload(self, op_id: int, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE session_ops
                SET payload=?, status='pending', error=NULL
                WHERE id=?
                """,
                (json.dumps(payload, ensure_ascii=False), int(op_id)),
            )
            self._conn.commit()

    def get_session_op(self, op_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM session_ops WHERE id = ?", (int(op_id),)).fetchone()
        return dict(row) if row else None

    def find_pending_create(self, local_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM session_ops
                WHERE status = 'pending' AND op = 'create' AND local_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(local_key or ""),),
            ).fetchone()
        return dict(row) if row else None

    def find_pending_update(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM session_ops
                WHERE status = 'pending' AND op = 'update' AND session_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(session_id or ""),),
            ).fetchone()
        return dict(row) if row else None

    def pending_session_ops(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM session_ops
                WHERE status = 'pending'
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def count_pending_session_ops(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM session_ops WHERE status = 'pending'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_session_op_sent(self, op_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session_ops SET status='sent', error=NULL WHERE id=?",
                (int(op_id),),
            )
            self._conn.commit()

    def mark_session_op_rejected(self, op_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session_ops SET status='rejected', error=? WHERE id=?",
                (str(error or "")[:500], int(op_id)),
            )
            self._conn.commit()

    def mark_session_op_retry(self, op_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session_ops SET status='pending', error=? WHERE id=?",
                (str(error or "")[:500], int(op_id)),
            )
            self._conn.commit()


def _public_rows(rows: list) -> list:
    cleaned = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cleaned.append({key: value for key, value in row.items() if str(key).lower() != "password"})
    return cleaned


def _load_rows(payload: str | None) -> list:
    if not payload:
        return []
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return _public_rows(loaded)
