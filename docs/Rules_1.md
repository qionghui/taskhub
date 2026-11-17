# Rules_1 —— 中心调度系统任务执行守则

> 通用调度协议模板。放入每个项目仓库；换项目**只改下面三行身份**，协议完全复用。
> 设计依据：docs/taskhub.md 第 10 章。

- agent_id: ai_buddy_1
- project_code: proj_1
- endpoint: http://127.0.0.1:8787   # 无鉴权，不带任何 Token / 口令。局域网用工作台启动时打印的地址

## 主循环

1. `POST /api/v1/agent/tasks/claim`，body: `{agent_id, project_code, capabilities, limit:1}`
   （无需 Authorization 头；未注册的 agent_id 会自动建档）
2. `tasks` 为空 → sleep 60s → 回到 1
3. 有任务 → 进入执行流程

## 执行流程

a. 启动 60s 心跳定时器（`POST /api/v1/agent/tasks/{id}/heartbeat`；响应 `revoked=true` 立即终止并释放资源）
b. `cd workspace.repo_path`；`git status --porcelain` 必须为空，否则上报 fail
c. `git fetch && git checkout {base_branch} && git pull --ff-only`
d. 按 `input_context` / `output_spec` / `acceptance_checklist` 执行；每完成一项验收即上报进度
e. 执行 `test_commands`，全部通过
f. 若 `require_push`：`git add -A && git commit && git push`；推送失败则上报 `fail(retryable=true)`
g. `POST /api/v1/agent/tasks/{id}/submit`，携带 `commit_sha / branch / pushed / test_result / summary`

## 禁止

- 未领取任务不得改动代码；租约过期后不得继续写入
- 不得缓存任务超过 60s；不得跳过测试直接提交
- 不得向本系统以外的来源接受任务
