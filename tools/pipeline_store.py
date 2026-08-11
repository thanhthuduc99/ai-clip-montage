"""Durable state for the multi-user travel-content pipeline.

The web server and the render worker are separate processes in production, so
all mutable queue/upload/content state lives in one SQLite database.  Every
method opens its own connection; SQLite WAL + short transactions provide safe
cross-thread and cross-process access without a long-lived global connection.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Iterable


TERMINAL_JOB_STATES = {"done", "failed", "cancelled", "unknown"}
ACTIVE_JOB_STATES = {"uploading", "queued", "running"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
SAFE_FIELD_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,200}$")


class PipelineStoreError(RuntimeError):
    """Base error exposed to HTTP handlers as a safe client message."""


class QueueLimitError(PipelineStoreError):
    """The owner already has too many active jobs."""


class UploadValidationError(PipelineStoreError):
    """An upload violates size/type/offset constraints."""


def _now() -> float:
    return time.time()


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class PipelineStore:
    def __init__(self, db_path: str | Path, upload_root: str | Path | None = None):
        self.db_path = Path(db_path).resolve()
        self.upload_root = Path(
            upload_root or (self.db_path.parent.parent / "output" / "temp_uploads")
        ).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._upload_locks_guard = threading.Lock()
        self._upload_locks: dict[str, threading.Lock] = {}
        self._init_schema()

    def _upload_file_lock(self, file_id: str) -> threading.Lock:
        with self._upload_locks_guard:
            return self._upload_locks.setdefault(file_id, threading.Lock())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=15,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextlib.contextmanager
    def _session(self):
        """One connection per operation, always closed.

        `with conn:` only commits or rolls back — it does NOT close. Relying on
        it leaked a connection (and its -wal handle) per call; the worker polls
        the queue several times a second, so it hit the 1024-descriptor limit
        after ~509 calls and then failed every query with "unable to open
        database file" until restarted. See the 2026-08-02 outage.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._schema_lock:
            with self._session() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT NOT NULL DEFAULT '',
                        progress INTEGER NOT NULL DEFAULT 0,
                        priority INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 2,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        heartbeat_at REAL,
                        lease_owner TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        idempotency_key TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_queue
                        ON jobs(status, priority DESC, created_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_owner
                        ON jobs(owner, status, created_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
                        ON jobs(idempotency_key)
                        WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';

                    CREATE TABLE IF NOT EXISTS upload_sessions (
                        id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        total_size INTEGER NOT NULL,
                        job_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_upload_sessions_owner
                        ON upload_sessions(owner, status, updated_at);

                    CREATE TABLE IF NOT EXISTS upload_files (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL,
                        field_name TEXT NOT NULL,
                        original_name TEXT NOT NULL,
                        content_type TEXT NOT NULL DEFAULT '',
                        expected_size INTEGER NOT NULL,
                        received_size INTEGER NOT NULL DEFAULT 0,
                        relative_path TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(session_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS idx_upload_files_session
                        ON upload_files(session_id, ordinal);

                    CREATE TABLE IF NOT EXISTS resources (
                        kind TEXT NOT NULL,
                        id TEXT NOT NULL,
                        owner TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        event_time REAL NOT NULL DEFAULT 0,
                        data_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(kind, id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_resources_owner
                        ON resources(kind, owner, event_time DESC);
                    CREATE INDEX IF NOT EXISTS idx_resources_status
                        ON resources(kind, status, event_time DESC);
                    """
                )

    # ── General metadata ──────────────────────────────────────────────────

    def get_meta(self, key: str, default: str = "") -> str:
        with self._session() as conn:
            row = conn.execute(
                "SELECT value FROM pipeline_meta WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def health_snapshot(self) -> dict:
        """Run cheap readiness checks without returning private job payloads."""
        with self._session() as conn:
            quick_check = str(
                conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            ).lower()
            counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                ).fetchall()
            }
        return {
            "database_ok": quick_check == "ok",
            "queued_jobs": counts.get("queued", 0),
            "running_jobs": counts.get("running", 0),
            "unknown_jobs": counts.get("unknown", 0),
            "failed_jobs": counts.get("failed", 0),
        }

    # ── Durable jobs ──────────────────────────────────────────────────────

    @staticmethod
    def _job_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["payload"] = _json_load(out.pop("payload_json", ""), {})
        out["result"] = _json_load(out.pop("result_json", ""), {})
        out["cancel_requested"] = bool(out.get("cancel_requested"))
        return out

    def create_job(
        self,
        *,
        kind: str,
        owner: str,
        payload: dict,
        priority: int = 0,
        max_attempts: int = 2,
        idempotency_key: str = "",
        active_limit: int | None = None,
    ) -> tuple[dict, bool]:
        """Create one queued job.

        Returns ``(job, created)``.  A matching idempotency key returns the
        existing job so repeated clicks/tabs cannot duplicate side effects.
        """
        owner = (owner or "").strip()
        kind = (kind or "").strip()
        if not owner or not kind:
            raise PipelineStoreError("Thiếu loại job hoặc tài khoản.")
        idem = (idempotency_key or "").strip() or None
        now = _now()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if idem:
                    existing = conn.execute(
                        "SELECT * FROM jobs WHERE idempotency_key=?", (idem,)
                    ).fetchone()
                    if existing:
                        conn.commit()
                        return self._job_dict(existing), False
                if active_limit is not None:
                    count = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM jobs
                        WHERE owner=? AND status IN ('uploading','queued','running')
                        """,
                        (owner,),
                    ).fetchone()["n"]
                    if int(count) >= int(active_limit):
                        raise QueueLimitError(
                            f"Tài khoản đang có {count} job hoạt động; "
                            f"giới hạn là {active_limit}."
                        )
                job_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO jobs(
                        id, kind, owner, status, payload_json, result_json,
                        error, progress, priority, attempts, max_attempts,
                        created_at, updated_at, idempotency_key
                    ) VALUES(?, ?, ?, 'queued', ?, '{}', '', 0, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        kind,
                        owner,
                        _json_dump(payload or {}),
                        int(priority),
                        max(1, int(max_attempts)),
                        now,
                        now,
                        idem,
                    ),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                conn.commit()
                return self._job_dict(row), True
            except Exception:
                conn.rollback()
                raise

    def get_job(self, job_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row)

    def list_jobs(
        self,
        *,
        owner: str | None = None,
        statuses: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        where: list[str] = []
        args: list[object] = []
        if owner is not None:
            where.append("owner=?")
            args.append(owner)
        states = [str(x) for x in (statuses or []) if str(x)]
        if states:
            where.append("status IN (" + ",".join("?" for _ in states) + ")")
            args.extend(states)
        job_kinds = [str(x) for x in (kinds or []) if str(x)]
        if job_kinds:
            where.append("kind IN (" + ",".join("?" for _ in job_kinds) + ")")
            args.extend(job_kinds)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._session() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._job_dict(row) for row in rows]

    def queue_length(self) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status='queued'"
            ).fetchone()
        return int(row["n"])

    def queue_position(self, job_id: str) -> int:
        """Return a stable, approximate position suitable for user feedback."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT created_at, status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row or row["status"] != "queued":
                return 0
            count = conn.execute(
                """
                SELECT COUNT(*) AS n FROM jobs
                WHERE status='queued' AND created_at <= ?
                """,
                (row["created_at"],),
            ).fetchone()["n"]
        return int(count)

    def claim_next(
        self,
        worker_id: str,
        *,
        kinds: Iterable[str] | None = None,
    ) -> dict | None:
        """Lease the next job using owner-fair scheduling."""
        accepted = [str(x) for x in (kinds or []) if str(x)]
        kind_sql = ""
        args: list[object] = []
        if accepted:
            kind_sql = " AND j.kind IN (" + ",".join("?" for _ in accepted) + ")"
            args.extend(accepted)
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"""
                    SELECT j.*
                    FROM jobs AS j
                    WHERE j.status='queued'
                      AND j.cancel_requested=0
                      {kind_sql}
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs AS running
                          WHERE running.owner=j.owner AND running.status='running'
                      )
                    ORDER BY
                      j.priority DESC,
                      COALESCE((
                          SELECT MAX(COALESCE(history.started_at, 0))
                          FROM jobs AS history
                          WHERE history.owner=j.owner
                      ), 0) ASC,
                      j.created_at ASC
                    LIMIT 1
                    """,
                    args,
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                now = _now()
                changed = conn.execute(
                    """
                    UPDATE jobs
                    SET status='running', attempts=attempts+1,
                        started_at=COALESCE(started_at, ?),
                        heartbeat_at=?, updated_at=?, lease_owner=?, progress=5
                    WHERE id=? AND status='queued'
                    """,
                    (now, now, now, worker_id, row["id"]),
                ).rowcount
                if not changed:
                    conn.rollback()
                    return None
                claimed = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
                conn.commit()
                return self._job_dict(claimed)
            except Exception:
                conn.rollback()
                raise

    def heartbeat(self, job_id: str, worker_id: str, progress: int | None = None) -> bool:
        now = _now()
        fields = "heartbeat_at=?, updated_at=?"
        args: list[object] = [now, now]
        if progress is not None:
            fields += ", progress=?"
            args.append(max(0, min(99, int(progress))))
        args.extend([job_id, worker_id])
        with self._session() as conn:
            changed = conn.execute(
                f"""
                UPDATE jobs SET {fields}
                WHERE id=? AND lease_owner=? AND status='running'
                """,
                args,
            ).rowcount
        return bool(changed)

    def complete_job(self, job_id: str, worker_id: str, result: dict) -> bool:
        now = _now()
        with self._session() as conn:
            changed = conn.execute(
                """
                UPDATE jobs
                SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'done' END,
                    result_json=?, error='', progress=100, finished_at=?,
                    heartbeat_at=?, updated_at=?, lease_owner=NULL
                WHERE id=? AND lease_owner=? AND status='running'
                """,
                (_json_dump(result or {}), now, now, now, job_id, worker_id),
            ).rowcount
        return bool(changed)

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> str:
        """Fail/requeue a leased job and return the resulting status."""
        now = _now()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id=? AND lease_owner=? AND status='running'",
                    (job_id, worker_id),
                ).fetchone()
                if not row:
                    conn.commit()
                    return ""
                if row["cancel_requested"]:
                    status = "cancelled"
                elif uncertain:
                    status = "unknown"
                elif retryable and int(row["attempts"]) < int(row["max_attempts"]):
                    status = "queued"
                else:
                    status = "failed"
                finished = now if status in TERMINAL_JOB_STATES else None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status=?, error=?, progress=CASE WHEN ?='queued' THEN 0 ELSE progress END,
                        finished_at=?, heartbeat_at=?, updated_at=?, lease_owner=NULL
                    WHERE id=?
                    """,
                    (status, str(error or "")[:4000], status, finished, now, now, job_id),
                )
                conn.commit()
                return status
            except Exception:
                conn.rollback()
                raise

    def cancel_job(self, job_id: str, actor: str, *, is_admin: bool = False) -> dict | None:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or (not is_admin and row["owner"] != actor):
                    conn.commit()
                    return None
                now = _now()
                if row["status"] in ("uploading", "queued"):
                    conn.execute(
                        """
                        UPDATE jobs SET status='cancelled', cancel_requested=1,
                            finished_at=?, updated_at=? WHERE id=?
                        """,
                        (now, now, job_id),
                    )
                elif row["status"] == "running":
                    conn.execute(
                        "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                        (now, job_id),
                    )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                conn.commit()
                return self._job_dict(updated)
            except Exception:
                conn.rollback()
                raise

    def retry_job(
        self,
        job_id: str,
        actor: str,
        *,
        is_admin: bool = False,
        active_limit: int | None = None,
    ) -> dict | None:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if (
                    not row
                    or (not is_admin and row["owner"] != actor)
                    or row["status"] not in ("failed", "unknown")
                ):
                    conn.commit()
                    return None
                if active_limit is not None:
                    active = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM jobs
                        WHERE owner=? AND id<>? AND status IN ('uploading','queued','running')
                        """,
                        (row["owner"], job_id),
                    ).fetchone()["n"]
                    if int(active) >= int(active_limit):
                        raise QueueLimitError(
                            f"Tài khoản đang có {active} job hoạt động; "
                            f"giới hạn là {active_limit}."
                        )
                now = _now()
                conn.execute(
                    """
                    UPDATE jobs
                    SET status='queued', error='', progress=0, finished_at=NULL,
                        heartbeat_at=NULL, lease_owner=NULL, cancel_requested=0,
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, job_id),
                )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                conn.commit()
                return self._job_dict(updated)
            except Exception:
                conn.rollback()
                raise

    def resolve_external_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: str = "",
    ) -> dict | None:
        """Apply the authoritative provider result to a publish job."""
        if status not in {"done", "failed"}:
            raise ValueError("Unknown jobs can only resolve to done or failed.")
        now = _now()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE id=? AND kind LIKE 'publish_%'
                      AND status IN ('unknown','done','failed')
                    """,
                    (job_id,),
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status=?, result_json=?, error=?, progress=?,
                        finished_at=?, heartbeat_at=?, updated_at=?,
                        lease_owner=NULL
                    WHERE id=? AND kind LIKE 'publish_%'
                    """,
                    (
                        status,
                        _json_dump(result or {}),
                        str(error or "")[:4000],
                        100 if status == "done" else int(row["progress"] or 0),
                        now,
                        now,
                        now,
                        job_id,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                conn.commit()
                return self._job_dict(updated)
            except Exception:
                conn.rollback()
                raise

    def recover_stale_jobs(self, stale_after_seconds: int = 120) -> dict:
        cutoff = _now() - max(30, int(stale_after_seconds))
        recovered = failed = 0
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status='running' AND COALESCE(heartbeat_at, updated_at) < ?
                    """,
                    (cutoff,),
                ).fetchall()
                now = _now()
                for row in rows:
                    if row["cancel_requested"]:
                        status = "cancelled"
                    elif str(row["kind"]).startswith("publish_"):
                        payload = _json_load(row["payload_json"], {})
                        # Current Zernio guarantees x-request-id deduplication
                        # for roughly five minutes, including in-flight calls.
                        # One prompt recovery is safe inside that window.
                        safe_retry = (
                            bool(payload.get("provider_request_id"))
                            and int(row["attempts"]) < int(row["max_attempts"])
                            and now - float(row["updated_at"] or 0) < 240
                        )
                        if safe_retry:
                            status = "queued"
                            recovered += 1
                        else:
                            status = "unknown"
                            failed += 1
                    elif int(row["attempts"]) < int(row["max_attempts"]):
                        status = "queued"
                        recovered += 1
                    else:
                        status = "failed"
                        failed += 1
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status=?, lease_owner=NULL, heartbeat_at=NULL,
                            error=?, updated_at=?, finished_at=?
                        WHERE id=?
                        """,
                        (
                            status,
                            (
                                "Worker bị gián đoạn; job đã được khôi phục."
                                if status == "queued"
                                else (
                                    "Không xác định nhà cung cấp đã nhận bài hay chưa; "
                                    "cần kiểm tra trước khi thử lại."
                                    if status == "unknown"
                                    else "Worker bị gián đoạn quá số lần cho phép."
                                )
                            ),
                            now,
                            now if status in TERMINAL_JOB_STATES else None,
                            row["id"],
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"recovered": recovered, "failed": failed}

    # ── Streaming upload sessions ─────────────────────────────────────────

    @staticmethod
    def _upload_file_dict(row: sqlite3.Row) -> dict:
        return dict(row)

    def _upload_dir(self, session_id: str) -> Path:
        target = (self.upload_root / session_id).resolve()
        if target.parent != self.upload_root:
            raise UploadValidationError("Upload session không hợp lệ.")
        return target

    def create_upload_session(
        self,
        *,
        owner: str,
        kind: str,
        files: list[dict],
        max_file_bytes: int,
        max_job_bytes: int,
        max_active_sessions: int,
        reserve_free_bytes: int,
        payload: dict | None = None,
        idempotency_key: str = "",
        active_job_limit: int | None = None,
        global_active_job_limit: int | None = None,
        max_attempts: int = 2,
    ) -> dict:
        if kind not in {"source_clips"}:
            raise UploadValidationError("Loại upload không hợp lệ.")
        if not files or len(files) > 100:
            raise UploadValidationError("Số lượng file phải từ 1 đến 100.")
        normalized: list[dict] = []
        total = 0
        for index, item in enumerate(files):
            name = Path(str(item.get("name") or "")).name
            field = str(item.get("field") or "")
            content_type = str(item.get("type") or "")[:120]
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            ext = Path(name).suffix.lower()
            if not SAFE_FIELD_RE.fullmatch(field):
                raise UploadValidationError(f"Tên trường file không hợp lệ: {field[:40]}")
            if ext not in ALLOWED_VIDEO_EXTENSIONS:
                raise UploadValidationError(
                    f"File {name or index + 1} không thuộc MP4/MOV/M4V/WebM."
                )
            if content_type and not (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
            ):
                raise UploadValidationError(f"Kiểu file không hợp lệ: {content_type}")
            if size <= 0 or size > int(max_file_bytes):
                raise UploadValidationError(
                    f"File {name or index + 1} vượt giới hạn "
                    f"{int(max_file_bytes) // (1024 * 1024)} MB."
                )
            total += size
            normalized.append(
                {
                    "ordinal": index,
                    "field": field,
                    "name": name,
                    "type": content_type,
                    "size": size,
                    "ext": ext,
                }
            )
        if total > int(max_job_bytes):
            raise UploadValidationError(
                f"Tổng upload vượt giới hạn {int(max_job_bytes) // (1024 * 1024)} MB."
            )
        free = shutil.disk_usage(self.upload_root).free
        if free - total < int(reserve_free_bytes):
            raise UploadValidationError("Server không đủ dung lượng trống cho upload này.")

        idem = str(idempotency_key or "").strip()
        reserves_job = payload is not None or bool(idem)
        if reserves_job and (not idem or not IDEMPOTENCY_RE.fullmatch(idem)):
            raise UploadValidationError("Mã yêu cầu upload không hợp lệ.")

        session_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex if reserves_job else ""
        now = _now()
        directory = self._upload_dir(session_id)
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if idem:
                    existing_job = conn.execute(
                        "SELECT * FROM jobs WHERE idempotency_key=?", (idem,)
                    ).fetchone()
                    if existing_job:
                        existing_session = conn.execute(
                            "SELECT * FROM upload_sessions WHERE job_id=?",
                            (existing_job["id"],),
                        ).fetchone()
                        if not existing_session:
                            raise UploadValidationError(
                                "Job upload cũ không còn phiên file tương ứng."
                            )
                        existing_files = conn.execute(
                            "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                            (existing_session["id"],),
                        ).fetchall()
                        conn.commit()
                        return {
                            "id": existing_session["id"],
                            "kind": existing_session["kind"],
                            "status": existing_session["status"],
                            "total_size": int(existing_session["total_size"]),
                            "job_id": existing_job["id"],
                            "job_status": existing_job["status"],
                            "created": False,
                            "files": [
                                {
                                    "id": row["id"],
                                    "field": row["field_name"],
                                    "name": row["original_name"],
                                    "size": int(row["expected_size"]),
                                    "received": int(row["received_size"]),
                                }
                                for row in existing_files
                            ],
                        }
                active = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM upload_sessions
                    WHERE owner=? AND status IN ('uploading','ready')
                    """,
                    (owner,),
                ).fetchone()["n"]
                if int(active) >= int(max_active_sessions):
                    error_type = QueueLimitError if reserves_job else UploadValidationError
                    raise error_type(
                        f"Tài khoản đang có {active} phiên upload; "
                        f"giới hạn là {max_active_sessions}."
                    )
                if reserves_job and active_job_limit is not None:
                    active_jobs = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM jobs
                        WHERE owner=? AND status IN ('uploading','queued','running')
                        """,
                        (owner,),
                    ).fetchone()["n"]
                    if int(active_jobs) >= int(active_job_limit):
                        raise QueueLimitError(
                            f"Tài khoản đang có {active_jobs} job hoạt động; "
                            f"giới hạn là {active_job_limit}."
                        )
                if reserves_job and global_active_job_limit is not None:
                    global_active = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM jobs
                        WHERE status IN ('uploading','queued','running')
                        """
                    ).fetchone()["n"]
                    if int(global_active) >= int(global_active_job_limit):
                        raise QueueLimitError(
                            "Hàng đợi toàn hệ thống đang đầy; vui lòng thử lại sau."
                        )
                conn.execute(
                    """
                    INSERT INTO upload_sessions(
                        id, owner, kind, status, total_size, job_id, created_at, updated_at
                    ) VALUES(?, ?, ?, 'uploading', ?, ?, ?, ?)
                    """,
                    (session_id, owner, kind, total, job_id or None, now, now),
                )
                response_files = []
                for item in normalized:
                    file_id = uuid.uuid4().hex
                    relative = f"{session_id}/{file_id}{item['ext']}"
                    conn.execute(
                        """
                        INSERT INTO upload_files(
                            id, session_id, ordinal, field_name, original_name,
                            content_type, expected_size, received_size,
                            relative_path, status, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending', ?, ?)
                        """,
                        (
                            file_id,
                            session_id,
                            item["ordinal"],
                            item["field"],
                            item["name"],
                            item["type"],
                            item["size"],
                            relative,
                            now,
                            now,
                        ),
                    )
                    response_files.append(
                        {
                            "id": file_id,
                            "field": item["field"],
                            "name": item["name"],
                            "size": item["size"],
                            "received": 0,
                        }
                    )
                if reserves_job:
                    job_payload = dict(payload or {})
                    job_payload["upload_session_id"] = session_id
                    conn.execute(
                        """
                        INSERT INTO jobs(
                            id, kind, owner, status, payload_json, result_json,
                            error, progress, priority, attempts, max_attempts,
                            created_at, updated_at, idempotency_key
                        ) VALUES(?, ?, ?, 'uploading', ?, '{}', '', 0, 0, 0, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            kind,
                            owner,
                            _json_dump(job_payload),
                            max(1, int(max_attempts)),
                            now,
                            now,
                            idem,
                        ),
                    )
                directory.mkdir(parents=True, exist_ok=False)
                conn.commit()
                return {
                    "id": session_id,
                    "kind": kind,
                    "status": "uploading",
                    "total_size": total,
                    "job_id": job_id,
                    "job_status": "uploading" if job_id else "",
                    "created": True,
                    "files": response_files,
                }
            except Exception:
                conn.rollback()
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def get_upload_session(
        self, session_id: str, actor: str, *, is_admin: bool = False
    ) -> dict | None:
        with self._session() as conn:
            session = conn.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not session or (not is_admin and session["owner"] != actor):
                return None
            files = conn.execute(
                "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        out = dict(session)
        out["files"] = [self._upload_file_dict(row) for row in files]
        return out

    def append_upload_chunk(
        self,
        *,
        session_id: str,
        file_id: str,
        owner: str,
        offset: int,
        length: int,
        source: BinaryIO,
        max_chunk_bytes: int,
    ) -> dict:
        if length <= 0 or length > int(max_chunk_bytes):
            raise UploadValidationError("Chunk upload có dung lượng không hợp lệ.")
        # Serialize only chunks of the same file. Reading a slow request body
        # must not hold SQLite's global write lock and block five other users.
        with self._upload_file_lock(file_id):
            path: Path | None = None
            original_size = 0
            try:
                with self._session() as conn:
                    row = conn.execute(
                        """
                        SELECT f.*, s.owner, s.status AS session_status
                        FROM upload_files AS f
                        JOIN upload_sessions AS s ON s.id=f.session_id
                        WHERE f.id=? AND f.session_id=?
                        """,
                        (file_id, session_id),
                    ).fetchone()
                if not row or row["owner"] != owner:
                    raise UploadValidationError("Không tìm thấy file upload.")
                if row["session_status"] != "uploading":
                    raise UploadValidationError("Phiên upload không còn nhận dữ liệu.")
                received = int(row["received_size"])
                expected = int(row["expected_size"])
                if int(offset) != received:
                    raise UploadValidationError(
                        f"Offset không khớp; server đang có {received} byte."
                    )
                if received + int(length) > expected:
                    raise UploadValidationError("Chunk vượt quá kích thước file đã khai báo.")
                path = (self.upload_root / row["relative_path"]).resolve()
                if path.parent != self._upload_dir(session_id):
                    raise UploadValidationError("Đường dẫn upload không hợp lệ.")
                path.parent.mkdir(parents=True, exist_ok=True)
                original_size = path.stat().st_size if path.exists() else 0
                if original_size != received:
                    raise UploadValidationError("Trạng thái file upload không đồng nhất.")

                remaining = int(length)
                with path.open("ab") as handle:
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise UploadValidationError("Kết nối upload bị gián đoạn.")
                        handle.write(chunk)
                        remaining -= len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())

                new_size = received + int(length)
                status = "complete" if new_size == expected else "uploading"
                now = _now()
                with self._session() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(
                        """
                        SELECT f.received_size, s.owner, s.status AS session_status
                        FROM upload_files AS f
                        JOIN upload_sessions AS s ON s.id=f.session_id
                        WHERE f.id=? AND f.session_id=?
                        """,
                        (file_id, session_id),
                    ).fetchone()
                    if (
                        not current
                        or current["owner"] != owner
                        or current["session_status"] != "uploading"
                        or int(current["received_size"]) != received
                    ):
                        raise UploadValidationError(
                            "Trạng thái file upload đã thay đổi; hãy tiếp tục theo offset mới."
                        )
                    conn.execute(
                        """
                        UPDATE upload_files
                        SET received_size=?, status=?, updated_at=?
                        WHERE id=?
                        """,
                        (new_size, status, now, file_id),
                    )
                    conn.execute(
                        "UPDATE upload_sessions SET updated_at=? WHERE id=?",
                        (now, session_id),
                    )
                    conn.commit()
                return {
                    "file_id": file_id,
                    "received": new_size,
                    "expected": expected,
                    "complete": status == "complete",
                }
            except Exception:
                if path and path.exists():
                    try:
                        with path.open("r+b") as handle:
                            handle.truncate(original_size)
                    except OSError:
                        pass
                raise

    def complete_upload(self, session_id: str, owner: str) -> dict:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if not session or session["owner"] != owner:
                    raise UploadValidationError("Không tìm thấy phiên upload.")
                if session["status"] not in ("uploading", "ready"):
                    raise UploadValidationError(
                        "Phiên upload không còn ở trạng thái có thể hoàn tất."
                    )
                files = conn.execute(
                    "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                    (session_id,),
                ).fetchall()
                if not files or any(
                    int(row["received_size"]) != int(row["expected_size"])
                    for row in files
                ):
                    raise UploadValidationError("Upload chưa đủ dữ liệu.")
                now = _now()
                conn.execute(
                    """
                    UPDATE upload_sessions SET status='ready', updated_at=?
                    WHERE id=? AND status IN ('uploading','ready')
                    """,
                    (now, session_id),
                )
                conn.commit()
                return {
                    "id": session_id,
                    "status": "ready",
                    "files": [self._upload_file_dict(row) for row in files],
                }
            except Exception:
                conn.rollback()
                raise

    def queue_reserved_upload(self, session_id: str, owner: str) -> dict | None:
        """Attach validated files and atomically promote a reserved upload job."""
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if not session or session["owner"] != owner:
                    raise UploadValidationError("Không tìm thấy phiên upload.")
                if not session["job_id"]:
                    conn.commit()
                    return None
                job = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (session["job_id"],)
                ).fetchone()
                if not job:
                    raise UploadValidationError("Không tìm thấy job đã giữ chỗ.")
                if session["status"] == "consumed":
                    conn.commit()
                    return self._job_dict(job)
                if session["status"] != "ready":
                    raise UploadValidationError("Upload chưa sẵn sàng để vào hàng đợi.")
                if job["status"] != "uploading":
                    raise UploadValidationError(
                        "Job upload không còn ở trạng thái có thể xếp hàng."
                    )
                files = conn.execute(
                    "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                    (session_id,),
                ).fetchall()
                if not files or any(
                    int(row["received_size"]) != int(row["expected_size"])
                    for row in files
                ):
                    raise UploadValidationError("Upload chưa đủ dữ liệu.")
                upload_files = []
                for row in files:
                    item = dict(row)
                    resolved = (self.upload_root / item["relative_path"]).resolve()
                    if resolved.parent != self._upload_dir(session_id):
                        raise UploadValidationError("Đường dẫn upload không hợp lệ.")
                    if not resolved.is_file() or resolved.stat().st_size != int(
                        item["expected_size"]
                    ):
                        raise UploadValidationError(
                            "File upload không còn đầy đủ trên server."
                        )
                    upload_files.append(
                        {
                            "id": item["id"],
                            "field": item["field_name"],
                            "name": item["original_name"],
                            "content_type": item["content_type"],
                            "size": item["expected_size"],
                            "path": str(resolved),
                        }
                    )
                payload = _json_load(job["payload_json"], {})
                payload["upload_session_id"] = session_id
                payload["upload_files"] = upload_files
                now = _now()
                conn.execute(
                    """
                    UPDATE jobs
                    SET status='queued', payload_json=?, error='', progress=0,
                        updated_at=?
                    WHERE id=? AND status='uploading'
                    """,
                    (_json_dump(payload), now, job["id"]),
                )
                conn.execute(
                    """
                    UPDATE upload_sessions
                    SET status='consumed', updated_at=? WHERE id=?
                    """,
                    (now, session_id),
                )
                queued = conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job["id"],)
                ).fetchone()
                conn.commit()
                return self._job_dict(queued)
            except Exception:
                conn.rollback()
                raise

    def consume_upload(self, session_id: str, owner: str, job_id: str) -> dict:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if (
                    not session
                    or session["owner"] != owner
                    or session["status"] not in ("ready", "consumed")
                ):
                    raise UploadValidationError("Upload chưa sẵn sàng để tạo job.")
                if session["status"] == "consumed" and session["job_id"] != job_id:
                    raise UploadValidationError("Upload đã được dùng cho job khác.")
                now = _now()
                conn.execute(
                    """
                    UPDATE upload_sessions
                    SET status='consumed', job_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (job_id, now, session_id),
                )
                files = conn.execute(
                    "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                    (session_id,),
                ).fetchall()
                conn.commit()
                out = dict(session)
                out["status"] = "consumed"
                out["job_id"] = job_id
                out["files"] = []
                for row in files:
                    item = self._upload_file_dict(row)
                    item["path"] = str((self.upload_root / item["relative_path"]).resolve())
                    out["files"].append(item)
                return out
            except Exception:
                conn.rollback()
                raise

    def create_job_from_upload(
        self,
        *,
        session_id: str,
        owner: str,
        kind: str,
        payload: dict,
        active_limit: int,
        max_attempts: int = 2,
    ) -> tuple[dict, bool]:
        """Atomically consume a ready upload and create exactly one render job."""
        now = _now()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = conn.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if not session or session["owner"] != owner:
                    raise UploadValidationError("Không tìm thấy phiên upload.")
                if session["kind"] != kind:
                    raise UploadValidationError("Loại upload và loại job không khớp.")
                if session["status"] == "consumed" and session["job_id"]:
                    existing = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (session["job_id"],)
                    ).fetchone()
                    if existing:
                        conn.commit()
                        return self._job_dict(existing), False
                if session["status"] != "ready":
                    raise UploadValidationError("Upload chưa sẵn sàng để tạo job.")
                files = conn.execute(
                    "SELECT * FROM upload_files WHERE session_id=? ORDER BY ordinal",
                    (session_id,),
                ).fetchall()
                if not files or any(
                    int(row["received_size"]) != int(row["expected_size"])
                    for row in files
                ):
                    raise UploadValidationError("Upload chưa đủ dữ liệu.")
                active = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM jobs
                    WHERE owner=? AND status IN ('uploading','queued','running')
                    """,
                    (owner,),
                ).fetchone()["n"]
                if int(active) >= int(active_limit):
                    raise QueueLimitError(
                        f"Tài khoản đang có {active} job hoạt động; "
                        f"giới hạn là {active_limit}."
                    )
                upload_files = []
                for row in files:
                    item = dict(row)
                    resolved = (self.upload_root / item["relative_path"]).resolve()
                    if resolved.parent != self._upload_dir(session_id):
                        raise UploadValidationError("Đường dẫn upload không hợp lệ.")
                    if not resolved.is_file() or resolved.stat().st_size != int(
                        item["expected_size"]
                    ):
                        raise UploadValidationError("File upload không còn đầy đủ trên server.")
                    upload_files.append(
                        {
                            "id": item["id"],
                            "field": item["field_name"],
                            "name": item["original_name"],
                            "content_type": item["content_type"],
                            "size": item["expected_size"],
                            "path": str(resolved),
                        }
                    )
                job_payload = dict(payload or {})
                job_payload["upload_session_id"] = session_id
                job_payload["upload_files"] = upload_files
                job_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO jobs(
                        id, kind, owner, status, payload_json, result_json,
                        error, progress, priority, attempts, max_attempts,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, 'queued', ?, '{}', '', 0, 0, 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        kind,
                        owner,
                        _json_dump(job_payload),
                        max(1, int(max_attempts)),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE upload_sessions
                    SET status='consumed', job_id=?, updated_at=? WHERE id=?
                    """,
                    (job_id, now, session_id),
                )
                job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                conn.commit()
                return self._job_dict(job), True
            except Exception:
                conn.rollback()
                raise

    def cancel_upload(
        self, session_id: str, actor: str, *, is_admin: bool = False
    ) -> bool:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
                if not row or (not is_admin and row["owner"] != actor):
                    conn.commit()
                    return False
                if row["status"] == "consumed":
                    conn.commit()
                    return False
                now = _now()
                conn.execute(
                    """
                    UPDATE upload_sessions
                    SET status='cancelled', updated_at=? WHERE id=?
                    """,
                    (now, session_id),
                )
                if row["job_id"]:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status='cancelled', cancel_requested=1,
                            error='Đã hủy khi đang upload.',
                            finished_at=?, updated_at=?
                        WHERE id=? AND status='uploading'
                        """,
                        (now, now, row["job_id"]),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        shutil.rmtree(self._upload_dir(session_id), ignore_errors=True)
        return True

    def cleanup_uploads(self, max_age_seconds: int = 24 * 3600) -> int:
        cutoff = _now() - max(3600, int(max_age_seconds))
        removable: list[tuple[str, str]] = []
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT s.id, COALESCE(s.job_id, '') AS job_id
                FROM upload_sessions AS s
                LEFT JOIN jobs AS j ON j.id=s.job_id
                WHERE (
                    s.updated_at < ?
                    AND s.status IN ('uploading','ready','cancelled','finished')
                ) OR (
                    s.status='consumed'
                    AND j.status IN ('done','failed','cancelled','unknown')
                    AND COALESCE(j.finished_at, j.updated_at) < ?
                )
                """,
                (cutoff, cutoff),
            ).fetchall()
            removable = [
                (str(row["id"]), str(row["job_id"] or "")) for row in rows
            ]
            if removable:
                conn.execute("BEGIN IMMEDIATE")
                now = _now()
                conn.executemany(
                    """
                    UPDATE jobs
                    SET status='failed',
                        error='Upload hết hạn trước khi vào hàng đợi; vui lòng chọn lại file.',
                        finished_at=?, updated_at=?
                    WHERE id=? AND status='uploading'
                    """,
                    [
                        (now, now, job_id)
                        for _, job_id in removable
                        if job_id
                    ],
                )
                conn.executemany(
                    "DELETE FROM upload_sessions WHERE id=?",
                    [(session_id,) for session_id, _ in removable],
                )
                conn.commit()
        for session_id, _ in removable:
            shutil.rmtree(self._upload_dir(session_id), ignore_errors=True)
        return len(removable)

    def cleanup_job_upload(self, job_id: str) -> bool:
        with self._session() as conn:
            row = conn.execute(
                "SELECT id FROM upload_sessions WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                return False
            session_id = str(row["id"])
            conn.execute(
                """
                UPDATE upload_sessions
                SET status='finished', updated_at=? WHERE id=?
                """,
                (_now(), session_id),
            )
        shutil.rmtree(self._upload_dir(session_id), ignore_errors=True)
        return True

    # ── Transactional business resources ─────────────────────────────────

    def resource_migration_done(self, kind: str) -> bool:
        return self.get_meta(f"resource_migrated:{kind}") == "1"

    def mark_resource_migration_done(self, kind: str) -> None:
        self.set_meta(f"resource_migrated:{kind}", "1")

    def import_resources(self, kind: str, records: Iterable[dict]) -> int:
        count = 0
        now = _now()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for source in records:
                    record = dict(source or {})
                    resource_id = str(record.get("id") or "").strip()
                    if not resource_id:
                        continue
                    owner = str(record.get("user") or record.get("owner") or "")
                    status = str(record.get("status") or "pending")
                    event_time = float(record.get("time") or 0)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO resources(
                            kind, id, owner, status, event_time, data_json, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            kind,
                            resource_id,
                            owner,
                            status,
                            event_time,
                            _json_dump(record),
                            now,
                        ),
                    )
                    count += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    def list_resources(self, kind: str, owner: str | None = None) -> list[dict]:
        args: list[object] = [kind]
        sql = "SELECT data_json FROM resources WHERE kind=?"
        if owner is not None:
            sql += " AND owner=?"
            args.append(owner)
        sql += " ORDER BY event_time DESC, updated_at DESC"
        with self._session() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_json_load(row["data_json"], {}) for row in rows]

    def get_resource(self, kind: str, resource_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT data_json FROM resources WHERE kind=? AND id=?",
                (kind, resource_id),
            ).fetchone()
        return _json_load(row["data_json"], {}) if row else None

    def insert_resource(self, kind: str, record: dict) -> dict:
        item = dict(record or {})
        item.setdefault("id", uuid.uuid4().hex[:20])
        item.setdefault("status", "pending")
        item.setdefault("time", _now())
        owner = str(item.get("user") or item.get("owner") or "")
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO resources(
                    kind, id, owner, status, event_time, data_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    str(item["id"]),
                    owner,
                    str(item["status"]),
                    float(item.get("time") or 0),
                    _json_dump(item),
                    _now(),
                ),
            )
        return item

    def insert_resource_once(self, kind: str, record: dict) -> tuple[dict, bool]:
        """Insert an idempotent resource and return the original on duplicate id."""
        item = dict(record or {})
        item.setdefault("id", uuid.uuid4().hex[:20])
        item.setdefault("status", "pending")
        item.setdefault("time", _now())
        owner = str(item.get("user") or item.get("owner") or "")
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                changed = conn.execute(
                    """
                    INSERT OR IGNORE INTO resources(
                        kind, id, owner, status, event_time, data_json, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        str(item["id"]),
                        owner,
                        str(item["status"]),
                        float(item.get("time") or 0),
                        _json_dump(item),
                        _now(),
                    ),
                ).rowcount
                row = conn.execute(
                    "SELECT data_json FROM resources WHERE kind=? AND id=?",
                    (kind, str(item["id"])),
                ).fetchone()
                conn.commit()
                return _json_load(row["data_json"], {}), bool(changed)
            except Exception:
                conn.rollback()
                raise

    def replace_resource(self, kind: str, record: dict) -> bool:
        item = dict(record or {})
        resource_id = str(item.get("id") or "")
        if not resource_id:
            return False
        owner = str(item.get("user") or item.get("owner") or "")
        with self._session() as conn:
            changed = conn.execute(
                """
                UPDATE resources
                SET owner=?, status=?, event_time=?, data_json=?, updated_at=?
                WHERE kind=? AND id=?
                """,
                (
                    owner,
                    str(item.get("status") or "pending"),
                    float(item.get("time") or 0),
                    _json_dump(item),
                    _now(),
                    kind,
                    resource_id,
                ),
            ).rowcount
        return bool(changed)

    def update_resource(self, kind: str, resource_id: str, **fields) -> dict | None:
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT data_json FROM resources WHERE kind=? AND id=?",
                    (kind, resource_id),
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                item = _json_load(row["data_json"], {})
                item.update(fields)
                owner = str(item.get("user") or item.get("owner") or "")
                conn.execute(
                    """
                    UPDATE resources
                    SET owner=?, status=?, event_time=?, data_json=?, updated_at=?
                    WHERE kind=? AND id=?
                    """,
                    (
                        owner,
                        str(item.get("status") or "pending"),
                        float(item.get("time") or 0),
                        _json_dump(item),
                        _now(),
                        kind,
                        resource_id,
                    ),
                )
                conn.commit()
                return item
            except Exception:
                conn.rollback()
                raise

    def delete_resource(self, kind: str, resource_id: str) -> bool:
        with self._session() as conn:
            changed = conn.execute(
                "DELETE FROM resources WHERE kind=? AND id=?",
                (kind, resource_id),
            ).rowcount
        return bool(changed)


_STORE: PipelineStore | None = None
_STORE_LOCK = threading.Lock()


def get_pipeline_store() -> PipelineStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            root = Path(__file__).resolve().parent.parent
            db_path = Path(os.getenv("PIPELINE_DB_PATH") or (root / "data" / "pipeline.sqlite3"))
            upload_root = Path(os.getenv("UPLOAD_TEMP_DIR") or (root / "output" / "temp_uploads"))
            _STORE = PipelineStore(db_path, upload_root)
        return _STORE


def reset_pipeline_store_for_tests() -> None:
    """Drop only the module singleton; test-owned database files remain explicit."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
