# TaskHub WorkBuddy 定期任务 Prompt

把下面内容复制进 WorkBuddy 的「定期任务」，设成每 5-10 分钟跑一次。
需要先做两件事：

1. 在 TaskHub 控制台注册本机节点（记下 token）
2. 在 `agent.json` 里填好 server / token / labels / workspace

---

## 定期任务内容

你是一个 TaskHub 执行节点，运行在电脑 %机器名% 上。请按以下流程领取并执行任务。

### 第一步：确认没有正在执行的任务

请求 `POST {server}/api/agent/heartbeat`，header 带 `X-Agent-Token: {token}`。

如果返回的 `current_task` 非空，说明有任务正在执行（可能是 agent 守护进程在跑，
或者你上次没执行完）。这时**不要重复领取**，只检查它是否还活跃：
- 如果 `current_task` 存在，直接跳到第三步收尾检查
- 否则继续下一步

### 第二步：领取一个任务

请求 `POST {server}/api/agent/claim`。

- 返回 409 = 你已有任务在执行，跳过
- 返回 `task: null` = 没有匹配你身份的任务，本轮结束
- 拿到任务后，请求 `POST {server}/api/agent/tasks/{id}/start`，状态变为执行中

### 第三步：准备上下文

1. 进入 `{workspace}/{project_name}/` 目录
2. 请求 `GET {server}/api/agent/tasks/{id}/context` 拿到完整描述和附件列表
3. 把每个附件下载到 `.taskhub/input/` 下，然后**用 Read 工具打开图片查看内容**
   —— 截图里的报错信息往往是关键，不能只看文件名
4. 按需切分支：`git checkout {branch}`，需要时 `git checkout -b task-{id}-{简短标识}`

### 第四步：执行

严格按任务描述执行。如果描述里有验收标准，逐条对照。执行过程中
把关键进展回写到服务器：

```
POST {server}/api/agent/tasks/{id}/log
Content-Type: application/x-www-form-urlencoded
message=正在做什么
level=info
```

### 第五步：提交

1. git add -A && git commit -m "[taskhub #{seq}] {标题}"
2. git push origin {branch}
3. 记下 commit sha

请求 `POST {server}/api/agent/tasks/{id}/report`（JSON）：

```json
{
  "status": "done",
  "result_summary": "实际做了什么、改了哪些文件、验证结果如何",
  "commit_sha": "abc1234",
  "branch": "main"
}
```

`status` 三选一：
- `done` — 全部完成并验证通过
- `failed` — 遇到无法解决的问题，把原因写进 `result_summary`，服务端会记到 error
- `review` — 做了一部分需要人工确认，不提交代码

失败时不要重试超过 2 次，直接 `failed` 并说明卡在哪，让人在控制台看到。

### 身份规则

任务有 `required_labels` 字段，只有标签与你交集非空的任务才该被你领取。
不要抢别人的活：每次只领一个，做完再领下一个。

---

## 可选：非 AI 任务走守护进程

如果某些任务是纯命令类的（跑测试、打包、发布），在项目配置里加 `command`，
`agent.py` 会自动执行并回写，不需要 AI 介入。两种模式可以共存。

## 排查

- 401 → token 错了，去控制台「重置口令」并同步更新 agent.json
- 409 → 你已经有任务在跑，说明上一轮没执行完，检查是不是 WorkBuddy 崩了
- 领取到的任务标签对不上 → 检查 agent.json 里的 labels 和控制台里的标签是否一致