"""SQLite-backed state storage for incremental sync."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from yuque2obsidian.models import SyncState


class Storage:
    """Manages sync state in a SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS docs (
                    namespace TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    title TEXT,
                    content_updated_at TEXT,
                    last_synced_at TEXT,
                    file_path TEXT,
                    PRIMARY KEY (namespace, slug)
                );

                CREATE TABLE IF NOT EXISTS repos (
                    namespace TEXT PRIMARY KEY,
                    name TEXT,
                    type TEXT,
                    last_synced_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT,
                    ended_at TEXT,
                    docs_total INTEGER,
                    docs_updated INTEGER,
                    docs_skipped INTEGER,
                    errors TEXT
                );
                """
            )
            conn.commit()

    def get_doc_state(self, namespace: str, slug: str) -> Optional[SyncState]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM docs WHERE namespace = ? AND slug = ?",
                (namespace, slug),
            ).fetchone()
        if row is None:
            return None
        return SyncState(
            namespace=row["namespace"],
            slug=row["slug"],
            title=row["title"] or "",
            content_updated_at=_parse_dt(row["content_updated_at"]),
            last_synced_at=_parse_dt(row["last_synced_at"]),
            file_path=row["file_path"] or None,
        )

    def upsert_doc_state(self, state: SyncState) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO docs (namespace, slug, title, content_updated_at,
                                  last_synced_at, file_path)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, slug) DO UPDATE SET
                    title = excluded.title,
                    content_updated_at = excluded.content_updated_at,
                    last_synced_at = excluded.last_synced_at,
                    file_path = excluded.file_path
                """,
                (
                    state.namespace,
                    state.slug,
                    state.title,
                    _fmt_dt(state.content_updated_at),
                    _fmt_dt(state.last_synced_at),
                    state.file_path,
                ),
            )
            conn.commit()

    def upsert_repo_state(self, namespace: str, name: str, repo_type: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO repos (namespace, name, type, last_synced_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    last_synced_at = excluded.last_synced_at
                """,
                (namespace, name, repo_type, _fmt_dt(datetime.now())),
            )
            conn.commit()

    def list_repo_namespaces(self) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute("SELECT namespace FROM repos").fetchall()
        return [row["namespace"] for row in rows]

    def start_sync_log(self) -> int:
        now = datetime.now().isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_log (started_at) VALUES (?)",
                (now,),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def finish_sync_log(
        self,
        log_id: int,
        docs_total: int,
        docs_updated: int,
        docs_skipped: int,
        errors: list[str],
    ) -> None:
        now = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE sync_log
                SET ended_at = ?,
                    docs_total = ?,
                    docs_updated = ?,
                    docs_skipped = ?,
                    errors = ?
                WHERE id = ?
                """,
                (
                    now,
                    docs_total,
                    docs_updated,
                    docs_skipped,
                    json.dumps(errors, ensure_ascii=False),
                    log_id,
                ),
            )
            conn.commit()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()
