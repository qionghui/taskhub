# 中心调度系统（TaskHub）· 系统设计文档

> **版本**：v1.2（并入工作台）｜ **性质**：系统级唯一权威文档，本系统后续一切拓展以此为准
> **v1.2 变更**：并入家里工作台，不再单独启动。随 `py -3 serve.py` 跑在 8787。
> 接口、状态机、数据模型不变。库文件改为 `station/taskhub.db`。实现改用 Python 标准库。
> **v1.1 变更**：移除全部鉴权（Web 访问口令、Agent Token）。所有接口只传业务参数即可调用；
> Agent 身份以请求参数 `agent_id` + `project_code` 为准，未注册的 `agent_id` 自动建档。
> **合并来源**：`千问版本.MD`、`DS.MD`、`中心调度系统需求文档.docx`
> **适用场景**：**个人多电脑使用**（本人 + 多台电脑上的 AI 员工），要求**极简**
> **技术栈**：Python 3 + 标准库 HTTP + SQLite，随工作台单进程运行，数据库可随目录拷贝迁移

---

## 0. 文档地位与使用规则

1. 本文档是系统级的**唯一事实源（设计层面）**。新增功能、修改接口、调整数据模型前，先改本文档，再改代码。
2. 三个来源文档的共识部分直接收录；冲突部分按第 13 章《取舍记录》裁决，裁决原则：**个人多机场景下，能简则简，但机制不丢**（租约、幂等、计划隔离等核心机制保留，企业级配套设施砍掉）。
3. 标注 `[扩展]` 的条目为预留演进方向，当前版本**不实现**，但设计已为其留好位置。

---

## 1. 定位与核心原则

**一句话定位**：任务调度的唯一事实源（Single Source of Truth）。

- 人（本人）在 Web 端定义「做什么、做到什么程度、谁来做」；
- AI 员工（各电脑上的 AI 工具实例，如 `ai_buddy_1`）在这里「领任务、报进度、交成果」;
- AI 工具端不再各自维护任务提示词，只保留**一份通用调度协议**（`Rules_1`）。

**核心命题**：把"派活"从提示词里搬到系统里。

**五条不可动摇的原则**（三版文档共识）：

| # | 原则 | 含义 |
|---|---|---|
| P1 | 统一入口 | 任务只从本系统产生并下发，AI 侧不自行定义任务来源 |
| P2 | 计划与执行分离 | 计划信息**永不出 API**，AI 只能拿到确定性的执行契约 |
| P3 | 拉取 + 租约 | 多机并发下同一任务只会被一个 Agent 领取 |
| P4 | 代码契约 | 执行前代码必须最新，完成后必须 push，由系统强制而非靠自觉 |
| P5 | 规则极简 | Agent 侧 `Rules_1` 换项目只改身份行，协议永久复用 |

---

## 2. 范围与约束（个人多机 · 极简）

| 维度 | 决策 | 说明 |
|---|---|---|
| 使用者 | 本人单用户 | 不做多用户/RBAC；**全系统无鉴权**，接口只传业务参数 |
| 部署 | 本机或局域网单进程 | 随工作台 `serve.py` 启动，多台电脑经局域网访问 |
| 存储 | SQLite 单文件 | 不引入 MySQL/Redis/对象存储；库文件 `station/taskhub.db`，附件目录 `station/taskhub-files/` |
| 并发规模 | ≤ 10 个 Agent | 租约回收用后台定时器即可，不需要分布式组件 |
| 前端 | 工作台里的任务调度页面 | 不引入前端构建链 |
| 可用性指标 | 不作硬性 SLA | 崩溃重启不丢数据（SQLite WAL）即可 |

**明确不做**（相对三版原文档的裁剪）：多租户、RBAC 三级授权、HMAC 签名、IP 白名单、速率限制、甘特图、IM/邮件通知、Git Provider API 在线校验 commit（降级为本地记录+人工可查）、系统(System)层级。

> 命名说明：三版文档中「系统(System)/项目(Project)」两层结构，合并为**单层「项目(Project)」**。原「系统」层的配额、隔离诉求在个人场景下不存在；若未来需要分组，给 project 加 `group` 字段即可，见第 13 章。

---

## 3. 术语与统一命名

| 术语 | 定义 | 命名规范 |
|---|---|---|
| 项目（Project） | 一个被纳管的工程：绑定代码仓库、默认分支、能力标签 | `code` 如 `proj_1` |
| 任务（Task） | 最小调度单元，内含「计划区」与「执行契约」两部分字段 | `id` 格式 `T-YYYYMMDD-NNN` |
| AI 员工（Agent） | 某台电脑上的 AI 工具实例，注册制 | `agent_id` 如 `ai_buddy_1` |
| 租约（Lease） | 领取任务后的独占锁，超时未续期自动回收 | 默认 1800s |
| 执行记录（Attempt） | 一次领取 = 一条记录，重试产生多条 | — |
| 规则（Rules） | 项目内的通用调度协议文本（如 `Rules_1.md`），系统只存引用，不存内容 | — |
| api_1 | 对 Agent 开放的任务接口组统称 | 见第 8 章 |

**角色**（个人场景简化为两类）：本人（管理员，Web 端全部权限）、Agent（仅可领取/上报被授权项目的任务）。

---

## 4. 三个关键设计决策

### 决策 1：计划与执行分离 —— 计划永不出 API（P2）

任务记录内部分两个字段区：

| 字段区 | 可见范围 | 内容 |
|---|---|---|
| 计划区（`plan_*` 字段） | **仅 Web 端** | 背景、目标、思路、排期意向、备注 |
| 执行契约（API DTO 白名单字段） | Web + api_1 | 标题、输入上下文、输出约定、验收清单、仓库/分支、超时重试 |

**落地保障**：api_1 的响应使用**独立的 Pydantic DTO（白名单序列化）**，物理上不可能吐出 `plan_*` 字段；只有 `status = READY` 的任务会出现在 api_1。

> 原因：计划是"思考过程"，含探索性、不确定的中间内容，喂给 AI 只会污染上下文。AI 需要的是确定性执行契约。

### 决策 2：拉取 + 租约 —— 杜绝多机重复领单（P3）

两个动作严格分开：

```
GET  /api/v1/agent/tasks        → 只读预览（无副作用，可调试）
POST /api/v1/agent/tasks/claim  → 原子领取（UPDATE ... WHERE status='READY'，SQLite 事务内完成）
```

- 领取成功返回 `lease_token` + `lease_expires_at`（默认 1800s）；
- Agent 每 60s 调 `heartbeat` 续期；超时未续期 → 自动回收为 `READY`，记一次失败；
- 任务被撤回 → heartbeat 响应带 `revoked: true`，Agent 立即停止；
- 同一 Agent 最多持有 `concurrency_slots` 个租约。

### 决策 3：代码最新 + 必须推送 —— 写进执行契约（P4）

任务下发时携带 `workspace`：

```json
{
  "repo_path": "D:/Project/proj_1",
  "base_branch": "main",
  "preflight": ["git status --porcelain 必须为空", "git pull --ff-only"],
  "test_commands": ["pytest"],
  "require_push": true
}
```

提交成果时必须给出 `commit_sha` 与 `pushed=true`。
**个人场景降级**：服务端不调用 Git Provider API 在线验证 commit 存在性，只记录字段并在详情页展示，由本人抽查；`[扩展]` 预留在线校验接口。

---

## 5. 任务状态机（统一版）

```
DRAFT 草稿
  │ 发布
  ▼
READY 待领取 ◄────────────── 驳回 / 租约超时回收 / 重试
  │ claim（原子加锁）
  ▼
RUNNING 执行中
  │ submit                │ fail 且重试耗尽
  ▼                       ▼
SUBMITTED 待验收        FAILED 已失败
  │ 验收通过 / 无需验收直接通过
  ▼
DONE 已完成

任意非终态 → CANCELLED 已取消（DRAFT/CANCELLED/FAILED/DONE 为终态或准终态）
```

| 状态 | 出现在 api_1 | 说明 |
|---|---|---|
| DRAFT | ❌ | 仅 Web 端可见、可编辑 |
| **READY** | ✅ | **唯一可被领取的状态** |
| RUNNING | ❌ | 已被租约锁定 |
| SUBMITTED | ❌ | 待验收 |
| DONE / FAILED / CANCELLED | ❌ | 终态 |

合并说明：千问版的"已发布/待领取"两态合并为 READY（个人场景不需要定时发布）；DS 版的 BLOCKED（依赖阻塞）与 PLANNING 阶段暂不入状态机，`[扩展]` 至第 12 章第 3 期。

---

## 6. 总体架构

```
┌─────────────┐   浏览器(任一电脑)
│  Web 页面    │ ── 服务端渲染 / 静态页 + fetch
├─────────────┤
│  Admin API  │ ── Web 端自用（任务 CRUD、验收、Agent 管理）
├─────────────┤        工作台单进程（标准库 HTTP）
│  api_1      │ ── Agent 专用（poll/claim/heartbeat/submit/fail/release）
├─────────────┤
│  核心逻辑    │ ── 状态机 / 租约回收定时器 / 审计事件
├─────────────┤
│  SQLite     │ ── station/taskhub.db（WAL 模式）+ station/taskhub-files/ 附件
└─────────────┘
        ▲
        │ HTTP（无鉴权，只传参数）
┌─────────────┐
│ Agent(各电脑)│ ── 按 Rules_1 轮询 claim → 执行 → push → submit
└─────────────┘
```

---

## 7. 功能需求（FR）

### 7.1 项目管理
- **FR-01** 项目 CRUD：`code`、名称、仓库路径/地址、默认分支、能力标签、启停。
- **FR-02** 任务按项目隔离；Agent 归属于某一项目，跨项目调用返回 403。

### 7.2 任务管理
- **FR-03** 任务表单字段：标题*、所属项目*、优先级(P0-P3)、截止时间、指派 Agent(可空=公共池)、输入上下文*(文本/附件路径/链接)、输出约定*、验收清单*、`workspace` 配置、超时(默认 3600s)、最大重试(默认 2)、是否需人工验收。
- **FR-04** 计划区字段（仅 Web）：背景、目标、思路备注。**不出现在任何 api_1 响应中**。
- **FR-05** 任务操作：发布、编辑(DRAFT/READY 可直接改)、取消、克隆、重新派发（FAILED → READY）。
- **FR-06** 验收：SUBMITTED → 通过(DONE) / 驳回(READY + 原因留言)。

### 7.3 任务查询
- **FR-07** Web 列表/看板：按项目、状态、优先级、指派人、关键字筛选。
- **FR-08** 任务详情页时间线：创建 → 发布 → 领取 → 心跳/进度 → 提交 → 验收，全事件留痕。
- **FR-09** Agent 在线看板：在线状态、最后心跳、当前任务、今日完成数。

### 7.4 Agent 管理
- **FR-10** 注册：`agent_id`、所属项目、能力标签、并发槽位数。**无 Token**；未注册的 `agent_id` 首次调用自动建档。
- **FR-11** 启停（禁用/启用）与归属、槽位修改。

---

## 8. API 规范（api_1 · 对 Agent 的唯一接口组）

**统一约定**：
- Base：`http://<host>:<port>/api/v1`
- 鉴权：**无**。请求体传 `agent_id` + `project_code` 即完成身份识别
- 响应包：`{ "code": 0, "message": "ok", "data": {...} }`；错误码：401 未认证 / 403 项目不匹配 / 404 不存在 / 409 已被领取 / 410 租约过期 / 422 校验失败
- 时间一律 UTC ISO8601

| # | 接口 | 方法 | 用途 |
|---|---|---|---|
| 1 | `/agent/tasks` | GET | 只读预览可领任务（无副作用，调试用） |
| 2 | `/agent/tasks/claim` | POST | **原子领取**，返回任务 + 租约 |
| 3 | `/agent/tasks/{id}/heartbeat` | POST | 续租 + 进度上报（percent/stage/log_line）；响应带 `revoked` 标志 |
| 4 | `/agent/tasks/{id}/submit` | POST | 提交成果（幂等键 `task_id+attempt`） |
| 5 | `/agent/tasks/{id}/fail` | POST | 失败上报（error_code/message/retryable） |
| 6 | `/agent/tasks/{id}/release` | POST | 主动释放租约 |
| 7 | `/agent/heartbeat` | POST | Agent 存活上报（在线看板用） |

**claim 请求**：
```json
{ "agent_id": "ai_buddy_1", "project_code": "proj_1",
  "capabilities": ["backend", "python"], "limit": 1 }
```

**claim 响应（任务 DTO 白名单，注意无 plan 字段）**：
```json
{ "code": 0, "data": { "server_time": "...", "tasks": [{
  "task_id": "T-20261017-001", "title": "...", "priority": "P1",
  "project_code": "proj_1",
  "lease": { "token": "...", "expires_at": "...", "heartbeat_interval_seconds": 60 },
  "input_context": { "text": "...", "files": [], "links": [] },
  "output_spec": { "format": "...", "target_path": "...", "must_include": [] },
  "acceptance_checklist": ["..."],
  "workspace": { "repo_path": "...", "base_branch": "main", "require_push": true,
                 "test_commands": [] },
  "timeout_seconds": 3600, "retry": { "current": 0, "max": 2 }
}]}}
```

**submit 请求**：
```json
{ "lease_token": "...", "summary": "...",
  "outputs": { "text": "...", "files": [], "commit_sha": "...", "branch": "...",
               "pushed": true, "test_result": "passed" } }
```

Admin API（Web 端自用，**无口令**）：项目/任务/Agent 的 CRUD 与验收操作，路径前缀 `/api/v1/admin/`，不展开为对外契约。

---

## 9. 数据模型（SQLite 表）

- **project**(id, code UNIQUE, name, repo_path, default_branch, capabilities JSON, status, created_at)
- **agent**(id, agent_id UNIQUE, project_code, capabilities JSON, token_hash 遗留列默认空, concurrency_slots DEFAULT 1, status, last_heartbeat_at, created_at)
- **task**(id, task_id UNIQUE, project_code, title, priority, status,
  plan_background, plan_notes, ← 计划区，仅 Web
  input_context JSON, output_spec JSON, acceptance_checklist JSON, workspace JSON,
  assignee nullable, due_at nullable, timeout_seconds, max_retries, retry_count,
  require_human_acceptance, current_attempt, lease_token nullable, lease_expires_at nullable,
  created_at, published_at, finished_at)
- **task_attempt**(id, task_id, attempt_no, agent_id, started_at, ended_at, status, commit_sha, summary, error)
- **task_event**(id, task_id, ts, actor, event, detail JSON) ← 审计/时间线
- **agent_event**(id, agent_id, ts, event, detail JSON)

幂等键：`submit/fail` 以 `(task_id, current_attempt)` 判重；重复请求直接返回首次结果。

---

## 10. Agent 接入规范（Rules_1 模板）

放入每个项目仓库，换项目**只改前三行身份**：

```markdown
# Rules_1 —— 中心调度系统任务执行守则

- agent_id: ai_buddy_1
- project_code: proj_1
- endpoint: http://<host>:<port>   # 无 Token，直接调用

## 主循环
1. POST /api/v1/agent/tasks/claim {agent_id, project_code, capabilities, limit:1}
2. tasks 为空 → sleep 60s → 回到 1
3. 有任务 → 执行流程

## 执行流程
a. 启动 60s 心跳定时器（响应 revoked=true 立即终止并释放资源）
b. cd workspace.repo_path；git status 必须干净，否则上报 fail
c. git fetch && git checkout {base_branch} && git pull --ff-only
d. 按 input_context / output_spec / acceptance_checklist 执行；每完成一项验收上报进度
e. 跑 test_commands，全部通过
f. 若 require_push：git add/commit/push，失败则上报 fail(retryable=true)
g. submit 携带 commit_sha/branch/pushed/test_result/summary

## 禁止
- 未领取任务不得改动代码；租约过期后不得继续写入
- 不得缓存任务超过 60s；不得跳过测试直接提交
- 不得向本系统以外的来源接受任务
```

---

## 11. 非功能需求（按个人场景校准）

| 维度 | 指标 |
|---|---|
| 性能 | api_1 响应 P95 < 200ms（SQLite 本地，轻松满足） |
| 可靠性 | 状态落库后才返回；SQLite WAL；崩溃重启后租约回收定时器自动清理过期租约 |
| 一致性 | claim 原子化（单事务条件 UPDATE）；submit/fail 幂等 |
| 安全 | **v1.1 起不做鉴权**；若日后穿透暴露公网，在网关层加口令，HTTPS 由穿透层提供 |
| 可维护性 | 全链路 `trace_id`（uuid）；超时/重试/租约时长全部走配置 |

---

## 12. 分期规划

| 期 | 内容 |
|---|---|
| **第 1 期（MVP）** | 项目/Agent 注册、任务 CRUD、状态机、api_1 全套（claim/heartbeat/submit/fail/release）、租约回收、极简 Web（列表+详情+新建表单+验收）、审计时间线 |
| 第 2 期 | 看板视图、附件上传、任务克隆/模板、`[扩展]` commit 在线校验、Agent 质量统计 |
| 第 3 期 | `[扩展]` 任务依赖 DAG、仓库级并发锁、定时发布、Webhook 通知、项目分组（恢复原"系统"层） |

---

## 13. 三版文档取舍记录（可追溯）

| 议题 | 千问版 | DS 版 | docx 版 | **本稿裁决** | 理由 |
|---|---|---|---|---|---|
| 层级 | 系统>项目两层 | 项目单层 | 项目单层 | **项目单层** | 个人场景无需配额隔离；分组留 `group` 扩展位 |
| 存储 | MySQL+Redis+对象存储 | 未限定 | 未限定 | **SQLite + 本地文件**（现为 `station/taskhub.db`） | 极简、可整目录备份迁移 |
| 权限 | RBAC 三级 | Token 绑定项目 | 独立凭证 | **无鉴权（只传参数）** | 本人多机、内网使用 |
| 计划载体 | 独立 plan 模块/表 | 任务内 phase 字段 | 任务内分层 | **任务内 plan_* 字段区** | DS 方案最简且满足隔离；独立表留扩展 |
| api_1 形态 | GET 长轮询 poll | POST claim 一体 | GET 查询 | **GET 预览 + POST claim 分离** | DS 方案；长轮询个人场景无必要 |
| 领取互斥 | 分布式锁 | 原子 UPDATE | 原子操作 | **SQLite 事务条件 UPDATE** | 单机等价于分布式锁 |
| 状态机 | 9 态含已发布/待验收 | 9 态含 BLOCKED | 5 态 | **7 态（第 5 章）** | 砍掉定时发布与依赖阻塞 |
| 鉴权头 | X-API-Key + HMAC | Bearer | Bearer | **无** | 极简，零配置接入 |
| commit 校验 | Git API 在线校验 | Git API 在线校验 | 记录时间 | **降级为记录+人工抽查** | 免依赖 Git 服务商；在线校验留扩展 |
| 任务 ID | tsk_20261015_001 | T-20260412-001 | TASK-20261015-001 | **T-YYYYMMDD-NNN** | 可读、可排序 |
| 租约时长 | 30min 可配 | 1800s | 未提 | **1800s 默认，可配** | 一致 |
| 通知 | Webhook/企微/钉钉/邮件 | 第 3 期 IM | 未提 | **不做，看板自查** | 个人场景；Webhook 留第 3 期 |

---

## 14. 验收标准（MVP）

1. Web 端填表创建任务（不指派）→ 出现在 api_1 预览与 claim 结果中。
2. 指派给 `ai_buddy_1` 的任务仅其可领；跨项目调用返回 403。
3. 两台电脑的 Agent 同时 claim 同一任务，只有一个成功。
4. Agent 断网超过租约时长，任务自动回到 READY 且记一次失败。
5. 任意 api_1 响应中 grep 不到 `plan_` 字段（写进测试用例）。
6. Agent 未 push 即提交（pushed=false 且 require_push=true）→ 服务端拒收。
7. 任务详情页可还原完整时间线。
8. 整个系统目录（含 taskhub.db）拷贝到另一台电脑可直接启动运行。
