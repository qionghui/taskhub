# TaskHub · 多电脑多 agent 任务工作台

B/S 架构的任务调度系统。在浏览器里写图文任务并下发到服务器，多台电脑上的 agent  
按各自身份领取任务、执行、提交 git、回写状态。

```
浏览器控制台  ──写──►  TaskHub 服务器  ◄──轮询/回写──  电脑 A agent
(多项目/图文)     ──读──  任务库 + 附件   ◄─────────────  电脑 B agent
```

## 核心概念

| 概念               | 说明                                      |
| ---------------- | --------------------------------------- |
| **项目 project**   | 一个代码仓库。记录仓库地址、分支、各电脑的本地路径、可选的自动命令       |
| **节点 agent**     | 一台执行电脑。用 token 注册，带一组身份标签               |
| **标签 labels**    | 身份标识。任务的 `required_labels` 与节点标签有交集才可领取 |
| **任务 task**      | 项目下的一条工作项，支持 Markdown 描述 + 图片附件         |
| **心跳 heartbeat** | agent 每轮开始时上报，附带"我当前是否正忙"               |

## 任务状态流转

```
pending ──claim──► claimed ──start──► running ──report──► done
   ▲                                                        │
   │                                          ┌─────────────┼──────────┐
   │                                          ▼             ▼          ▼
   └──────────── retry ──────────────────── review      failed   cancelled
                                                  │
                                        人工确认后手动结项
```

- **pending** 待领取 — 队列中等待匹配的节点
- **claimed** 已领取 — 已被某节点锁定，其他节点领不到
- **running** 执行中
- **review** 待验收 — 干了一部分需要人确认，不提交代码
- **done** 已完成 / **failed** 失败 / **cancelled** 已取消

## 目录结构

```
taskhub/
├── server/
│   ├── app.py           REST API 全部接口
│   ├── db.py            SQLite 数据层（WAL 模式，支持并发）
│   └── static/index.html 浏览器控制台（单文件，无构建步骤）
├── agent/
│   ├── agent.py         agent 客户端（轮询 / 执行 / git / 回写）
│   └── agent.example.json
├── docs/
│   ├── workbuddy-schedule-prompt.md   WorkBuddy 定期任务模板
│   └── DEPLOY.md                      服务器部署
├── scripts/start_server.bat
├── e2e_test.py          API 层端到端测试（48 项）
└── e2e_git_test.py      git 闭环测试（29 项）
```

## 本地启动

```bash
pip install -r requirements.txt

# 启动服务端
TASKHUB_ADMIN_TOKEN=taskhub python -m uvicorn server.app:app --host 0.0.0.0 --port 8787
```

浏览器打开 `http://127.0.0.1:8787`，右上角「管理口令」填 `taskhub`。

Windows 可直接跑 `scripts/start_server.bat`。

## 接入一台电脑

1. 控制台点「节点」→「注册节点」，填：
   - 节点名：`win-a`
   - 能力标签：`win-a, delphi, windows` ← **决定它能领哪些任务**
   - 工作区：`D:\work`
2. 注册后会弹出一份配置，复制到该电脑的 `agent.json`：

```json
{
  "server": "http://<服务器IP>:8787",
  "name": "win-a",
  "identity": "win-a",
  "token": "注册时下发的 token",
  "labels": ["win-a", "delphi", "windows"],
  "workspace": "D:\\work",
  "poll_seconds": 30
}
```

1. 该电脑运行：

```bash
pip install requests
python agent/agent.py --config agent.json
```

控制台「节点」页能看到心跳时间和当前任务。

## 两种执行模式

### 模式一：WorkBuddy 定期任务（智能任务）

项目**不填** `command`。agent 领取任务后会把图文上下文落地成  
`<工作区>/.taskhub/task-{id}.md`，状态置为 `review`，等待 WorkBuddy 定期任务接手。

把 `docs/workbuddy-schedule-prompt.md` 的内容复制进 WorkBuddy 定期任务即可。  
关键是它会**用 Read 工具真的打开图片看内容**，而不只是看文件名。

### 模式二：自动命令（确定性任务）

项目填了 `command`，比如 `dotnet build -c Release`。agent 直接跑，  
成功则自动 `git add/commit/push` 并回写 `done`，失败回写 `failed`。

适合构建、打包、跑测试这类不需要 AI 判断的任务。

两种模式可以共存：AI 类任务走模式一，命令类任务走模式二。

## API

### 管理端（需 `X-Admin-Token` header）

| 方法             | 路径                               | 说明                     |
| -------------- | -------------------------------- | ---------------------- |
| GET            | `/api/projects`                  | 项目列表（含任务统计）            |
| POST           | `/api/projects`                  | 新建项目（排到末尾）             |
| PATCH          | `/api/projects/{id}`             | 更新项目                   |
| POST           | `/api/projects/reorder`          | 按传入 id 顺序重排项目          |
| DELETE         | `/api/projects/{id}`             | 删除项目（其下任务一并删除）        |
| GET            | `/api/tasks?project_id=&status=` | 任务列表                   |
| POST           | `/api/tasks`                     | 新建任务（JSON 或 multipart） |
| GET            | `/api/tasks/{id}`                | 任务详情（含附件、日志、备注）        |
| PATCH          | `/api/tasks/{id}`                | 修改任务                   |
| POST           | `/api/tasks/{id}/retry`          | 退回队列重试                 |
| POST           | `/api/tasks/{id}/comments`       | 加备注                    |
| GET            | `/api/attachments/{id}`          | 下载附件                   |
| GET/POST/PATCH | `/api/agents...`                 | 节点管理与 token 下发         |

### Agent 端（需 `X-Agent-Token` header）

| 方法   | 路径                                  | 说明                           |
| ---- | ----------------------------------- | ---------------------------- |
| GET  | `/api/agent/whoami`                 | 确认身份                         |
| POST | `/api/agent/heartbeat`              | 心跳 + 当前任务                    |
| POST | `/api/agent/claim`                  | **领取任务**，按优先级和标签匹配           |
| POST | `/api/agent/pending`                | 只看不领，预览可领任务                  |
| POST | `/api/agent/tasks/{id}/start`       | 开始执行                         |
| POST | `/api/agent/tasks/{id}/log`         | 写日志                          |
| POST | `/api/agent/tasks/{id}/report`      | **回写结果**（done/failed/review） |
| POST | `/api/agent/tasks/{id}/attachments` | 上传产出文件                       |
| GET  | `/api/agent/tasks/{id}/context`     | 取完整图文上下文                     |

## 关键设计

**身份隔离靠标签交集，不靠指定。** 任务 `required_labels` 为空 → 任何节点都能领；  
非空 → 只有标签有交集的节点能领。领取用 `UPDATE ... WHERE status='pending'`  
的原子操作，避免两个节点抢到同一个任务。

**一个节点同时只做一个任务。** claim 时检查是否已有未完成任务，  
有则返回 409。这防止 AI 卡住时堆积任务。

**agent 的工作目录以项目 `repo_path` 为准**，其次才用节点 workspace 拼项目名。  
因为同一个项目在不同电脑上的路径往往不一样。

**`.taskhub/` 自动加进 `.gitignore`**，避免 TaskHub 的内部工作区被提交。

## 测试

```bash
# 需要服务端已启动
python e2e_test.py        # 48 项：API、鉴权、身份隔离、状态流转、看板
python e2e_git_test.py    # 29 项：真实建 git 仓库，跑完整 commit/push 闭环
```

两套测试都可重复运行，不会因残留数据报错。

## 部署到服务器

见 `docs/DEPLOY.md`。
