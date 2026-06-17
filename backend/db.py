
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from config import DATABASE_URL, JOB_TTL_HOURS

_pool: ConnectionPool | None = None



def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
        )
    return _pool


def create_job(*, filename: str, voice: str, page_count: int | None = None) -> str:
    """Insert a new job in 'queued' state and return its UUID."""
    job_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JOB_TTL_HOURS)
    with pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, state, filename, voice, page_count, expires_at)
            VALUES (%s, 'queued', %s, %s, %s, %s)
            """,
            (job_id, filename, voice, page_count, expires_at),
        )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with pool().connection() as conn:
        cur = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def update_job(job_id: str, **fields: Any) -> None:
    """Update arbitrary fields on a job row. Always bumps updated_at."""
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc)
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [job_id]
    with pool().connection() as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = %s", values)


def claim_next_job(worker_id: str, lock_seconds: int = 600) -> dict[str, Any] | None:

    locked_until = datetime.now(timezone.utc) + timedelta(seconds=lock_seconds)
    with pool().connection() as conn:
        cur = conn.execute(
            """
            WITH next_job AS (
                SELECT id FROM jobs
                WHERE state = 'queued'
                  AND (locked_until IS NULL OR locked_until < NOW())
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs
            SET worker_id = %s,
                locked_until = %s,
                updated_at = NOW()
            FROM next_job
            WHERE jobs.id = next_job.id
            RETURNING jobs.*
            """,
            (worker_id, locked_until),
        )
        return cur.fetchone()


def fail_job(job_id: str, error_code: str, error_message: str) -> None:
    update_job(
        job_id,
        state="failed",
        error_code=error_code,
        error_message=error_message,
        locked_until=None,
    )

def queue_position(job_id: str) -> int | None:

    with pool().connection() as conn:
        cur = conn.execute(
            """
            SELECT
                j.state AS this_state,
                (
                    SELECT COUNT(*) FROM jobs other
                    WHERE other.created_at < j.created_at
                      AND other.state NOT IN ('done', 'failed')
                ) AS ahead
            FROM jobs j
            WHERE j.id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if row["this_state"] != "queued":
            return None
        return int(row["ahead"])