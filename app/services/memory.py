"""
本地记忆存储（M1：基础设施层，不含任何业务集成）。

三张表对应三类记忆：
- material_memory：跨任务素材记忆（M2 接 ranker）
- script_samples：脚本接受/拒绝/手改样本（M3 接 critic agent）
- failure_records：失败模式记录（M3 消费 task_summary 日志）

存储位置：./storage/memory/memory.db（SQLite，stdlib 实现，无新依赖）。
线程安全：单连接 + 互斥锁，因为 task pipeline 用了 ThreadPoolExecutor。
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from app.utils import utils

_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS _meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS material_memory (
  url             TEXT PRIMARY KEY,
  subject         TEXT,
  search_terms    TEXT,
  used_count      INTEGER NOT NULL DEFAULT 0,
  in_final_count  INTEGER NOT NULL DEFAULT 0,
  ranker_score    REAL,
  last_used_at    INTEGER NOT NULL,
  task_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_material_subject   ON material_memory(subject);
CREATE INDEX IF NOT EXISTS idx_material_last_used ON material_memory(last_used_at);

CREATE TABLE IF NOT EXISTS script_samples (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT,
  subject     TEXT,
  script      TEXT NOT NULL,
  status      TEXT NOT NULL,
  edited_to   TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_script_subject ON script_samples(subject);
CREATE INDEX IF NOT EXISTS idx_script_status  ON script_samples(status);

CREATE TABLE IF NOT EXISTS failure_records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT,
  stage       TEXT NOT NULL,
  error       TEXT,
  provider    TEXT,
  recovery    TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failure_stage ON failure_records(stage);
"""


def _serialize_terms(terms) -> Optional[str]:
    if terms is None:
        return None
    if isinstance(terms, str):
        return terms
    return json.dumps(list(terms), ensure_ascii=False)


def _parse_terms(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [raw]
    except (ValueError, TypeError):
        return [raw]


def _row_to_material(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["search_terms"] = _parse_terms(d.get("search_terms"))
    return d


class MemoryStore:
    """SQLite-backed memory store. Instantiate once per process; connection is lazy."""

    def __init__(self, db_path: Optional[str] = None):
        # db_path=None defers resolution to first use, so utils.storage_dir is
        # only called when the store is actually needed (avoids creating
        # ./storage/memory/ at import time).
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ---- lifecycle ----------------------------------------------------------

    def _ensure_init(self) -> None:
        if self._conn is not None:
            return
        if self._db_path is None:
            self._db_path = os.path.join(
                utils.storage_dir("memory", create=True), "memory.db"
            )
        # check_same_thread=False because the task pipeline writes from worker threads.
        # isolation_level=None puts us in autocommit; we use explicit transactions only when batching.
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(_SCHEMA_V1)
        cur.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        # Future: when bumping schema, branch on existing version here.

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- material memory ----------------------------------------------------

    def record_material_use(
        self,
        url: str,
        subject: str = "",
        search_terms: Optional[List[str]] = None,
        ranker_score: Optional[float] = None,
        task_id: Optional[str] = None,
        in_final: bool = False,
    ) -> None:
        """
        Record one use of a material URL. Idempotent on URL: subject/terms/ranker_score
        are overwritten with the latest values, used_count and in_final_count increment.
        """
        with self._lock:
            self._ensure_init()
            terms_json = _serialize_terms(search_terms)
            now = int(time.time())
            in_final_inc = 1 if in_final else 0
            self._conn.execute(
                """
                INSERT INTO material_memory
                  (url, subject, search_terms, ranker_score, last_used_at, task_id,
                   used_count, in_final_count)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(url) DO UPDATE SET
                  subject       = excluded.subject,
                  search_terms  = excluded.search_terms,
                  ranker_score  = COALESCE(excluded.ranker_score, material_memory.ranker_score),
                  last_used_at  = excluded.last_used_at,
                  task_id       = excluded.task_id,
                  used_count    = material_memory.used_count + 1,
                  in_final_count= material_memory.in_final_count + excluded.in_final_count
                """,
                (url, subject, terms_json, ranker_score, now, task_id, in_final_inc),
            )

    def get_material_history(self, url: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ensure_init()
            row = self._conn.execute(
                "SELECT * FROM material_memory WHERE url = ?", (url,)
            ).fetchone()
            return _row_to_material(row) if row else None

    def search_materials_by_subject(
        self, subject: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_init()
            rows = self._conn.execute(
                """
                SELECT * FROM material_memory
                WHERE subject = ?
                ORDER BY in_final_count DESC, ranker_score DESC, last_used_at DESC
                LIMIT ?
                """,
                (subject, limit),
            ).fetchall()
            return [_row_to_material(r) for r in rows]

    def get_material_histories(self, urls: List[str]) -> Dict[str, int]:
        """
        Batch lookup of last_used_at timestamps for the given URL list.
        Returns {url: last_used_at_unix_ts} for URLs present in the DB.
        Missing URLs are simply absent from the result dict.
        Single SQL round-trip, single lock acquisition.
        """
        if not urls:
            return {}
        with self._lock:
            self._ensure_init()
            placeholders = ",".join("?" * len(urls))
            rows = self._conn.execute(
                f"SELECT url, last_used_at FROM material_memory WHERE url IN ({placeholders})",
                urls,
            ).fetchall()
            return {row["url"]: row["last_used_at"] for row in rows}

    def list_materials(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_init()
            rows = self._conn.execute(
                "SELECT * FROM material_memory ORDER BY last_used_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [_row_to_material(r) for r in rows]

    def delete_material(self, url: str) -> bool:
        with self._lock:
            self._ensure_init()
            cur = self._conn.execute(
                "DELETE FROM material_memory WHERE url = ?", (url,)
            )
            return cur.rowcount > 0

    # ---- script samples -----------------------------------------------------

    def record_script_sample(
        self,
        task_id: Optional[str],
        subject: str,
        script: str,
        status: str,
        edited_to: Optional[str] = None,
    ) -> int:
        """status ∈ {'accepted', 'rejected', 'edited'}. Returns the new row id."""
        if status not in ("accepted", "rejected", "edited"):
            raise ValueError(
                f"status must be one of accepted/rejected/edited, got {status!r}"
            )
        with self._lock:
            self._ensure_init()
            cur = self._conn.execute(
                """
                INSERT INTO script_samples
                  (task_id, subject, script, status, edited_to, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, subject, script, status, edited_to, int(time.time())),
            )
            return cur.lastrowid

    def list_script_samples(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_init()
            if status:
                rows = self._conn.execute(
                    """
                    SELECT * FROM script_samples WHERE status = ?
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                    """,
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM script_samples ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_script_sample(self, sample_id: int) -> bool:
        with self._lock:
            self._ensure_init()
            cur = self._conn.execute(
                "DELETE FROM script_samples WHERE id = ?", (sample_id,)
            )
            return cur.rowcount > 0

    # ---- failure records ----------------------------------------------------

    def record_failure(
        self,
        task_id: Optional[str],
        stage: str,
        error: Optional[str] = None,
        provider: Optional[str] = None,
        recovery: Optional[str] = None,
    ) -> int:
        with self._lock:
            self._ensure_init()
            cur = self._conn.execute(
                """
                INSERT INTO failure_records
                  (task_id, stage, error, provider, recovery, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, stage, error, provider, recovery, int(time.time())),
            )
            return cur.lastrowid

    def list_failures(
        self,
        limit: int = 50,
        offset: int = 0,
        stage: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure_init()
            if stage:
                rows = self._conn.execute(
                    """
                    SELECT * FROM failure_records WHERE stage = ?
                    ORDER BY created_at DESC LIMIT ? OFFSET ?
                    """,
                    (stage, limit, offset),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM failure_records ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def delete_failure(self, failure_id: int) -> bool:
        with self._lock:
            self._ensure_init()
            cur = self._conn.execute(
                "DELETE FROM failure_records WHERE id = ?", (failure_id,)
            )
            return cur.rowcount > 0

    # ---- admin --------------------------------------------------------------

    def clear_all(self) -> None:
        """Delete all rows from all three memory tables. _meta is preserved."""
        with self._lock:
            self._ensure_init()
            self._conn.execute("DELETE FROM material_memory")
            self._conn.execute("DELETE FROM script_samples")
            self._conn.execute("DELETE FROM failure_records")

    def stats(self) -> Dict[str, int]:
        with self._lock:
            self._ensure_init()
            return {
                "materials": self._conn.execute(
                    "SELECT COUNT(*) FROM material_memory"
                ).fetchone()[0],
                "scripts": self._conn.execute(
                    "SELECT COUNT(*) FROM script_samples"
                ).fetchone()[0],
                "failures": self._conn.execute(
                    "SELECT COUNT(*) FROM failure_records"
                ).fetchone()[0],
            }


# Module-level singleton. Connection is lazy, so importing this module is cheap.
store = MemoryStore()
