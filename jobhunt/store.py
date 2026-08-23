"""SQLite persistence: dedupe across runs, track what's new, remember status."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import Job
from .scoring import Verdict
from .util import now_iso

USER_STATUSES = ("new", "seen", "applied", "ignored")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT NOT NULL,
    external_id TEXT,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    department  TEXT,
    posted_at   TEXT,
    description TEXT,
    remote      INTEGER DEFAULT 0,
    score       REAL DEFAULT 0,
    matched     TEXT DEFAULT '[]',
    rejected    TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    closed_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class UpsertStats:
    new: int = 0
    updated: int = 0
    closed: int = 0


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def upsert(self, scored: list[tuple[Job, Verdict]], close_missing_for: str | None = None) -> UpsertStats:
        """Insert or refresh postings.

        `close_missing_for` should be a company name whose fetch succeeded;
        any of its previously-known jobs absent from this batch are stamped
        closed so stale listings stop showing up in digests.
        """
        stats = UpsertStats()
        stamp = now_iso()
        seen_uids: list[str] = []

        for job, verdict in scored:
            uid = job.uid
            seen_uids.append(uid)
            exists = self.conn.execute(
                "SELECT 1 FROM jobs WHERE uid = ?", (uid,)
            ).fetchone()

            self.conn.execute(
                """
                INSERT INTO jobs (
                    uid, source, company, external_id, title, url, location,
                    department, posted_at, description, remote, score, matched,
                    rejected, first_seen, last_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                    title       = excluded.title,
                    url         = excluded.url,
                    location    = excluded.location,
                    department  = excluded.department,
                    posted_at   = COALESCE(excluded.posted_at, jobs.posted_at),
                    description = excluded.description,
                    remote      = excluded.remote,
                    score       = excluded.score,
                    matched     = excluded.matched,
                    rejected    = excluded.rejected,
                    last_seen   = excluded.last_seen,
                    closed_at   = NULL
                """,
                (
                    uid,
                    job.source,
                    job.company,
                    job.external_id,
                    job.title,
                    job.url,
                    job.location,
                    job.department,
                    job.posted_at,
                    job.description,
                    1 if job.remote else 0,
                    verdict.score,
                    json.dumps(verdict.matched),
                    verdict.rejected,
                    stamp,
                    stamp,
                ),
            )
            if exists:
                stats.updated += 1
            else:
                stats.new += 1

        if close_missing_for:
            placeholders = ",".join("?" for _ in seen_uids) or "''"
            cursor = self.conn.execute(
                f"""
                UPDATE jobs SET closed_at = ?
                WHERE company = ?
                  AND closed_at IS NULL
                  AND uid NOT IN ({placeholders})
                """,
                [stamp, close_missing_for, *seen_uids],
            )
            stats.closed = cursor.rowcount or 0

        self.conn.commit()
        return stats

    def update_verdict(self, uid: str, verdict: Verdict) -> bool:
        """Rewrite only the scoring columns.

        Deliberately separate from `upsert`, which treats a job's presence as
        proof it is still open. Rescoring must not resurrect a closed role.
        """
        cursor = self.conn.execute(
            "UPDATE jobs SET score = ?, matched = ?, rejected = ? WHERE uid = ?",
            (verdict.score, json.dumps(verdict.matched), verdict.rejected, uid),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def all_jobs(self) -> list[sqlite3.Row]:
        """Every stored row, closed and rejected ones included."""
        return self.conn.execute("SELECT * FROM jobs").fetchall()

    def count_for_company(self, company: str) -> int:
        """Exact-name count. Deliberately not the LIKE match `query` uses, so
        'Royal Society' cannot be confused with 'Royal Society of Chemistry'."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE company = ?", (company,)
        ).fetchone()
        return row["n"] or 0

    def delete_company(self, company: str) -> int:
        """Forget every posting stored for one company. Exact name only."""
        cursor = self.conn.execute("DELETE FROM jobs WHERE company = ?", (company,))
        self.conn.commit()
        return cursor.rowcount or 0

    def set_status(self, uid: str, status: str) -> bool:
        if status not in USER_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {USER_STATUSES}")
        cursor = self.conn.execute(
            "UPDATE jobs SET status = ? WHERE uid = ?", (status, uid)
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def set_note(self, uid: str, note: str) -> bool:
        cursor = self.conn.execute("UPDATE jobs SET note = ? WHERE uid = ?", (note, uid))
        self.conn.commit()
        return bool(cursor.rowcount)

    # -- reads ----------------------------------------------------------

    def query(
        self,
        *,
        status: str | None = None,
        company: str | None = None,
        min_score: float | None = None,
        rejected: bool = False,
        include_closed: bool = False,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        where = []
        params: list = []

        where.append("rejected IS NOT NULL" if rejected else "rejected IS NULL")
        if not include_closed:
            where.append("closed_at IS NULL")
        if status:
            where.append("status = ?")
            params.append(status)
        if company:
            where.append("company LIKE ?")
            params.append(f"%{company}%")
        if min_score is not None:
            where.append("score >= ?")
            params.append(min_score)
        if since:
            where.append("first_seen > ?")
            params.append(since)

        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score DESC, COALESCE(posted_at, first_seen) DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"

        return self.conn.execute(sql, params).fetchall()

    def resolve(self, uid_prefix: str) -> list[sqlite3.Row]:
        """Look a job up by a uid prefix, so short ids work on the CLI."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE uid LIKE ? ORDER BY score DESC",
            (f"{uid_prefix}%",),
        ).fetchall()

    def counts(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                                AS total,
                SUM(rejected IS NULL AND closed_at IS NULL)             AS matching,
                SUM(rejected IS NULL AND closed_at IS NULL
                    AND status = 'new')                                 AS unseen,
                SUM(status = 'applied')                                 AS applied,
                SUM(closed_at IS NOT NULL)                              AS closed
            FROM jobs
            """
        ).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}

    def departments(self, company: str | None = None) -> list[sqlite3.Row]:
        """Distinct departments seen, for narrowing a broad board down."""
        sql = """
            SELECT COALESCE(NULLIF(department, ''), '(none)') AS department,
                   company,
                   COUNT(*) AS total,
                   SUM(rejected IS NULL) AS matching
            FROM jobs
            WHERE closed_at IS NULL
        """
        params: list = []
        if company:
            sql += " AND company LIKE ?"
            params.append(f"%{company}%")
        sql += " GROUP BY department, company ORDER BY total DESC, department"
        return self.conn.execute(sql, params).fetchall()

    def companies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT company,
                   COUNT(*) AS total,
                   SUM(rejected IS NULL AND closed_at IS NULL) AS matching,
                   MAX(last_seen) AS last_seen
            FROM jobs GROUP BY company ORDER BY matching DESC, company
            """
        ).fetchall()

    # -- meta -----------------------------------------------------------

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()
