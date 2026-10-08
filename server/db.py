"""TaskHub 数据库层。

单文件 SQLite，开启 WAL 以支持多 agent 并发读写。
所有时间统一存 UTC ISO8601 字符串，客户端负责按本地时区展示。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

DATA_DIR = os.environ.get(
    "TASKHUB_DATA",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
)
DB_PATH = os.path.join(DATA_DIR, "taskhub.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

_local = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """每线程一个连接。agent 端和 Web 端会并发访问。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    repo_url      TEXT DEFAULT '',
    branch        TEXT DEFAULT 'main',
    repo_path     TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    -- 非 AI 类任务直接执行的命令（如构建、打包、跑测试）。留空则交给 WorkBuddy。
    command       TEXT DEFAULT '',
    -- 手动排序用。数值越小越靠前，默认与 id 同序，保持老数据原有顺序。
    sort_order    INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL
);

-- 身份即能力标签：win-a 只会领 required_labels 里含 win-a 的任务
CREATE TABLE IF NOT EXISTS agents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    identity       TEXT NOT NULL,
    token          TEXT NOT NULL UNIQUE,
    labels         TEXT NOT NULL DEFAULT '[]',
    workspace      TEXT DEFAULT '',
    auto_push      INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_seen      TEXT DEFAULT '',
    current_task   INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    -- pending / claimed / running / review / done / failed / cancelled
    status           TEXT NOT NULL DEFAULT 'pending',
    priority         INTEGER NOT NULL DEFAULT 0,
    required_labels  TEXT NOT NULL DEFAULT '[]',
    assigned_agent   TEXT DEFAULT '',
    branch           TEXT DEFAULT '',
    result_summary   TEXT DEFAULT '',
    error            TEXT DEFAULT '',
    commit_sha       TEXT DEFAULT '',
    retry_count      INTEGER NOT NULL DEFAULT 0,
    heartbeat_at     TEXT DEFAULT '',
    created_at       TEXT NOT NULL,
    claimed_at       TEXT DEFAULT '',
    finished_at      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime        TEXT DEFAULT '',
    size        INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_name TEXT DEFAULT 'system',
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_project_seq ON tasks(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_status        ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project       ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_logs_task           ON task_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_attach_task         ON attachments(task_id);
"""


def init_db() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn) -> None:
    """给已存在的表补齐后加的列。

    CREATE TABLE IF NOT EXISTS 不会改动已建好的表结构，
    所以新增字段必须在这里补，否则老库会一直报 no such column。
    """
    for table, column, ddl in [
        ("projects", "sort_order", "ALTER TABLE projects ADD COLUMN sort_order INTEGER DEFAULT 0"),
    ]:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    # 老项目 sort_order 为 0，按 id 赋值以保持原有先后顺序
    conn.execute("UPDATE projects SET sort_order=id WHERE sort_order IS NULL OR sort_order=0")
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    cur = connect().execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    cur = connect().execute(sql, tuple(params))
    row = cur.fetchone()
    return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    return connect().execute(sql, tuple(params)).lastrowid


def execute_many(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    connect().executemany(sql, [tuple(s) for s in seq])


def loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_to_project(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "repo_url": row.get("repo_url") or "",
        "branch": row.get("branch") or "main",
        "repo_path": row.get("repo_path") or "",
        "description": row.get("description") or "",
        "command": row.get("command") or "",
        "sort_order": row.get("sort_order") if row.get("sort_order") is not None else row["id"],
        "created_at": row["created_at"],
    }


def row_to_agent(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "identity": row["identity"],
        "labels": loads(row.get("labels"), []),
        "workspace": row.get("workspace") or "",
        "auto_push": bool(row.get("auto_push", 1)),
        "enabled": bool(row.get("enabled", 1)),
        "last_seen": row.get("last_seen") or "",
        "current_task": row.get("current_task"),
        "created_at": row["created_at"],
    }


def row_to_task(row: dict, with_detail: bool = False) -> dict:
    task = {
        "id": row["id"],
        "seq": row["seq"],
        "project_id": row["project_id"],
        "project_name": row.get("project_name", ""),
        "title": row["title"],
        "description": row.get("description") or "",
        "status": row["status"],
        "priority": row.get("priority", 0),
        "required_labels": loads(row.get("required_labels"), []),
        "assigned_agent": row.get("assigned_agent") or "",
        "branch": row.get("branch") or "",
        "result_summary": row.get("result_summary") or "",
        "error": row.get("error") or "",
        "commit_sha": row.get("commit_sha") or "",
        "retry_count": row.get("retry_count", 0),
        "created_at": row["created_at"],
        "claimed_at": row.get("claimed_at") or "",
        "finished_at": row.get("finished_at") or "",
    }
    if with_detail:
        task["attachments"] = query(
            "SELECT id, filename, mime, size, created_at FROM attachments WHERE task_id=? ORDER BY id",
            (row["id"],),
        )
        task["logs"] = query(
            "SELECT id, agent_name, level, message, created_at FROM task_logs WHERE task_id=? ORDER BY id",
            (row["id"],),
        )
        task["comments"] = query(
            "SELECT id, author, body, created_at FROM task_comments WHERE task_id=? ORDER BY id",
            (row["id"],),
        )
    return task