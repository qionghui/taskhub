"""任务调度。数据在 station/taskhub.db。

页面是 station/pages/taskhub.html，内容从 /api/v1 来。
给员工的响应只用白名单，计划字段不会出现在里面。
领取靠一条带条件的 UPDATE，避免两台电脑领走同一条。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    repo_path      TEXT DEFAULT '',
    default_branch TEXT DEFAULT 'main',
    capabilities   TEXT DEFAULT '[]',
    status         TEXT DEFAULT 'active',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id          TEXT UNIQUE NOT NULL,
    project_code      TEXT NOT NULL,
    capabilities      TEXT DEFAULT '[]',
    token_hash        TEXT DEFAULT '',
    concurrency_slots INTEGER DEFAULT 1,
    status            TEXT DEFAULT 'active',
    last_heartbeat_at TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT UNIQUE NOT NULL,
    project_code   TEXT NOT NULL,
    title          TEXT NOT NULL,
    priority       TEXT DEFAULT 'P1',
    status         TEXT NOT NULL DEFAULT 'DRAFT',
    plan_background TEXT DEFAULT '',
    plan_notes      TEXT DEFAULT '',
    input_context        TEXT DEFAULT '{}',
    output_spec          TEXT DEFAULT '{}',
    acceptance_checklist TEXT DEFAULT '[]',
    workspace            TEXT DEFAULT '{}',
    assignee         TEXT,
    due_at           TEXT,
    timeout_seconds  INTEGER DEFAULT 3600,
    max_retries      INTEGER DEFAULT 2,
    retry_count      INTEGER DEFAULT 0,
    require_human_acceptance INTEGER DEFAULT 1,
    current_attempt  INTEGER DEFAULT 0,
    lease_token      TEXT,
    lease_expires_at TEXT,
    created_by       TEXT DEFAULT 'admin',
    created_at       TEXT NOT NULL,
    published_at     TEXT,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status, project_code);

CREATE TABLE IF NOT EXISTS task_attempt (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    attempt_no  INTEGER NOT NULL,
    agent_id    TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT DEFAULT 'running',
    commit_sha  TEXT,
    summary     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempt_task ON task_attempt(task_id);

CREATE TABLE IF NOT EXISTS task_event (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_task ON task_event(task_id, ts);

CREATE TABLE IF NOT EXISTS agent_event (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    ts       TEXT NOT NULL,
    event    TEXT NOT NULL,
    detail   TEXT DEFAULT '{}'
);
"""

TRANSITIONS = {
    "DRAFT": {"READY", "CANCELLED"},
    "READY": {"RUNNING", "CANCELLED", "DRAFT"},
    "RUNNING": {"SUBMITTED", "DONE", "READY", "FAILED", "CANCELLED"},
    "SUBMITTED": {"DONE", "READY", "CANCELLED"},
    "FAILED": {"READY", "CANCELLED"},
    "DONE": set(),
    "CANCELLED": set(),
}
TERMINAL = {"DONE", "FAILED", "CANCELLED"}
PRIORITY_ORDER = (
    "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
    "WHEN 'P2' THEN 2 ELSE 3 END"
)
PATCH_FIELDS = {
    "title", "priority", "due_at", "assignee",
    "input_context", "output_spec", "acceptance_checklist", "workspace",
    "plan_background", "plan_notes",
}
JSON_FIELDS = {"input_context", "output_spec", "acceptance_checklist", "workspace"}

_ready = False
_reaper_on = False


def _int_env(name, default):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DB = Path(os.environ.get("TASKHUB_DB_PATH") or (ROOT / "taskhub.db"))
FILES = Path(os.environ.get("TASKHUB_FILES_DIR") or (ROOT / "taskhub-files"))
LEASE_SECONDS = _int_env("TASKHUB_LEASE_SECONDS", 1800)
HEARTBEAT_INTERVAL = _int_env("TASKHUB_HEARTBEAT_INTERVAL_SECONDS", 60)
DEFAULT_TIMEOUT = _int_env("TASKHUB_DEFAULT_TIMEOUT_SECONDS", 3600)
DEFAULT_RETRIES = _int_env("TASKHUB_DEFAULT_MAX_RETRIES", 2)
DEFAULT_SLOTS = _int_env("TASKHUB_DEFAULT_CONCURRENCY_SLOTS", 2)
REAPER_INTERVAL = _int_env("TASKHUB_REAPER_INTERVAL_SECONDS", 15)
ONLINE_WINDOW = _int_env("TASKHUB_AGENT_ONLINE_WINDOW_SECONDS", 120)


class IllegalTransition(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jloads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def jdumps(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def ensure():
    global _ready
    if _ready:
        return
    FILES.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
    _ready = True


def start():
    global _reaper_on
    try:
        ensure()
    except Exception as exc:
        raise SystemExit(f"任务调度的数据库打不开：{exc}") from exc
    if _reaper_on:
        return
    _reaper_on = True
    threading.Thread(target=_reaper_loop, name="taskhub-reaper", daemon=True).start()


def _reaper_loop():
    while True:
        time.sleep(max(1, REAPER_INTERVAL))
        try:
            with session() as conn:
                with tx(conn):
                    reclaim_expired(conn)
        except Exception:
            pass


def err(status, message):
    trace = uuid.uuid4().hex[:12]
    return status, {
        "code": status,
        "message": message,
        "trace_id": trace,
    }, {"X-Trace-ID": trace}


def ok(data=None):
    trace = uuid.uuid4().hex[:12]
    return 200, {"code": 0, "message": "ok", "data": data}, {"X-Trace-ID": trace}


def log_event(conn, task_id, actor, event, detail=None):
    conn.execute(
        "INSERT INTO task_event (task_id, ts, actor, event, detail) VALUES (?,?,?,?,?)",
        (task_id, utcnow(), actor, event, jdumps(detail or {})),
    )


def transition(conn, task, to, actor, event, detail=None, clear_lease=False):
    frm = task["status"]
    if to not in TRANSITIONS.get(frm, set()):
        raise IllegalTransition(f"{frm} -> {to} 非法迁移")
    sets, params = ["status=?"], [to]
    if to == "READY" and frm == "DRAFT":
        sets.append("published_at=?")
        params.append(utcnow())
    if to in TERMINAL:
        sets.append("finished_at=?")
        params.append(utcnow())
    if clear_lease:
        sets.append("lease_token=NULL")
        sets.append("lease_expires_at=NULL")
    params.append(task["task_id"])
    conn.execute(f"UPDATE task SET {', '.join(sets)} WHERE task_id=?", params)
    merged = {"from": frm, "to": to}
    if detail:
        merged.update(detail)
    log_event(conn, task["task_id"], actor, event, merged)


def new_lease():
    token = "ls_" + uuid.uuid4().hex[:16]
    expires = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    return token, expires.strftime("%Y-%m-%dT%H:%M:%SZ")


def renew_lease():
    expires = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


def lease_expired(task):
    exp = task["lease_expires_at"]
    return bool(exp) and exp < utcnow()


def running_count(conn, agent_id):
    row = conn.execute(
        """SELECT COUNT(*) AS c FROM task t
           JOIN task_attempt a ON a.task_id = t.task_id AND a.attempt_no = t.current_attempt
           WHERE t.status = 'RUNNING' AND a.agent_id = ?""",
        (agent_id,),
    ).fetchone()
    return row["c"]


def reclaim_expired(conn):
    now = utcnow()
    rows = conn.execute(
        "SELECT * FROM task WHERE status='RUNNING' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at < ?",
        (now,),
    ).fetchall()
    reclaimed = []
    for task in rows:
        attempt = conn.execute(
            "SELECT * FROM task_attempt WHERE task_id=? AND attempt_no=?",
            (task["task_id"], task["current_attempt"]),
        ).fetchone()
        if attempt and attempt["status"] == "running":
            conn.execute(
                "UPDATE task_attempt SET status='reclaimed', ended_at=?, error='lease expired' WHERE id=?",
                (now, attempt["id"]),
            )
        new_retry = task["retry_count"] + 1
        conn.execute(
            "UPDATE task SET retry_count=? WHERE task_id=?",
            (new_retry, task["task_id"]),
        )
        fresh = get_task(conn, task["task_id"])
        if new_retry > task["max_retries"]:
            transition(conn, fresh, "FAILED", "system", "lease_expired", {
                "attempt": task["current_attempt"],
                "retry_count": new_retry,
                "reason": "租约过期且重试耗尽",
            }, clear_lease=True)
        else:
            transition(conn, fresh, "READY", "system", "lease_expired", {
                "attempt": task["current_attempt"],
                "retry_count": new_retry,
                "reason": "租约超时自动回收",
            }, clear_lease=True)
        reclaimed.append(task["task_id"])
    return reclaimed


def get_task(conn, task_id):
    return conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()


def gen_task_id(conn):
    prefix = "T-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-"
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM task WHERE task_id LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return f"{prefix}{row['c'] + 1:03d}"


def touch(conn, agent_id):
    conn.execute(
        "UPDATE agent SET last_heartbeat_at=? WHERE agent_id=?",
        (utcnow(), agent_id),
    )


def within_seconds(ts, seconds):
    if not ts:
        return False
    try:
        moment = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() <= seconds


def agent_dto(task, lease=None):
    # 计划区不在这份字段里。员工接口只能走这里出站。
    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "priority": task["priority"],
        "project_code": task["project_code"],
        "due_at": task["due_at"],
        "input_context": jloads(task["input_context"], {}),
        "output_spec": jloads(task["output_spec"], {}),
        "acceptance_checklist": jloads(task["acceptance_checklist"], []),
        "workspace": jloads(task["workspace"], {}),
        "timeout_seconds": task["timeout_seconds"],
        "retry": {"current": task["retry_count"], "max": task["max_retries"]},
        "lease": lease,
    }


def task_full(task):
    data = dict(task)
    for key in JSON_FIELDS:
        data[key] = jloads(data[key], [] if key == "acceptance_checklist" else {})
    return data


def resolve_agent(conn, agent_id, project_code):
    row = conn.execute("SELECT * FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
    if not row:
        if not project_code:
            return None, err(422, "project_code required for new agent")
        conn.execute(
            "INSERT INTO agent (agent_id, project_code, capabilities, token_hash,"
            " concurrency_slots, created_at) VALUES (?,?,?,'',?,?)",
            (agent_id, project_code, jdumps([]), DEFAULT_SLOTS, utcnow()),
        )
        row = conn.execute("SELECT * FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
    if row["status"] != "active":
        return None, err(403, "agent disabled")
    if project_code and row["project_code"] != project_code:
        return None, err(403, "project mismatch")
    return row, None


def claimable(conn, project_code, agent_id, limit):
    return conn.execute(
        f"""SELECT * FROM task
            WHERE status='READY' AND project_code=?
              AND (assignee IS NULL OR assignee='' OR assignee=?)
            ORDER BY {PRIORITY_ORDER}, created_at LIMIT ?""",
        (project_code, agent_id, limit),
    ).fetchall()


def check_lease(task, lease_token):
    if task["status"] == "CANCELLED":
        return None
    if task["status"] != "RUNNING":
        return err(409, f"task not RUNNING (status={task['status']})")
    if not task["lease_token"] or task["lease_token"] != lease_token:
        return err(409, "lease_token mismatch")
    if lease_expired(task):
        return err(410, "lease expired")
    return None


def actor_of_lease(conn, task_id, lease_token):
    row = conn.execute(
        "SELECT agent_id FROM task_attempt WHERE task_id=? AND lease_token=? "
        "ORDER BY attempt_no DESC LIMIT 1",
        (task_id, lease_token),
    ).fetchone()
    return row["agent_id"] if row else "unknown"


def text_of(body, key, required=False):
    if not isinstance(body, dict):
        return None
    value = body.get(key)
    if value is None:
        return None if required else ""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if required and not value:
        return None
    return value


def as_int(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return None


def as_object(value, fallback):
    if value is None:
        return fallback
    if isinstance(fallback, dict) and isinstance(value, dict):
        return value
    if isinstance(fallback, list) and isinstance(value, list):
        return value
    return None


def query_one(query, key):
    values = (query or {}).get(key) or []
    return values[-1] if values else None


def split_path(path):
    clean = []
    for piece in path.split("/"):
        if not piece:
            continue
        piece = unquote(piece)
        if (
            not piece
            or piece in (".", "..")
            or "/" in piece
            or "\\" in piece
            or "\x00" in piece
            or len(piece) > 200
        ):
            return None
        clean.append(piece)
    return clean


def preview(query):
    agent_id = query_one(query, "agent_id")
    if not agent_id:
        return err(422, "agent_id required")
    project_code = query_one(query, "project_code")
    limit = as_int(query_one(query, "limit"), 20)
    if limit is None:
        limit = 20
    limit = max(1, min(limit, 20))
    with session() as conn:
        agent, bad = resolve_agent(conn, agent_id, project_code)
        if bad:
            return bad
        rows = claimable(conn, agent["project_code"], agent["agent_id"], limit)
        return ok({
            "server_time": utcnow(),
            "tasks": [agent_dto(task) for task in rows],
        })


def claim(body):
    agent_id = text_of(body, "agent_id", required=True)
    project_code = text_of(body, "project_code", required=True)
    if not agent_id or not project_code:
        return err(422, "agent_id and project_code required")
    capabilities = body.get("capabilities", [])
    if not isinstance(capabilities, list):
        return err(422, "capabilities must be a list")
    limit = as_int(body.get("limit", 1), None)
    if limit is None:
        return err(422, "limit must be an integer")
    with session() as conn:
        agent, bad = resolve_agent(conn, agent_id, project_code)
        if bad:
            return bad
        touch(conn, agent["agent_id"])
        claimed = []
        with tx(conn):
            # 槽位和领取放在同一把写锁里，两台电脑同时领时后到的看见已占用的槽。
            free = agent["concurrency_slots"] - running_count(conn, agent["agent_id"])
            take = max(0, min(limit or 1, free, 20))
            for task in claimable(conn, project_code, agent["agent_id"], take):
                token, expires = new_lease()
                cur = conn.execute(
                    """UPDATE task SET status='RUNNING', lease_token=?,
                           lease_expires_at=?, current_attempt=current_attempt+1
                       WHERE task_id=? AND status='READY'""",
                    (token, expires, task["task_id"]),
                )
                if cur.rowcount != 1:
                    continue
                attempt_no = task["current_attempt"] + 1
                conn.execute(
                    """INSERT INTO task_attempt
                           (task_id, attempt_no, agent_id, lease_token, started_at, status)
                       VALUES (?,?,?,?,?,'running')""",
                    (task["task_id"], attempt_no, agent["agent_id"], token, utcnow()),
                )
                log_event(conn, task["task_id"], agent["agent_id"], "claimed", {
                    "attempt": attempt_no,
                    "lease_expires_at": expires,
                })
                fresh = get_task(conn, task["task_id"])
                claimed.append(agent_dto(fresh, {
                    "token": token,
                    "expires_at": expires,
                    "heartbeat_interval_seconds": HEARTBEAT_INTERVAL,
                }))
        return ok({"server_time": utcnow(), "tasks": claimed})


def heartbeat(task_id, body):
    lease_token = text_of(body, "lease_token", required=True)
    if not lease_token:
        return err(422, "lease_token required")
    percent = body.get("percent") if isinstance(body, dict) and "percent" in body else None
    if percent is not None:
        percent = as_int(percent, None)
        if percent is None:
            return err(422, "percent must be an integer")
    stage = body.get("stage") if isinstance(body, dict) else None
    log_line = body.get("log_line") if isinstance(body, dict) else None
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        if task["status"] == "CANCELLED":
            return ok({"revoked": True})
        bad = check_lease(task, lease_token)
        if bad:
            return bad
        actor = actor_of_lease(conn, task_id, lease_token)
        expires = renew_lease()
        with tx(conn):
            conn.execute(
                "UPDATE task SET lease_expires_at=? WHERE task_id=?",
                (expires, task_id),
            )
            touch(conn, actor)
            if percent is not None or stage or log_line:
                log_event(conn, task_id, actor, "progress", {
                    "percent": percent,
                    "stage": stage,
                    "log_line": log_line,
                })
        return ok({"revoked": False, "lease_expires_at": expires})


def submit(task_id, body):
    lease_token = text_of(body, "lease_token", required=True)
    if not lease_token:
        return err(422, "lease_token required")
    summary = text_of(body, "summary") or ""
    outputs = as_object(body.get("outputs") if isinstance(body, dict) else None, {})
    if outputs is None:
        return err(422, "outputs must be an object")
    pushed = as_bool(outputs.get("pushed"), False)
    if pushed is None:
        return err(422, "pushed must be a boolean")
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        dup = conn.execute(
            "SELECT 1 FROM task_attempt WHERE task_id=? AND lease_token=? AND status='success'",
            (task_id, lease_token),
        ).fetchone()
        if dup:
            return ok({"duplicate": True, "status": task["status"]})
        bad = check_lease(task, lease_token)
        if bad:
            return bad
        workspace = jloads(task["workspace"], {})
        if workspace.get("require_push") and not pushed:
            return err(422, "require_push=true 但 pushed=false，提交被拒")
        actor = actor_of_lease(conn, task_id, lease_token)
        now = utcnow()
        to = "SUBMITTED" if task["require_human_acceptance"] else "DONE"
        try:
            with tx(conn):
                conn.execute(
                    """UPDATE task_attempt SET status='success', ended_at=?,
                           commit_sha=?, summary=?
                       WHERE task_id=? AND attempt_no=?""",
                    (now, outputs.get("commit_sha"), summary, task_id, task["current_attempt"]),
                )
                transition(conn, task, to, actor, "submitted", {
                    "summary": summary,
                    "commit_sha": outputs.get("commit_sha"),
                    "test_result": outputs.get("test_result"),
                }, clear_lease=True)
                touch(conn, actor)
        except IllegalTransition as exc:
            return err(409, str(exc))
        return ok({"status": to})


def fail(task_id, body):
    lease_token = text_of(body, "lease_token", required=True)
    if not lease_token:
        return err(422, "lease_token required")
    error_code = text_of(body, "error_code") or "AGENT_ERROR"
    error_message = text_of(body, "error_message") or ""
    retryable = as_bool(body.get("retryable") if isinstance(body, dict) else None, True)
    if retryable is None:
        return err(422, "retryable must be a boolean")
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        bad = check_lease(task, lease_token)
        if bad:
            return bad
        actor = actor_of_lease(conn, task_id, lease_token)
        now = utcnow()
        new_retry = task["retry_count"] + 1
        will_retry = retryable and new_retry <= task["max_retries"]
        try:
            with tx(conn):
                conn.execute(
                    """UPDATE task_attempt SET status='failed', ended_at=?, error=?
                       WHERE task_id=? AND attempt_no=?""",
                    (now, f"{error_code}: {error_message}", task_id, task["current_attempt"]),
                )
                conn.execute(
                    "UPDATE task SET retry_count=? WHERE task_id=?",
                    (new_retry, task_id),
                )
                fresh = get_task(conn, task_id)
                transition(
                    conn, fresh, "READY" if will_retry else "FAILED",
                    actor, "failed", {
                        "error_code": error_code,
                        "error_message": error_message,
                        "retry_count": new_retry,
                        "will_retry": will_retry,
                    }, clear_lease=True,
                )
                touch(conn, actor)
        except IllegalTransition as exc:
            return err(409, str(exc))
        return ok({
            "status": "READY" if will_retry else "FAILED",
            "retry_count": new_retry,
        })


def release(task_id, body):
    lease_token = text_of(body, "lease_token", required=True)
    if not lease_token:
        return err(422, "lease_token required")
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        bad = check_lease(task, lease_token)
        if bad:
            return bad
        actor = actor_of_lease(conn, task_id, lease_token)
        try:
            with tx(conn):
                conn.execute(
                    """UPDATE task_attempt SET status='released', ended_at=?
                       WHERE task_id=? AND attempt_no=?""",
                    (utcnow(), task_id, task["current_attempt"]),
                )
                transition(conn, task, "READY", actor, "released", clear_lease=True)
        except IllegalTransition as exc:
            return err(409, str(exc))
        return ok({"status": "READY"})


def agent_heartbeat(body):
    agent_id = text_of(body, "agent_id", required=True)
    if not agent_id:
        return err(422, "agent_id required")
    with session() as conn:
        row = conn.execute("SELECT * FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO agent (agent_id, project_code, capabilities, token_hash,"
                " concurrency_slots, created_at) VALUES (?,?,?,'',?,?)",
                (agent_id, "", jdumps([]), DEFAULT_SLOTS, utcnow()),
            )
        touch(conn, agent_id)
        return ok({"server_time": utcnow()})


def create_project(body):
    code = text_of(body, "code", required=True)
    name = text_of(body, "name", required=True)
    if not code or not name:
        return err(422, "code and name required")
    repo_path = text_of(body, "repo_path") or ""
    branch = text_of(body, "default_branch") or "main"
    capabilities = as_object(body.get("capabilities", []), [])
    if capabilities is None:
        return err(422, "capabilities must be a list")
    with session() as conn:
        if conn.execute("SELECT 1 FROM project WHERE code=?", (code,)).fetchone():
            return err(409, "project code exists")
        conn.execute(
            "INSERT INTO project (code,name,repo_path,default_branch,capabilities,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (code, name, repo_path, branch, jdumps(capabilities), utcnow()),
        )
        return ok({"code": code})


def list_projects():
    with session() as conn:
        rows = conn.execute(
            """SELECT p.*, (SELECT COUNT(*) FROM task t WHERE t.project_code=p.code) AS task_count
               FROM project p ORDER BY p.code"""
        ).fetchall()
        return ok({"projects": [
            {
                "code": row["code"],
                "name": row["name"],
                "repo_path": row["repo_path"],
                "default_branch": row["default_branch"],
                "status": row["status"],
                "task_count": row["task_count"],
                "capabilities": jloads(row["capabilities"], []),
            }
            for row in rows
        ]})


def create_agent(body):
    agent_id = text_of(body, "agent_id", required=True)
    project_code = text_of(body, "project_code", required=True)
    if not agent_id or not project_code:
        return err(422, "agent_id and project_code required")
    capabilities = as_object(body.get("capabilities", []), [])
    if capabilities is None:
        return err(422, "capabilities must be a list")
    slots = as_int(body.get("concurrency_slots", 1), None)
    if slots is None or slots < 0:
        return err(422, "concurrency_slots must be an integer")
    with session() as conn:
        if not conn.execute("SELECT 1 FROM project WHERE code=?", (project_code,)).fetchone():
            return err(404, "project not found")
        if conn.execute("SELECT 1 FROM agent WHERE agent_id=?", (agent_id,)).fetchone():
            return err(409, "agent_id exists")
        conn.execute(
            "INSERT INTO agent (agent_id,project_code,capabilities,token_hash,"
            "concurrency_slots,created_at) VALUES (?,?,?,'',?,?)",
            (agent_id, project_code, jdumps([str(item) for item in capabilities]), slots, utcnow()),
        )
        return ok({"agent_id": agent_id})


def list_agents():
    with session() as conn:
        rows = conn.execute("SELECT * FROM agent ORDER BY agent_id").fetchall()
        agents = []
        for row in rows:
            online = bool(row["last_heartbeat_at"]) and within_seconds(
                row["last_heartbeat_at"], ONLINE_WINDOW,
            )
            running = conn.execute(
                """SELECT t.task_id, t.title FROM task t
                   JOIN task_attempt a ON a.task_id=t.task_id AND a.attempt_no=t.current_attempt
                   WHERE t.status='RUNNING' AND a.agent_id=?""",
                (row["agent_id"],),
            ).fetchall()
            agents.append({
                "agent_id": row["agent_id"],
                "project_code": row["project_code"],
                "capabilities": jloads(row["capabilities"], []),
                "concurrency_slots": row["concurrency_slots"],
                "status": row["status"],
                "online": online,
                "last_heartbeat_at": row["last_heartbeat_at"],
                "current_tasks": [
                    {"task_id": task["task_id"], "title": task["title"]}
                    for task in running
                ],
            })
        return ok({"agents": agents})


def toggle_agent(agent_id):
    with session() as conn:
        row = conn.execute("SELECT * FROM agent WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            return err(404, "agent not found")
        new = "disabled" if row["status"] == "active" else "active"
        conn.execute("UPDATE agent SET status=? WHERE agent_id=?", (new, agent_id))
        return ok({"agent_id": agent_id, "status": new})


def patch_agent(agent_id, body):
    project_code = text_of(body, "project_code", required=True)
    if not project_code:
        return err(422, "project_code required")
    capabilities = as_object(body.get("capabilities", []), [])
    if capabilities is None:
        return err(422, "capabilities must be a list")
    slots = as_int(body.get("concurrency_slots", 1), None)
    if slots is None or slots < 0:
        return err(422, "concurrency_slots must be an integer")
    with session() as conn:
        if not conn.execute("SELECT 1 FROM agent WHERE agent_id=?", (agent_id,)).fetchone():
            return err(404, "agent not found")
        conn.execute(
            "UPDATE agent SET project_code=?, capabilities=?, concurrency_slots=? WHERE agent_id=?",
            (project_code, jdumps(capabilities), slots, agent_id),
        )
        return ok({"agent_id": agent_id})


def create_task(body):
    title = text_of(body, "title", required=True)
    project_code = text_of(body, "project_code", required=True)
    if not title or not project_code:
        return err(422, "title and project_code required")
    priority = text_of(body, "priority") or "P1"
    input_context = as_object(body.get("input_context", {}), {})
    output_spec = as_object(body.get("output_spec", {}), {})
    checklist = as_object(body.get("acceptance_checklist", []), [])
    workspace = as_object(body.get("workspace", {}), {})
    if input_context is None or output_spec is None or checklist is None or workspace is None:
        return err(422, "task fields must match their types")
    accept = as_bool(body.get("require_human_acceptance"), True)
    publish = as_bool(body.get("publish"), False)
    if accept is None or publish is None:
        return err(422, "boolean field required")
    timeout = as_int(body.get("timeout_seconds"), None)
    retries = body.get("max_retries") if isinstance(body, dict) else None
    if "max_retries" in body and body.get("max_retries") is not None:
        retries = as_int(body.get("max_retries"), None)
        if retries is None:
            return err(422, "max_retries must be an integer")
    else:
        retries = DEFAULT_RETRIES
    timeout = timeout or DEFAULT_TIMEOUT
    assignee = text_of(body, "assignee") or None
    due_at = text_of(body, "due_at") or None
    with session() as conn:
        if not conn.execute("SELECT 1 FROM project WHERE code=?", (project_code,)).fetchone():
            return err(404, "project not found")
        try:
            with tx(conn):
                task_id = gen_task_id(conn)
                conn.execute(
                    """INSERT INTO task (task_id, project_code, title, priority, status,
                           plan_background, plan_notes, input_context, output_spec,
                           acceptance_checklist, workspace, assignee, due_at,
                           timeout_seconds, max_retries, require_human_acceptance,
                           created_by, created_at)
                       VALUES (?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, project_code, title, priority,
                        text_of(body, "plan_background") or "",
                        text_of(body, "plan_notes") or "",
                        jdumps(input_context), jdumps(output_spec),
                        jdumps([str(item) for item in checklist]), jdumps(workspace),
                        assignee, due_at, timeout, retries,
                        1 if accept else 0, "admin", utcnow(),
                    ),
                )
                log_event(conn, task_id, "admin", "created", {"title": title})
                if publish:
                    task = get_task(conn, task_id)
                    transition(conn, task, "READY", "admin", "published")
        except IllegalTransition as exc:
            return err(409, str(exc))
        return ok({"task_id": task_id})


def list_tasks(query):
    sql, params = "SELECT * FROM task WHERE 1=1", []
    project_code = query_one(query, "project_code")
    status = query_one(query, "status")
    priority = query_one(query, "priority")
    keyword = query_one(query, "q")
    if project_code:
        sql += " AND project_code=?"
        params.append(project_code)
    if status:
        sql += " AND status=?"
        params.append(status)
    if priority:
        sql += " AND priority=?"
        params.append(priority)
    if keyword:
        sql += " AND (title LIKE ? OR task_id LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    limit = as_int(query_one(query, "limit"), 100)
    if limit is None:
        limit = 100
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with session() as conn:
        rows = conn.execute(sql, params).fetchall()
        return ok({"tasks": [task_full(row) for row in rows]})


def task_detail(task_id):
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        events = conn.execute(
            "SELECT ts, actor, event, detail FROM task_event WHERE task_id=? ORDER BY ts, id",
            (task_id,),
        ).fetchall()
        attempts = conn.execute(
            "SELECT * FROM task_attempt WHERE task_id=? ORDER BY attempt_no",
            (task_id,),
        ).fetchall()
        return ok({
            "task": task_full(task),
            "events": [
                {
                    "ts": event["ts"],
                    "actor": event["actor"],
                    "event": event["event"],
                    "detail": jloads(event["detail"], {}),
                }
                for event in events
            ],
            "attempts": [dict(attempt) for attempt in attempts],
        })


def do_transition(task_id, to, event, detail=None):
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        try:
            with tx(conn):
                transition(
                    conn, task, to, "admin", event, detail,
                    clear_lease=(to == "CANCELLED"),
                )
        except IllegalTransition as exc:
            return err(409, str(exc))
        return ok({"task_id": task_id, "status": to})


def clone_task(task_id):
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        with tx(conn):
            new_id = gen_task_id(conn)
            conn.execute(
                """INSERT INTO task (task_id, project_code, title, priority, status,
                       plan_background, plan_notes, input_context, output_spec,
                       acceptance_checklist, workspace, assignee, due_at,
                       timeout_seconds, max_retries, require_human_acceptance,
                       created_by, created_at)
                   VALUES (?,?,?,?,'DRAFT',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id, task["project_code"], task["title"] + "（副本）", task["priority"],
                    task["plan_background"], task["plan_notes"], task["input_context"],
                    task["output_spec"], task["acceptance_checklist"], task["workspace"],
                    task["assignee"], task["due_at"], task["timeout_seconds"], task["max_retries"],
                    task["require_human_acceptance"], "admin", utcnow(),
                ),
            )
            log_event(conn, new_id, "admin", "created", {"cloned_from": task_id})
        return ok({"task_id": new_id})


def patch_task(task_id, body):
    if not isinstance(body, dict):
        return err(422, "nothing to update")
    fields = {key: value for key, value in body.items() if key in PATCH_FIELDS and value is not None}
    if not fields:
        return err(422, "nothing to update")
    with session() as conn:
        task = get_task(conn, task_id)
        if not task:
            return err(404, "task not found")
        if task["status"] not in ("DRAFT", "READY"):
            return err(409, f"status={task['status']} 不可编辑")
        sets, params = [], []
        for key, value in fields.items():
            if key in JSON_FIELDS:
                if key == "acceptance_checklist":
                    value = as_object(value, [])
                else:
                    value = as_object(value, {})
                if value is None:
                    return err(422, f"{key} has the wrong type")
                value = jdumps(value)
            if key == "assignee":
                value = value or None
            sets.append(f"{key}=?")
            params.append(value)
        params.append(task_id)
        with tx(conn):
            conn.execute(f"UPDATE task SET {', '.join(sets)} WHERE task_id=?", params)
            log_event(conn, task_id, "admin", "edited", {"fields": list(fields)})
        return ok({"task_id": task_id})


def overview():
    today = utcnow()[:10]
    with session() as conn:
        counts = {
            row["status"]: row["c"]
            for row in conn.execute("SELECT status, COUNT(*) AS c FROM task GROUP BY status")
        }
        done_today = conn.execute(
            "SELECT COUNT(*) AS c FROM task WHERE status='DONE' AND finished_at LIKE ?",
            (today + "%",),
        ).fetchone()["c"]
        overdue = conn.execute(
            "SELECT task_id, title FROM task WHERE due_at IS NOT NULL AND due_at<? "
            "AND status IN ('READY','RUNNING')",
            (utcnow(),),
        ).fetchall()
        failed = conn.execute(
            "SELECT task_id, title FROM task WHERE status='FAILED' ORDER BY finished_at DESC LIMIT 5"
        ).fetchall()
        agents = conn.execute(
            "SELECT agent_id, last_heartbeat_at, status FROM agent"
        ).fetchall()
        offline = [
            agent["agent_id"] for agent in agents
            if agent["status"] == "active" and not within_seconds(
                agent["last_heartbeat_at"] or "1970-01-01T00:00:00Z",
                ONLINE_WINDOW,
            )
        ]
        return ok({
            "counts": counts,
            "done_today": done_today,
            "alerts": {
                "overdue": [{"task_id": row["task_id"], "title": row["title"]} for row in overdue],
                "failed": [{"task_id": row["task_id"], "title": row["title"]} for row in failed],
                "offline_agents": offline,
            },
        })


def agent_api(method, rest, query, body):
    if rest == ["tasks"] and method == "GET":
        return preview(query)
    if rest == ["tasks", "claim"] and method == "POST":
        return claim(body)
    if rest == ["heartbeat"] and method == "POST":
        return agent_heartbeat(body)
    if len(rest) == 3 and rest[0] == "tasks" and method == "POST":
        task_id, action = rest[1], rest[2]
        if action == "heartbeat":
            return heartbeat(task_id, body)
        if action == "submit":
            return submit(task_id, body)
        if action == "fail":
            return fail(task_id, body)
        if action == "release":
            return release(task_id, body)
    return err(404, "not found")


def admin_api(method, rest, query, body):
    if rest == ["projects"] and method == "GET":
        return list_projects()
    if rest == ["projects"] and method == "POST":
        return create_project(body)
    if rest == ["agents"] and method == "GET":
        return list_agents()
    if rest == ["agents"] and method == "POST":
        return create_agent(body)
    if rest == ["overview"] and method == "GET":
        return overview()
    if rest == ["tasks"] and method == "GET":
        return list_tasks(query)
    if rest == ["tasks"] and method == "POST":
        return create_task(body)
    if len(rest) == 2 and rest[0] == "agents" and method == "PATCH":
        return patch_agent(rest[1], body)
    if len(rest) == 3 and rest[0] == "agents" and rest[2] == "toggle" and method == "POST":
        return toggle_agent(rest[1])
    if len(rest) == 2 and rest[0] == "tasks" and method == "GET":
        return task_detail(rest[1])
    if len(rest) == 2 and rest[0] == "tasks" and method == "PATCH":
        return patch_task(rest[1], body)
    if len(rest) == 3 and rest[0] == "tasks" and method == "POST":
        task_id, action = rest[1], rest[2]
        if action == "publish":
            return do_transition(task_id, "READY", "published")
        if action == "cancel":
            return do_transition(task_id, "CANCELLED", "cancelled")
        if action == "accept":
            return do_transition(task_id, "DONE", "accepted")
        if action == "reject":
            reason = text_of(body, "reason") or ""
            return do_transition(task_id, "READY", "rejected", {"reason": reason})
        if action == "clone":
            return clone_task(task_id)
    return err(404, "not found")


def handle(method, path, query, body):
    ensure()
    parts = split_path(path)
    if parts is None or parts[:2] != ["api", "v1"] or len(parts) < 3:
        return err(404, "not found")
    section, rest = parts[2], parts[3:]
    if section == "agent":
        return agent_api(method, rest, query, body or {})
    if section == "admin":
        return admin_api(method, rest, query, body or {})
    return err(404, "not found")
