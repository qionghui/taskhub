"""端到端验证：建项目 -> 注册两个节点 -> 下发图文任务 -> 按身份领取 -> 回写。

用法：python e2e_test.py
需要服务端已在 127.0.0.1:8787 运行。

每次运行会自建独立项目和节点（名字带时间戳），不影响库里的其他数据。
"""

import io
import json
import os
import sys
import time

import requests

BASE = "http://127.0.0.1:8787"
ADMIN = {"X-Admin-Token": "taskhub"}
STAMP = str(int(time.time()))

# 1x1 透明 PNG，模拟用户粘贴的截图
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "0005570bf2b40000000049454e44ae426082"
)

ok, fail = 0, 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")


print("\n[1] 创建项目")
p = requests.post(f"{BASE}/api/projects", headers=ADMIN, json={
    "name": f"ne-word-addin-{STAMP}", "repo_url": "https://github.com/example/repo.git",
    "branch": "main", "repo_path": "", "description": "引用管理插件",
}).json()
check("项目已创建", p.get("id") is not None, p)
pid = p["id"]

print("\n[2] 注册两个节点（模拟电脑 A / B）")
# 用带时间戳的专属标签，避免领到库里其他项目的任务
LA, LB = f"e2e-a-{STAMP}", f"e2e-b-{STAMP}"
a = requests.post(f"{BASE}/api/agents", headers=ADMIN, json={
    "name": f"win-a-{STAMP}", "identity": "win-a", "labels": [LA, "delphi"],
    "workspace": "e2e_ws",
}).json()
b = requests.post(f"{BASE}/api/agents", headers=ADMIN, json={
    "name": f"win-b-{STAMP}", "identity": "win-b", "labels": [LB, "web"],
    "workspace": "e2e_ws",
}).json()
check("节点 A 注册成功", bool(a.get("token")), a)
check("节点 B 注册成功", bool(b.get("token")), b)
check("重名节点被拒绝",
      requests.post(f"{BASE}/api/agents", headers=ADMIN,
                    json={"name": a["name"], "identity": "x"}).status_code == 409)

NAME_A, NAME_B = a["name"], b["name"]
HA = {"X-Agent-Token": a["token"]}
HB = {"X-Agent-Token": b["token"]}

print("\n[3] 身份认证")
check("无 token 被拒", requests.post(f"{BASE}/api/agent/claim").status_code == 401)
check("错误 token 被拒", requests.post(f"{BASE}/api/agent/claim",
      headers={"X-Agent-Token": "wrong"}).status_code == 401)
check("正确 token 识别身份",
      requests.get(f"{BASE}/api/agent/whoami", headers=HA).json()["name"] == NAME_A)

print("\n[4] 创建图文任务")
files = [("files", ("error-screenshot.png", io.BytesIO(PNG), "image/png"))]
r = requests.post(f"{BASE}/api/tasks", headers=ADMIN,
                  data={"body": json.dumps({
                      "project_id": pid, "title": "修复 DoGetData 引用丢失",
                      "description": "## 背景\n引用数据在特定流程下丢失\n\n## 验收\n- [ ] 不再丢失",
                      "priority": 10, "required_labels": [LA], "assigned_agent": ""})},
                  files=files)
t = r.json()
check("任务已创建", t.get("id") is not None, t)
check("任务号为 #1", t["seq"] == 1, t.get("seq"))
check("附件已上传", len(t.get("attachments", [])) == 1, t.get("attachments"))
check("初始状态 pending", t["status"] == "pending")
check("附件可访问", requests.get(f"{BASE}{t['attachments'][0]['url']}").status_code == 200)

# 第二个任务给 B，验证身份隔离
r2 = requests.post(f"{BASE}/api/tasks", headers=ADMIN, json={
    "project_id": pid, "title": "调整官网样式", "description": "B 的活",
    "required_labels": [LB], "priority": 1,
}).json()
check("B 的任务已创建（纯 JSON 建任务）", r2.get("seq") == 2, r2)

print("\n[5] 按身份领取（关键隔离验证）")
ra = requests.post(f"{BASE}/api/agent/claim", headers=HA)
check("A 领到任务", ra.status_code == 200, ra.text[:200])
ta = ra.json().get("task")
check("A 领到的是 #1（标签匹配）", ta and ta["seq"] == 1, ta and ta["seq"])
check("A 拿到了任务描述", bool(ta and ta["description"]))
check("A 拿到了附件上下文", ta and len(ta["attachments"]) == 1)
check("A 拿到了项目仓库信息", ta and ta["project"]["repo_url"].endswith("repo.git"))

check("A 重复领取被拒", requests.post(f"{BASE}/api/agent/claim", headers=HA).status_code == 409)

rb = requests.post(f"{BASE}/api/agent/claim", headers=HB)
tb = rb.json().get("task")
check("B 领到的是 #2", tb and tb["seq"] == 2, tb and tb["seq"])
check("B 没有拿到 A 的任务", tb and "delphi" not in ta["project"]["name"])

print("\n[6] 状态流转")
requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/start", headers=HA)
cur = requests.get(f"{BASE}/api/tasks/{ta['id']}").json()
check("状态变为 running", cur["status"] == "running", cur["status"])
check("执行者记录为本机节点", cur["assigned_agent"] == NAME_A, cur["assigned_agent"])

requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/log", headers=HA,
              data={"message": "定位到 fallback 分支丢失 Variables 数据", "level": "info"})
requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/log", headers=HA,
              data={"message": "编译报错一次", "level": "warn"})
det = requests.get(f"{BASE}/api/tasks/{ta['id']}").json()
check("日志已记录", len([l for l in det["logs"] if l["agent_name"] == NAME_A]) >= 3, len(det["logs"]))
check("warn 级别保留", any(l["level"] == "warn" for l in det["logs"]))

print("\n[7] 越权保护")
check("B 不能 start A 的任务",
      requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/start", headers=HB).status_code == 403)
check("B 不能 report A 的任务",
      requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/report", headers=HB,
                    json={"status": "done"}).status_code == 403)

print("\n[8] 完成回写")
rep = requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/report", headers=HA, json={
    "status": "done", "result_summary": "修复了 Code.Text 回退逻辑", "commit_sha": "abc1234def",
    "branch": "main",
})
check("回写成功", rep.status_code == 200, rep.text[:200])
fin = requests.get(f"{BASE}/api/tasks/{ta['id']}").json()
check("状态为 done", fin["status"] == "done", fin["status"])
check("结果摘要已存", "回退逻辑" in fin["result_summary"], fin["result_summary"])
check("commit sha 已存", fin["commit_sha"].startswith("abc1234"))
check("完成时间已记录", bool(fin["finished_at"]))

print("\n[9] 附件上传与看板统计")
up = requests.post(f"{BASE}/api/agent/tasks/{ta['id']}/attachments", headers=HA,
                   files=[("files", ("build.log", io.BytesIO(b"build ok"), "text/plain"))])
check("产出文件已上传", up.status_code == 200 and len(up.json()["saved"]) == 1)
check("附件数变为 2", len(requests.get(f"{BASE}/api/tasks/{ta['id']}").json()["attachments"]) == 2)

stats = requests.get(f"{BASE}/api/projects").json()
proj_stats = next(x for x in stats if x["id"] == pid)["stats"]
check("看板 done 计数为 1", proj_stats["done"] == 1, proj_stats)
check("看板 total 为 2", proj_stats["total"] == 2, proj_stats)
check("看板 active 为 1（B 已领取未完成）", proj_stats["active"] == 1, proj_stats)

print("\n[10] 待验收状态 + 人工重试")
requests.post(f"{BASE}/api/agent/tasks/{tb['id']}/report", headers=HB,
              json={"status": "review", "result_summary": "改了样式，请确认"})
check("B 的任务进入 review",
      requests.get(f"{BASE}/api/tasks/{tb['id']}").json()["status"] == "review")
rr = requests.post(f"{BASE}/api/tasks/{tb['id']}/retry", headers=ADMIN).json()
check("重试后回到 pending", rr["status"] == "pending", rr["status"])
check("重试计数为 1", rr["retry_count"] == 1)
check("重试后清除执行者", rr["assigned_agent"] == "")

print("\n[11] B 现在能领到重试的任务")
rb2 = requests.post(f"{BASE}/api/agent/claim", headers=HB).json().get("task")
check("B 重新领到 #2", rb2 and rb2["seq"] == 2, rb2 and rb2["seq"])

print("\n[12] 管理端鉴权")
check("无口令不能建项目",
      requests.post(f"{BASE}/api/projects", json={"name": "hack"}).status_code == 401)
check("无口令不能建任务",
      requests.post(f"{BASE}/api/tasks", json={"project_id": pid, "title": "x"}).status_code == 401)

print("\n[13] 心跳与 pending 预览接口")
hb = requests.post(f"{BASE}/api/agent/heartbeat", headers=HB).json()
check("心跳返回当前任务", hb["current_task"] and hb["current_task"]["seq"] == 2, hb.get("current_task"))
pend = requests.get(f"{BASE}/api/agent/pending", headers=HB).json()
check("pending 预览可用（不改变归属）", "count" in pend, pend)

print("\n[14] 前端页面")
check("控制台页面可访问", requests.get(f"{BASE}/").status_code == 200)
check("页面是 TaskHub", "TaskHub" in requests.get(f"{BASE}/").text)

print(f"\n{'='*46}\n通过 {ok} / 失败 {fail}\n{'='*46}\n")
sys.exit(1 if fail else 0)