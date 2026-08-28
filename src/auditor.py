"""SQLite audit log for the downstream agent.

Single table: action_log. Every action the agent considers (executed,
awaiting_greenlight, rejected, halted) is recorded. The action_id is a
stable hash so webhook replays are idempotent.

DB path comes from src.config.get_db_path() — /data/agent_audit.db on Fly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from src.config import get_db_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS action_log (
    action_id          TEXT PRIMARY KEY,
    video_id           TEXT NOT NULL,
    goal_id            TEXT NOT NULL,
    atom_ids           TEXT NOT NULL,
    stage2_relevance   INTEGER NOT NULL,
    raw_tier            TEXT NOT NULL,
    final_tier          TEXT NOT NULL,
    status              TEXT NOT NULL,
    realm               TEXT NOT NULL,
    action_description  TEXT NOT NULL,
    effort_hours        INTEGER NOT NULL,
    reversibility       TEXT NOT NULL,
    external_surface    INTEGER NOT NULL,
    dependencies        TEXT NOT NULL,
    artifacts           TEXT,
    rejection_reason    TEXT,
    policy_note         TEXT,
    target_repo         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video ON action_log(video_id);
CREATE INDEX IF NOT EXISTS idx_status ON action_log(status);
CREATE INDEX IF NOT EXISTS idx_created ON action_log(created_at);
CREATE INDEX IF NOT EXISTS idx_goal ON action_log(goal_id);
"""


STATUSES = {
    "executed",
    "pr_opened",
    "pr_closed",
    "awaiting_greenlight",
    "approved",
    "proposal_drafted",
    "rejected",
    "halted",
    "executed_failed",
}


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    db_path = db_path or get_db_path()
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> Path:
    db_path = db_path or get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
    return db_path


def record_action(
    *,
    action_id: str,
    video_id: str,
    goal_id: str,
    atom_ids: list[str],
    stage2_relevance: int,
    raw_tier: str,
    final_tier: str,
    status: str,
    realm: str,
    action_description: str,
    effort_hours: int,
    reversibility: str,
    external_surface: bool,
    dependencies: list[str],
    artifacts: Optional[dict] = None,
    rejection_reason: Optional[str] = None,
    policy_note: Optional[str] = None,
    target_repo: Optional[str] = None,
) -> dict:
    """Idempotently insert a new action_log row. Returns the row as a dict.

    If a row with the same action_id already exists, it's returned as-is
    (no overwrite) so webhook replays don't lose history. To update status,
    use update_action_status.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; must be in {sorted(STATUSES)}")
    now = _now_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM action_log WHERE action_id = ?", (action_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)

        conn.execute(
            """
            INSERT INTO action_log (
                action_id, video_id, goal_id, atom_ids, stage2_relevance,
                raw_tier, final_tier, status, realm, action_description,
                effort_hours, reversibility, external_surface, dependencies,
                artifacts, rejection_reason, policy_note, target_repo,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, video_id, goal_id, json.dumps(atom_ids), stage2_relevance,
                raw_tier, final_tier, status, realm, action_description,
                effort_hours, reversibility, 1 if external_surface else 0,
                json.dumps(dependencies), json.dumps(artifacts) if artifacts else None,
                rejection_reason, policy_note, target_repo, now, now,
            ),
        )
        return {"action_id": action_id, "status": status, "created_at": now}


def update_action_status(
    action_id: str,
    status: str,
    *,
    artifacts: Optional[dict] = None,
    rejection_reason: Optional[str] = None,
) -> Optional[dict]:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    now = _now_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT artifacts FROM action_log WHERE action_id = ?", (action_id,)
        ).fetchone()
        if existing is None:
            return None

        new_artifacts = artifacts
        if new_artifacts is None and existing["artifacts"]:
            new_artifacts = json.loads(existing["artifacts"])
        if isinstance(new_artifacts, dict):
            new_artifacts = json.dumps(new_artifacts)

        conn.execute(
            """
            UPDATE action_log SET status = ?, artifacts = ?, rejection_reason = ?, updated_at = ?
            WHERE action_id = ?
            """,
            (status, new_artifacts, rejection_reason, now, action_id),
        )
        row = conn.execute(
            "SELECT * FROM action_log WHERE action_id = ?", (action_id,)
        ).fetchone()
        return dict(row)


def get_action(action_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM action_log WHERE action_id = ?", (action_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_actions(
    status: Optional[str] = None,
    video_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    clauses = []
    args: list = []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if video_id:
        clauses.append("video_id = ?")
        args.append(video_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM action_log{where} ORDER BY created_at DESC LIMIT ?",
            args,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def stats() -> dict:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM action_log GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("atom_ids", "dependencies", "artifacts"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    d["external_surface"] = bool(d.get("external_surface"))
    return d