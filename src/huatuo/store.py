"""SQLite 状态存储：任务队列、MR 状态、问题（finding）生命周期、审计日志、反馈样本。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE,
    project_path TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- new_push | dev_reply | human_command
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mr_state (
    project_path TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    last_seen_updated_at TEXT,
    last_reviewed_sha TEXT,
    summary_note_id INTEGER,
    last_discussion_scan TEXT,
    pipeline_wait_sha TEXT,
    pipeline_notice TEXT,
    PRIMARY KEY (project_path, mr_iid)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    conclusion TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (project_path, mr_iid, head_sha)
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    mr_iid INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    discussion_id TEXT,
    inline INTEGER NOT NULL DEFAULT 1,
    file TEXT NOT NULL,
    line INTEGER,
    end_line INTEGER,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    suggestion TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    dispute_rounds INTEGER NOT NULL DEFAULT 0,
    first_sha TEXT NOT NULL,
    last_checked_sha TEXT NOT NULL,
    parent_fingerprint TEXT,
    source TEXT NOT NULL DEFAULT 'llm',   -- llm | lint
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_path, mr_iid, fingerprint)
);
CREATE TABLE IF NOT EXISTS finding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id INTEGER NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT,
    sha TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT NOT NULL,
    label TEXT NOT NULL,            -- false_positive | true_positive
    finding TEXT NOT NULL,          -- JSON 快照
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_notes (
    note_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""

FINDING_FIELDS = [
    "fingerprint", "discussion_id", "inline", "file", "line", "end_line", "severity", "category",
    "title", "detail", "evidence", "suggestion", "status", "status_reason", "dispute_rounds",
    "first_sha", "last_checked_sha", "parent_fingerprint", "source",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(mr_state)")}
        for col in ("pipeline_wait_sha", "pipeline_notice"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE mr_state ADD COLUMN {col} TEXT")
        fcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(findings)")}
        if "source" not in fcols:
            self.conn.execute("ALTER TABLE findings ADD COLUMN source TEXT NOT NULL DEFAULT 'llm'")

    def _exec(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

    # ---------------- jobs ----------------
    def enqueue(self, project_path: str, mr_iid: int, kind: str, payload: dict, dedupe_key: str) -> bool:
        ts = now()
        cur = self._exec(
            "INSERT OR IGNORE INTO jobs (dedupe_key, project_path, mr_iid, kind, payload, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (dedupe_key, project_path, mr_iid, kind, json.dumps(payload, ensure_ascii=False), ts, ts),
        )
        return cur.rowcount > 0

    def next_job(self) -> dict | None:
        # 同一 MR 的 push 优先于回复类事件，保证先拿到最新代码
        row = self._exec(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY (kind != 'new_push'), id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        self._exec("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now(), row["id"]))
        job = dict(row)
        job["payload"] = json.loads(job["payload"])
        return job

    def running_jobs(self) -> list[dict]:
        rows = self._exec("SELECT * FROM jobs WHERE status='running'").fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        self._exec(
            "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
            ("failed" if error else "done", error, now(), job_id),
        )

    def recent_jobs(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def job(self, job_id: int) -> dict | None:
        row = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    def requeue_job(self, job_id: int) -> None:
        self._exec("UPDATE jobs SET status='pending', updated_at=? WHERE id=?", (now(), job_id))

    def has_active_thread_job(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """该讨论是否已有排队中或执行中的任务（开发者回复、人工命令等）。"""
        return self._exec(
            "SELECT 1 FROM jobs WHERE project_path=? AND mr_iid=? AND status IN ('pending','running')"
            " AND json_extract(payload, '$.discussion_id')=? LIMIT 1",
            (project_path, mr_iid, discussion_id),
        ).fetchone() is not None

    # ---------------- mr_state ----------------
    def mr_state(self, project_path: str, mr_iid: int) -> dict:
        row = self._exec(
            "SELECT * FROM mr_state WHERE project_path=? AND mr_iid=?", (project_path, mr_iid)
        ).fetchone()
        return dict(row) if row else {"project_path": project_path, "mr_iid": mr_iid}

    def update_mr_state(self, project_path: str, mr_iid: int, **fields: Any) -> None:
        self._exec(
            "INSERT OR IGNORE INTO mr_state (project_path, mr_iid) VALUES (?,?)", (project_path, mr_iid)
        )
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(
            f"UPDATE mr_state SET {sets} WHERE project_path=? AND mr_iid=?",
            (*fields.values(), project_path, mr_iid),
        )

    # ---------------- reviews ----------------
    def reviewed(self, project_path: str, mr_iid: int, head_sha: str) -> bool:
        return self._exec(
            "SELECT 1 FROM reviews WHERE project_path=? AND mr_iid=? AND head_sha=?",
            (project_path, mr_iid, head_sha),
        ).fetchone() is not None

    def record_review(self, project_path: str, mr_iid: int, head_sha: str, conclusion: str, summary: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO reviews (project_path, mr_iid, head_sha, conclusion, summary, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (project_path, mr_iid, head_sha, conclusion, summary, now()),
        )

    def reviews(self, project_path: str, mr_iid: int) -> list[dict]:
        return [dict(r) for r in self._exec(
            "SELECT * FROM reviews WHERE project_path=? AND mr_iid=? ORDER BY id DESC", (project_path, mr_iid)
        ).fetchall()]

    def last_summary(self, project_path: str, mr_iid: int) -> str | None:
        row = self._exec(
            "SELECT summary FROM reviews WHERE project_path=? AND mr_iid=? ORDER BY id DESC LIMIT 1",
            (project_path, mr_iid),
        ).fetchone()
        return row["summary"] if row else None

    def waiting_pipeline(self) -> list[dict]:
        rows = self._exec("SELECT * FROM mr_state WHERE pipeline_wait_sha IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    # ---------------- findings ----------------
    def findings(self, project_path: str, mr_iid: int) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM findings WHERE project_path=? AND mr_iid=? ORDER BY id", (project_path, mr_iid)
        ).fetchall()
        return [dict(r) for r in rows]

    def escalated_findings(self) -> list[dict]:
        """所有 MR 中等待人工裁决的发现。"""
        return [dict(r) for r in self._exec(
            "SELECT id, project_path, mr_iid, severity, file, line, title, status_reason, updated_at "
            "FROM findings WHERE status='ESCALATED' ORDER BY updated_at DESC"
        ).fetchall()]

    def finding_by_discussion(self, discussion_id: str) -> dict | None:
        row = self._exec("SELECT * FROM findings WHERE discussion_id=?", (discussion_id,)).fetchone()
        return dict(row) if row else None

    def discussion_ids(self, project_path: str, mr_iid: int) -> dict[str, dict]:
        return {f["discussion_id"]: f for f in self.findings(project_path, mr_iid) if f["discussion_id"]}

    def upsert_finding(self, project_path: str, mr_iid: int, f: dict) -> int:
        ts = now()
        data = {k: f.get(k) for k in FINDING_FIELDS}
        data["inline"] = 1 if f.get("inline", True) else 0
        data["dispute_rounds"] = data.get("dispute_rounds") or 0
        data["evidence"] = data.get("evidence") or ""
        data["source"] = data.get("source") or "llm"
        existing = self._exec(
            "SELECT id FROM findings WHERE project_path=? AND mr_iid=? AND fingerprint=?",
            (project_path, mr_iid, data["fingerprint"]),
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in data)
            self._exec(
                f"UPDATE findings SET {sets}, updated_at=? WHERE id=?", (*data.values(), ts, existing["id"])
            )
            return existing["id"]
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = self._exec(
            f"INSERT INTO findings (project_path, mr_iid, {cols}, created_at, updated_at) VALUES (?,?,{marks},?,?)",
            (project_path, mr_iid, *data.values(), ts, ts),
        )
        return cur.lastrowid

    def add_event(self, finding_id: int, actor: str, kind: str, content: str, sha: str | None) -> None:
        self._exec(
            "INSERT INTO finding_events (finding_id, actor, kind, content, sha, created_at) VALUES (?,?,?,?,?,?)",
            (finding_id, actor, kind, content, sha, now()),
        )

    def events(self, finding_id: int) -> list[dict]:
        return [dict(r) for r in self._exec(
            "SELECT * FROM finding_events WHERE finding_id=? ORDER BY id", (finding_id,)
        ).fetchall()]

    # ---------------- feedback ----------------
    def add_feedback(self, project_path: str, label: str, finding: dict, reason: str) -> None:
        snap = {k: finding.get(k) for k in ("file", "severity", "category", "title", "detail", "evidence")}
        self._exec(
            "INSERT INTO feedback_samples (project_path, label, finding, reason, created_at) VALUES (?,?,?,?,?)",
            (project_path, label, json.dumps(snap, ensure_ascii=False), reason, now()),
        )

    def feedback(self, project_path: str, label: str, limit: int = 3) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM feedback_samples WHERE project_path=? AND label=? ORDER BY id DESC LIMIT ?",
            (project_path, label, limit),
        ).fetchall()
        return [dict(r) | {"finding": json.loads(r["finding"])} for r in rows]

    # ---------------- 质量统计 ----------------
    def quality_rows(self) -> list[dict]:
        """每条发现一行，带最终状态与关闭原因，供精确率统计。"""
        return [dict(r) for r in self._exec(
            "SELECT project_path, mr_iid, severity, category, status, status_reason, created_at, dispute_rounds FROM findings"
        ).fetchall()]

    # ---------------- notes ----------------
    def note_processed(self, note_id: int) -> bool:
        return self._exec("SELECT 1 FROM processed_notes WHERE note_id=?", (note_id,)).fetchone() is not None

    def mark_note(self, note_id: int) -> None:
        self._exec("INSERT OR IGNORE INTO processed_notes (note_id, created_at) VALUES (?,?)", (note_id, now()))
