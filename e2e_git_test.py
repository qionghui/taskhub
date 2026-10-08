"""验证 agent.py 真实执行闭环。

流程：创建真实 git 仓库 -> 下发任务 -> 启动 agent.py --once ->
检查 agent 是否真的执行了 command、提交、推送、回写状态。

会建两个本地裸仓库模拟 GitHub 远端。
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8787"
ADMIN = {"X-Admin-Token": "taskhub"}
ROOT = Path(__file__).parent / "e2e_git"
AGENT = Path(__file__).parent / "agent" / "agent.py"
STAMP = str(int(time.time()))
GLABEL = f"git-{STAMP}"
REPO = f"proj-{STAMP}"

ok, fail = 0, 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label} {extra}")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def setup_repo(name: str, branch: str) -> Path:
    """建本地裸仓库当远端 + 一个工作克隆。

    调用方传入带时间戳的 name，保证每轮都是全新目录，无需清理。
    """
    remote = ROOT / f"{name}-remote.git"
    work = ROOT / name
    remote.mkdir(parents=True, exist_ok=True)
    git(remote, "init", "--bare")
    work.mkdir(parents=True, exist_ok=True)
    git(work, "init")
    git(work, "config", "user.email", "agent@test.local")
    git(work, "config", "user.name", "TaskHub Agent")
    git(work, "remote", "add", "origin", str(remote))
    (work / "README.md").write_text("# init\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    git(work, "branch", "-M", branch)
    git(work, "push", "-u", "origin", branch)
    return work


print("\n[1] 准备 git 环境")
# 复用上次残留的目录（setup_repo 内部会重置），不删除整个目录树
ROOT.mkdir(parents=True, exist_ok=True)
check("git 可用", git(ROOT, "--version").returncode == 0)
work = setup_repo(REPO, "main")
REMOTE = ROOT / f"{REPO}-remote.git"
check("仓库初始化并推送到远端", (REMOTE / "refs" / "heads" / "main").exists())
remote_before = git(REMOTE, "rev-parse", "main").stdout.strip()

print("\n[2] 注册项目（配置自动执行命令）和节点")
# command 在工作目录里写一个文件，验证 agent 真的执行了
cmd = 'python -c "open(\'result.txt\',\'w\',encoding=\'utf-8\').write(\'built ok\')"'
proj = requests.post(f"{BASE}/api/projects", headers=ADMIN, json={
    "name": f"gitproj-{STAMP}", "repo_url": str(REMOTE),
    "branch": "main", "repo_path": str(work), "command": cmd,
    "description": "git 闭环测试",
}).json()
check("项目已创建", proj.get("id") is not None, proj)
check("command 已保存", proj.get("command") == cmd, proj.get("command"))

agent = requests.post(f"{BASE}/api/agents", headers=ADMIN, json={
    "name": f"git-agent-{STAMP}", "identity": "git-agent",
    "labels": [GLABEL], "workspace": str(work), "auto_push": True,
}).json()
check("节点注册成功", bool(agent.get("token")), agent)

print("\n[3] 写入 agent 配置")
cfg_path = ROOT / "agent.json"
cfg_path.write_text(json.dumps({
    "server": BASE, "name": agent["name"], "identity": "git-agent",
    "token": agent["token"], "labels": [GLABEL], "workspace": str(work),
    "poll_seconds": 5, "state_dir": str(ROOT / "state"),
}, ensure_ascii=False), encoding="utf-8")

print("\n[4] 下发任务")
task = requests.post(f"{BASE}/api/tasks", headers=ADMIN, json={
    "project_id": proj["id"], "title": "验证 git 提交与推送",
    "description": "写一个文件并提交到 main", "priority": 5,
    "required_labels": [GLABEL],
}).json()
check("任务已创建", task.get("id") is not None, task)

print("\n[5] 真实运行 agent.py --once")
r = subprocess.run(
    [sys.executable, str(AGENT), "--config", str(cfg_path), "--once"],
    capture_output=True, text=True, timeout=180,
)
print("  --- agent 输出 ---")
for line in (r.stdout or "").splitlines()[:20]:
    print(f"  | {line}")
if r.returncode != 0:
    print(f"  --- stderr ---")
    for line in (r.stderr or "").splitlines()[-15:]:
        print(f"  | {line}")
check("agent 执行成功", r.returncode == 0, f"rc={r.returncode}")
check("agent 领取了任务", "领取任务" in r.stdout, r.stdout[:200])
check("agent 执行了 command", "执行命令" in r.stdout, r.stdout[:300])
check("agent 完成了 git 提交", "git 完成" in r.stdout, r.stdout[:400])

print("\n[6] 验证 agent 真的改了仓库")
check("command 产出了文件", (work / "result.txt").exists())
check("文件内容正确", (work / "result.txt").read_text() == "built ok"
      if (work / "result.txt").exists() else False)

local_sha = git(work, "rev-parse", "HEAD").stdout.strip()
remote_after = git(REMOTE, "rev-parse", "main").stdout.strip()
check("本地有新 commit", local_sha != remote_before)
check("commit 已推送到远端", remote_after == local_sha,
      f"remote={remote_after[:8]} local={local_sha[:8]}")

last_commit = git(work, "log", "-1", "--pretty=%s").stdout.strip()
check("commit message 带 task 标记", last_commit.startswith("[taskhub #"), last_commit)

print("\n[7] 验证状态回写")
final = requests.get(f"{BASE}/api/tasks/{task['id']}").json()
check("任务状态为 done", final["status"] == "done", final["status"])
check("commit sha 已回写", final["commit_sha"] == local_sha,
      f"{final['commit_sha'][:8]} vs {local_sha[:8]}")
check("结果摘要已回写", len(final["result_summary"]) > 0, final["result_summary"])
check("完成时间已记录", bool(final["finished_at"]))

print("\n[8] 验证日志与产物")
check("记录了领取动作", any("领取" in l["message"] for l in final["logs"]))
check("记录了开始执行", any("开始执行" in l["message"] for l in final["logs"]))
check("记录了 command 执行", any("执行命令" in l["message"] for l in final["logs"]))
check("记录了 git 结果", any("git 完成" in l["message"] for l in final["logs"]))
check("记录了结果回写", any("提交结果" in l["message"] for l in final["logs"]))
check("下载了产出日志文件", any(a["filename"].startswith("run-") for a in final["attachments"]),
      [a["filename"] for a in final["attachments"]])

print("\n[9] 验证节点状态")
agents = requests.get(f"{BASE}/api/agents").json()
mine = next(a for a in agents if a["name"] == agent["name"])
check("节点 current_task 已清空", mine["current_task"] is None, mine["current_task"])
check("节点 last_seen 已更新", bool(mine["last_seen"]))

print("\n[10] 无任务时 agent 干净退出")
r2 = subprocess.run(
    [sys.executable, str(AGENT), "--config", str(cfg_path), "--once"],
    capture_output=True, text=True, timeout=120,
)
check("无任务时正常退出", r2.returncode == 0, r2.stderr[:200])
check("提示没有可领任务", "没有可领取" in r2.stdout, r2.stdout[:200])

print(f"\n{'='*46}\n通过 {ok} / 失败 {fail}\n{'='*46}\n")
sys.exit(1 if fail else 0)