"""Persistent SQLite storage for sync jobs and transfer task history."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CONFIG_DIR
from .paths import migrate_legacy_artifacts, tasks_db_path


class Storage:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = Storage(db_path=db_path)
            return cls._instance

    @classmethod
    def reset_instance_for_tests(cls):
        with cls._lock:
            if cls._instance is not None:
                conn = getattr(cls._instance._local, "conn", None)
                if conn is not None:
                    conn.close()
                    cls._instance._local.conn = None
            cls._instance = None

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = db_path
        else:
            migrate_legacy_artifacts()
            self.db_path = str(tasks_db_path())

        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for high concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return self._local.conn

    def close_thread_connection(self) -> None:
        """Close this thread's SQLite connection when its work is complete."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_db(self):
        conn = self._get_connection()
        with conn:
            # 1. Sync Jobs Table (Persistent sync rules & schedules)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    dest TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'sync',
                    skip_existing INTEGER NOT NULL DEFAULT 1,
                    recursive INTEGER NOT NULL DEFAULT 1,
                    interval_seconds INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_status TEXT DEFAULT NULL,
                    last_run_at REAL DEFAULT NULL,
                    next_run_at REAL DEFAULT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    extra_data TEXT DEFAULT '{}'
                );
            """)

            # 2. Transfer Tasks History Table (Full run log and persistence)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transfer_tasks (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    source TEXT NOT NULL,
                    dest TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_bytes INTEGER DEFAULT 0,
                    transferred_bytes INTEGER DEFAULT 0,
                    total_files INTEGER DEFAULT 1,
                    files_completed INTEGER DEFAULT 0,
                    current_file TEXT DEFAULT '',
                    speed_bytes_sec REAL DEFAULT 0,
                    error TEXT DEFAULT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL DEFAULT NULL,
                    finished_at REAL DEFAULT NULL,
                    extra_data TEXT DEFAULT '{}',
                    FOREIGN KEY (job_id) REFERENCES sync_jobs(id) ON DELETE SET NULL
                );
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_job_id ON transfer_tasks(job_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON transfer_tasks(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON transfer_tasks(created_at DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON sync_jobs(status);")

    # ==========================================
    # Sync Jobs CRUD
    # ==========================================

    def create_job(
        self,
        job_id: str,
        name: str,
        source: str,
        dest: str,
        mode: str = "sync",
        skip_existing: bool = True,
        recursive: bool = True,
        interval_seconds: int = 0,
        status: str = "active",
    ) -> Dict[str, Any]:
        now = time.time()
        next_run = (now + interval_seconds) if interval_seconds > 0 else None

        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO sync_jobs (
                    id, name, source, dest, mode, skip_existing, recursive,
                    interval_seconds, status, created_at, updated_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    source,
                    dest,
                    mode,
                    1 if skip_existing else 0,
                    1 if recursive else 0,
                    int(interval_seconds),
                    status,
                    now,
                    now,
                    next_run,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        if status:
            cur.execute("SELECT * FROM sync_jobs WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            cur.execute("SELECT * FROM sync_jobs ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]

    def update_job(self, job_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        valid_cols = {
            "name", "source", "dest", "mode", "skip_existing", "recursive",
            "interval_seconds", "status", "last_status", "last_run_at", "next_run_at",
        }
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in valid_cols:
                updates.append(f"{k} = ?")
                if isinstance(v, bool):
                    params.append(1 if v else 0)
                else:
                    params.append(v)

        if not updates:
            return self.get_job(job_id)

        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(job_id)

        conn = self._get_connection()
        with conn:
            conn.execute(f"UPDATE sync_jobs SET {', '.join(updates)} WHERE id = ?", params)
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        conn = self._get_connection()
        with conn:
            cur = conn.execute("DELETE FROM sync_jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    # ==========================================
    # Task History & Persistence
    # ==========================================

    def save_task(self, task_dict: Dict[str, Any]) -> None:
        """Insert or update a task record in transfer_tasks table."""
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO transfer_tasks (
                    id, job_id, source, dest, mode, status,
                    total_bytes, transferred_bytes, total_files, files_completed,
                    current_file, speed_bytes_sec, error, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    total_bytes = excluded.total_bytes,
                    transferred_bytes = excluded.transferred_bytes,
                    total_files = excluded.total_files,
                    files_completed = excluded.files_completed,
                    current_file = excluded.current_file,
                    speed_bytes_sec = excluded.speed_bytes_sec,
                    error = excluded.error,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at
                """,
                (
                    task_dict["id"],
                    task_dict.get("job_id"),
                    task_dict["source"],
                    task_dict["dest"],
                    task_dict.get("mode", "copy"),
                    task_dict.get("status", "pending"),
                    task_dict.get("total_bytes", 0),
                    task_dict.get("transferred_bytes", 0),
                    task_dict.get("total_files", 1),
                    task_dict.get("files_completed", 0),
                    task_dict.get("current_file", ""),
                    task_dict.get("speed_bytes_sec", 0.0),
                    task_dict.get("error"),
                    task_dict.get("created_at", time.time()),
                    task_dict.get("started_at"),
                    task_dict.get("finished_at"),
                ),
            )

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM transfer_tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_tasks(
        self,
        limit: int = 100,
        status: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cur = conn.cursor()
        query = "SELECT * FROM transfer_tasks"
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if job_id:
            conditions.append("job_id = ?")
            params.append(job_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

    def clean_interrupted_tasks(self) -> int:
        """Mark any task that was 'running' or 'pending' when the app died as 'interrupted'."""
        conn = self._get_connection()
        with conn:
            cur = conn.execute(
                """
                UPDATE transfer_tasks
                SET status = 'interrupted',
                    error = 'Process exited before task completion',
                    finished_at = ?
                WHERE status IN ('running', 'pending', 'cancelling')
                """,
                (time.time(),),
            )
            return cur.rowcount

    def clear_tasks(self, only_finished: bool = True) -> int:
        conn = self._get_connection()
        with conn:
            if only_finished:
                cur = conn.execute(
                    "DELETE FROM transfer_tasks WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')"
                )
            else:
                cur = conn.execute("DELETE FROM transfer_tasks")
            return cur.rowcount
