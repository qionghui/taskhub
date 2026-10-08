"""TaskHub 服务端：REST API + 静态前端。

启动：
    python -m uvicorn server.app:app --host 0.0.0.0 --port 8787
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .db import dumps, execute, execute_many, loads, now, query, query_one, row_to_agent, row_to_project, row_to_task

STATIC_DIR = Path(__file__).parent / "static"
ADMIN_TOKEN = os.environ.get("TASKHUB_ADMIN_TOKEN", "taskhub")

app = FastAPI(title="TaskHub", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------- 鉴权


def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Web 控制台写操作鉴权。token 通过 header 传入，登录后存 localStorage。"""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")


def auth_agent(x_agent_token: str = Header(default="")) -> dict:
    """agent 端鉴权：用注册时下发的 token 反查身份。"""
    row = query_one("SELECT * FROM agents WHERE token=?", (x_agent_token,))
    if not row:
        raise HTTPException(status_code=401, detail="invalid agent token")
    if not row["enabled"]:
        raise HTTPException(status_code=403, detail="agent disabled")
    return row


# ---------------------------------------------------------------- 模型


class ProjectIn(BaseModel):
    name: str
    repo_url: str = ""
    branch: str = "main"
    repo_path: str = ""
    description: str = ""
    command: str = ""


class TaskIn(BaseModel):
    project_id: int
    title: str
    description: str = ""
    priority: int = 0
    required_labels: list[str] = Field(default_factory=list)
    assigned_agent: str = ""


class TaskPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    required_labels: Optional[list[str]] = None
    assigned_agent: Optional[str] = None


class AgentIn(BaseModel):
    name: str
    identity: str
    labels: list[str] = Field(default_factory=list)
    workspace: str = ""
    auto_push: bool = True


class AgentPatch(BaseModel):
    labels: Optional[list[str]] = None
    workspace: Optional[str] = None
    auto_push: Optional[bool] = None
    enabled: Optional[bool] = None


class ReportIn(BaseModel):
    status: str
    result_summary: str = ""
    error: str = ""
    commit_sha: str = ""
    branch: str = ""
    artifacts: list[str] = Field(default_factory=list)


class CommentIn(BaseModel):
    author: str = "me"
    body: str


# ---------------------------------------------------------------- 工具


def add_log(task_id: int, message: str, agent_name: str = "system", level: str = "info") -> None:
    execute(
        "INSERT INTO task_logs (task_id, agent_name, level, message, created_at) VALUES (?,?,?,?,?)",
        (task_id, agent_name, level, message, now()),
    )


def get_task_or_404(task_id: int) -> dict:
    row = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    return row


def form_files(form) -> list[UploadFile]:
    """从 FormData 中取出所有 files 字段。

    注意：request.form() 产出的是 starlette.datastructures.UploadFile，
    它是 fastapi.UploadFile 的**父类**，所以 isinstance(x, fastapi.UploadFile)
    会失败。这里改用 duck typing（判断 filename 属性）。
    """
    try:
        values = form.getlist("files")
    except AttributeError:
        values = form.getall("files")
    return [v for v in values if hasattr(v, "filename") and hasattr(v, "read")]


def save_upload(task_id: int, upload) -> dict:
    safe = re.sub(r"[^\w.\-一-鿿]+", "_", Path(upload.filename or "file.bin").name)
    sub = Path(str(task_id))
    folder = db.UPLOAD_DIR / sub
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{uuid.uuid4().hex[:8]}_{safe}"
    data = upload.file.read()
    stored.write_bytes(data)
    att_id = execute(
        "INSERT INTO attachments (task_id, filename, stored_path, mime, size, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (task_id, upload.filename or safe, str(stored), upload.content_type or "", len(data), now()),
    )
    return {"id": att_id, "filename": upload.filename or safe, "size": len(data)}


# ---------------------------------------------------------------- 项目


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "time": now()}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    rows = query("SELECT * FROM projects ORDER BY sort_order, id")
    out = []
    for r in rows:
        p = row_to_project(r)
        stats = query_one(
            "SELECT COUNT(*) total,"
            " SUM(status='pending') pending, SUM(status IN ('claimed','running')) active,"
            " SUM(status='done') done, SUM(status='failed') failed"
            " FROM tasks WHERE project_id=?",
            (r["id"],),
        )
        p["stats"] = {
            "total": stats["total"] or 0,
            "pending": stats["pending"] or 0,
            "active": stats["active"] or 0,
            "done": stats["done"] or 0,
            "failed": stats["failed"] or 0,
        }
        out.append(p)
    return out


@app.post("/api/projects", dependencies=[Depends(require_admin)])
def create_project(body: ProjectIn) -> dict:
    if query_one("SELECT id FROM projects WHERE name=?", (body.name,)):
        raise HTTPException(status_code=409, detail="project name exists")
    # 新项目排到末尾
    nxt = query_one("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM projects")["n"]
    pid = execute(
        "INSERT INTO projects (name, repo_url, branch, repo_path, description, command,"
        " sort_order, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (body.name, body.repo_url, body.branch, body.repo_path, body.description,
         body.command, nxt, now()),
    )
    return row_to_project(query_one("SELECT * FROM projects WHERE id=?", (pid,)))


@app.patch("/api/projects/{project_id}", dependencies=[Depends(require_admin)])
def update_project(project_id: int, body: ProjectIn) -> dict:
    if not query_one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(status_code=404, detail="project not found")
    execute(
        "UPDATE projects SET name=?, repo_url=?, branch=?, repo_path=?, description=?,"
        " command=? WHERE id=?",
        (body.name, body.repo_url, body.branch, body.repo_path, body.description,
         body.command, project_id),
    )
    return row_to_project(query_one("SELECT * FROM projects WHERE id=?", (project_id,)))


class ProjectReorder(BaseModel):
    ids: list[int]


@app.post("/api/projects/reorder", dependencies=[Depends(require_admin)])
def reorder_projects(body: ProjectReorder) -> dict:
    """按传入的 id 顺序重排项目。id 必须是当前库中项目的完整集合。"""
    current = {r["id"] for r in query("SELECT id FROM projects")}
    incoming = set(body.ids)
    if incoming != current:
        raise HTTPException(
            status_code=400,
            detail=f"id 列表与现有项目不匹配（缺少 {sorted(current - incoming)}，"
                   f"多余 {sorted(incoming - current)}）",
        )
    execute_many(
        "UPDATE projects SET sort_order=? WHERE id=?",
        [(i, pid) for i, pid in enumerate(body.ids, start=1)],
    )
    return {"ok": True, "count": len(body.ids)}


@app.delete("/api/projects/{project_id}", dependencies=[Depends(require_admin)])
def delete_project(project_id: int) -> dict:
    """删除项目。任务表有 ON DELETE CASCADE，其下任务会一并删除。"""
    if not query_one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(status_code=404, detail="project not found")
    n = query_one("SELECT COUNT(*) c FROM tasks WHERE project_id=?", (project_id,))["c"]
    execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"ok": True, "deleted_tasks": n}


# ---------------------------------------------------------------- 任务


@app.get("/api/tasks")
def list_tasks(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(default=200, le=1000),
) -> list[dict]:
    sql = (
        "SELECT t.*, p.name AS project_name FROM tasks t"
        " JOIN projects p ON p.id=t.project_id WHERE 1=1"
    )
    params: list = []
    if project_id:
        sql += " AND t.project_id=?"
        params.append(project_id)
    if status:
        sql += " AND t.status=?"
        params.append(status)
    sql += " ORDER BY t.project_id, t.seq LIMIT ?"
    params.append(limit)
    return [row_to_task(r) for r in query(sql, params)]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict:
    row = query_one(
        "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?",
        (task_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="task not found")
    task = row_to_task(row, with_detail=True)
    for a in task["attachments"]:
        a["url"] = f"/api/attachments/{a['id']}"
    return task


@app.post("/api/tasks", dependencies=[Depends(require_admin)])
async def create_task(request: Request) -> dict:
    """创建任务。支持两种提交方式：

    - application/json：直接传 TaskIn（脚本/命令行调用）
    - multipart/form-data：body 字段是 TaskIn 的 JSON 字符串，可附带 files（浏览器调用）
    """
    content_type = request.headers.get("content-type", "")
    uploads: list[UploadFile] = []
    if "multipart/form-data" in content_type:
        form = await request.form()
        raw = form.get("body")
        payload = loads(raw, None)
        if payload is None:
            raise HTTPException(status_code=400, detail="missing body field")
        uploads = form_files(form)
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}") from exc

    try:
        body = TaskIn.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid body: {exc}") from exc

    proj = query_one("SELECT * FROM projects WHERE id=?", (body.project_id,))
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    nxt = query_one(
        "SELECT COALESCE(MAX(seq),0)+1 AS n FROM tasks WHERE project_id=?", (body.project_id,)
    )["n"]
    task_id = execute(
        "INSERT INTO tasks (project_id, seq, title, description, priority, required_labels,"
        " assigned_agent, branch, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            body.project_id,
            nxt,
            body.title,
            body.description,
            body.priority,
            dumps(body.required_labels),
            body.assigned_agent,
            proj["branch"] or "main",
            now(),
        ),
    )
    for f in uploads:
        save_upload(task_id, f)
    add_log(task_id, f"任务已创建（#{nxt}）", level="info")
    return get_task(task_id)


@app.patch("/api/tasks/{task_id}", dependencies=[Depends(require_admin)])
def patch_task(task_id: int, body: TaskPatch) -> dict:
    get_task_or_404(task_id)
    fields, params = [], []
    for key in ("title", "description", "priority", "status", "assigned_agent"):
        value = getattr(body, key)
        if value is not None:
            fields.append(f"{key}=?")
            params.append(value)
    if body.required_labels is not None:
        fields.append("required_labels=?")
        params.append(dumps(body.required_labels))
    if fields:
        params.append(task_id)
        execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", params)
        add_log(task_id, "任务被人工修改", agent_name="admin")
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(require_admin)])
def retry_task(task_id: int) -> dict:
    get_task_or_404(task_id)
    execute(
        "UPDATE tasks SET status='pending', assigned_agent='', claimed_at='', finished_at='',"
        " error='', retry_count=retry_count+1 WHERE id=?",
        (task_id,),
    )
    add_log(task_id, "任务被重新放回队列", agent_name="admin", level="warn")
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/comments", dependencies=[Depends(require_admin)])
def add_comment(task_id: int, body: CommentIn) -> dict:
    get_task_or_404(task_id)
    cid = execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
        (task_id, body.author, body.body, now()),
    )
    add_log(task_id, f"备注：{body.body}", agent_name=body.author)
    return {"id": cid, "task_id": task_id, "author": body.author, "body": body.body}


@app.get("/api/attachments/{attachment_id}")
def get_attachment(attachment_id: int) -> FileResponse:
    row = query_one("SELECT * FROM attachments WHERE id=?", (attachment_id,))
    if not row or not Path(row["stored_path"]).exists():
        raise HTTPException(status_code=404, detail="attachment not found")
    return FileResponse(row["stored_path"], media_type=row["mime"] or None, filename=row["filename"])


# ---------------------------------------------------------------- agent 注册


@app.get("/api/agents")
def list_agents() -> list[dict]:
    return [row_to_agent(r) for r in query("SELECT * FROM agents ORDER BY id")]


@app.post("/api/agents", dependencies=[Depends(require_admin)])
def create_agent(body: AgentIn) -> dict:
    if query_one("SELECT id FROM agents WHERE name=?", (body.name,)):
        raise HTTPException(status_code=409, detail="agent name exists")
    token = secrets.token_urlsafe(24)
    aid = execute(
        "INSERT INTO agents (name, identity, token, labels, workspace, auto_push, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (body.name, body.identity, token, dumps(body.labels), body.workspace, int(body.auto_push), now()),
    )
    row = query_one("SELECT * FROM agents WHERE id=?", (aid,))
    out = row_to_agent(row)
    out["token"] = token
    return out


@app.patch("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
def patch_agent(agent_id: int, body: AgentPatch) -> dict:
    if not query_one("SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise HTTPException(status_code=404, detail="agent not found")
    if body.labels is not None:
        execute("UPDATE agents SET labels=? WHERE id=?", (dumps(body.labels), agent_id))
    if body.workspace is not None:
        execute("UPDATE agents SET workspace=? WHERE id=?", (body.workspace, agent_id))
    if body.auto_push is not None:
        execute("UPDATE agents SET auto_push=? WHERE id=?", (int(body.auto_push), agent_id))
    if body.enabled is not None:
        execute("UPDATE agents SET enabled=? WHERE id=?", (int(body.enabled), agent_id))
    return row_to_agent(query_one("SELECT * FROM agents WHERE id=?", (agent_id,)))


@app.post("/api/agents/{agent_id}/rotate-token", dependencies=[Depends(require_admin)])
def rotate_token(agent_id: int) -> dict:
    if not query_one("SELECT id FROM agents WHERE id=?", (agent_id,)):
        raise HTTPException(status_code=404, detail="agent not found")
    token = secrets.token_urlsafe(24)
    execute("UPDATE agents SET token=? WHERE id=?", (token, agent_id))
    return {"id": agent_id, "token": token}


# ---------------------------------------------------------------- agent 运行时 API


@app.get("/api/agent/whoami", dependencies=[Depends(auth_agent)])
def whoami(agent: dict = Depends(auth_agent)) -> dict:
    return {"name": agent["name"], "identity": agent["identity"], "labels": loads(agent["labels"], [])}


@app.post("/api/agent/heartbeat")
def heartbeat(agent: dict = Depends(auth_agent)) -> dict:
    busy = query_one(
        "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE t.assigned_agent=? AND t.status IN ('claimed','running')"
        " ORDER BY t.id LIMIT 1",
        (agent["name"],),
    )
    execute("UPDATE agents SET last_seen=? WHERE id=?", (now(), agent["id"]))
    return {"ok": True, "server_time": now(), "current_task": row_to_task(busy) if busy else None}


@app.post("/api/agent/claim")
def claim(agent: dict = Depends(auth_agent)) -> JSONResponse:
    """按身份领取任务。优先级高的先领；同优先级按创建顺序。"""
    if not agent["enabled"]:
        raise HTTPException(status_code=403, detail="agent disabled")

    busy = query_one(
        "SELECT id FROM tasks WHERE assigned_agent=? AND status IN ('claimed','running')", (agent["name"],)
    )
    if busy:
        raise HTTPException(status_code=409, detail="agent already has a running task")

    labels = loads(agent["labels"], [])
    candidates = query(
        "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE t.status='pending' ORDER BY t.priority DESC, t.id ASC LIMIT 100"
    )
    picked = None
    for c in candidates:
        required = loads(c["required_labels"], [])
        if not required:
            picked = c
            break
        if set(required) & set(labels):
            picked = c
            break

    if not picked:
        return JSONResponse({"task": None, "message": "no matching pending task"})

    affected = execute(
        "UPDATE tasks SET status='claimed', assigned_agent=?, claimed_at=?, heartbeat_at=?"
        " WHERE id=? AND status='pending'",
        (agent["name"], now(), now(), picked["id"]),
    )
    if not affected:
        return JSONResponse({"task": None, "message": "task taken by another agent"})
    execute("UPDATE agents SET current_task=? WHERE id=?", (picked["id"], agent["id"]))
    add_log(picked["id"], f"{agent['name']} 领取了任务", agent_name=agent["name"])

    task = get_task(picked["id"])
    task["agent"] = {
        "name": agent["name"],
        "identity": agent["identity"],
        "workspace": agent["workspace"],
        "auto_push": bool(agent["auto_push"]),
    }
    task["project"] = row_to_project(query_one("SELECT * FROM projects WHERE id=?", (picked["project_id"],)))
    return JSONResponse({"task": task})


@app.post("/api/agent/tasks/{task_id}/start")
def start_task(task_id: int, agent: dict = Depends(auth_agent)) -> dict:
    row = get_task_or_404(task_id)
    if row["assigned_agent"] != agent["name"]:
        raise HTTPException(status_code=403, detail="not your task")
    execute(
        "UPDATE tasks SET status='running', heartbeat_at=? WHERE id=?", (now(), task_id)
    )
    add_log(task_id, f"{agent['name']} 开始执行", agent_name=agent["name"])
    return get_task(task_id)


@app.post("/api/agent/tasks/{task_id}/log")
async def agent_log(task_id: int, request: Request, agent: dict = Depends(auth_agent)) -> dict:
    """写执行日志。接受 form 或 JSON。"""
    get_task_or_404(task_id)
    message, level = "", "info"
    if "multipart/form-data" in request.headers.get("content-type", "") or \
       "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        form = await request.form()
        message = str(form.get("message", ""))
        level = str(form.get("level", "info"))
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        message = str(payload.get("message", ""))
        level = str(payload.get("level", "info"))

    execute("UPDATE tasks SET heartbeat_at=? WHERE id=?", (now(), task_id))
    add_log(task_id, message, agent_name=agent["name"], level=level)
    return {"ok": True}


@app.post("/api/agent/tasks/{task_id}/report")
async def report(task_id: int, request: Request,
                 agent: dict = Depends(auth_agent)) -> dict:
    """回写最终状态。status: done / failed / review。

    支持两种提交方式：
    - application/json，直接传 ReportIn
    - multipart/form-data，body 字段是 ReportIn 的 JSON 字符串（同带 files 附件）
    """
    row = get_task_or_404(task_id)
    if row["assigned_agent"] != agent["name"]:
        raise HTTPException(status_code=403, detail="not your task")

    content_type = request.headers.get("content-type", "")
    uploads: list[UploadFile] = []
    if "multipart/form-data" in content_type:
        form = await request.form()
        raw = form.get("body")
        try:
            body = ReportIn.model_validate(loads(raw, None))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid body: {exc}") from exc
        uploads = form_files(form)
    else:
        try:
            body = ReportIn.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid body: {exc}") from exc

    if body.status not in ("done", "failed", "review"):
        raise HTTPException(status_code=400, detail="status must be done/failed/review")

    for f in uploads:
        save_upload(task_id, f)

    execute(
        "UPDATE tasks SET status=?, result_summary=?, error=?, commit_sha=?, branch=?,"
        " finished_at=?, heartbeat_at=? WHERE id=?",
        (body.status, body.result_summary, body.error, body.commit_sha, body.branch,
         now(), now(), task_id),
    )
    execute("UPDATE agents SET current_task=NULL WHERE id=?", (agent["id"],))
    for artifact in body.artifacts:
        add_log(task_id, f"产出：{artifact}", agent_name=agent["name"])
    add_log(
        task_id,
        f"{agent['name']} 提交结果：{body.status}" + (f" — {body.result_summary}" if body.result_summary else ""),
        agent_name=agent["name"],
        level="info" if body.status == "done" else "warn",
    )
    return get_task(task_id)


@app.get("/api/agent/tasks/{task_id}/context")
def task_context(task_id: int, agent: dict = Depends(auth_agent)) -> dict:
    """给 agent 一次性拿走全部图文上下文，避免多次往返。"""
    task = get_task(task_id)
    for a in task["attachments"]:
        a["url"] = f"{os.environ.get('TASKHUB_PUBLIC_URL', '')}/api/attachments/{a['id']}"
    return task


@app.post("/api/agent/tasks/{task_id}/attachments")
async def agent_upload(task_id: int, request: Request,
                       agent: dict = Depends(auth_agent)) -> dict:
    """单独上传附件/产出，不改动任务状态。"""
    get_task_or_404(task_id)
    form = await request.form()
    saved = [save_upload(task_id, f) for f in form_files(form)]
    if saved:
        add_log(task_id, f"{agent['name']} 上传了 {len(saved)} 个文件", agent_name=agent["name"])
    return {"saved": saved}


@app.get("/api/agent/pending")
def agent_pending(agent: dict = Depends(auth_agent)) -> dict:
    """WorkBuddy 定期任务用：只看不领，把符合条件的任务原样取走。

    与 claim 的区别是不改变归属，AI 执行完再走 claim / report 流程，
    这样可以在真正动手前先看到任务全文和附件。
    """
    labels = loads(agent["labels"], [])
    candidates = query(
        "SELECT t.*, p.name AS project_name FROM tasks t JOIN projects p ON p.id=t.project_id"
        " WHERE t.status='pending' ORDER BY t.priority DESC, t.id ASC LIMIT 50"
    )
    mine = []
    for c in candidates:
        required = loads(c["required_labels"], [])
        if not required or set(required) & set(labels):
            mine.append(row_to_task(c))
    return {"agent": agent["name"], "count": len(mine), "tasks": mine}


# ---------------------------------------------------------------- 前端


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")