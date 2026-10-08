"""TaskHub agent 客户端。

放在每台执行电脑上常驻运行：
    python agent.py --config agent.json

职责：
1. 定期轮询服务器领取符合本机身份的任务
2. 把任务上下文（图 + 文）写到本地工作目录
3. 执行 tasks 目录下的处理脚本，或把任务交给外部命令
4. git commit + push
5. 回写状态、日志、截图

两种执行模式：
- command 模式：每个项目配一条 shell 命令，适合确定性任务（构建/测试/发布）
- handoff 模式：只把任务落地成 prompt 文件 + 心跳，等 WorkBuddy 定期任务来取走并执行
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：pip install requests")

DEFAULT_POLL = 30


class Agent:
    def __init__(self, cfg: dict):
        self.server = cfg["server"].rstrip("/")
        self.name = cfg["name"]
        self.identity = cfg.get("identity", cfg["name"])
        self.token = cfg["token"]
        self.labels = cfg.get("labels", [])
        self.workspace = cfg.get("workspace", "")
        self.poll = int(cfg.get("poll_seconds", DEFAULT_POLL))
        self.state_dir = Path(cfg.get("state_dir", f"agent_state/{self.name}"))
        self.running = True
        self.s = requests.Session()
        self.s.headers.update({"X-Agent-Token": self.token, "User-Agent": f"taskhub-agent/{self.name}"})

    # ------------------------------------------------------------ HTTP
    def api(self, path: str, **kw):
        kw.setdefault("timeout", 30)
        r = self.s.post(self.server + path, **kw)
        if r.status_code == 401:
            raise SystemExit("token 无效，请在工作台重置该节点口令并更新 agent.json")
        return r

    def log(self, task_id: int, message: str, level: str = "info") -> None:
        try:
            self.api(f"/api/agent/tasks/{task_id}/log",
                     data={"message": message, "level": level})
            print(f"  {message}", flush=True)
        except Exception as exc:  # 日志失败不能中断执行
            print(f"  [log failed] {exc}", flush=True)

    def upload(self, task_id: int, paths: list[Path]) -> None:
        """把产出文件作为附件回传到任务下，不改动任务状态。"""
        paths = [Path(p) for p in paths if p and Path(p).exists()]
        if not paths:
            return
        opened = []
        try:
            payload = []
            for p in paths:
                f = open(p, "rb")
                opened.append(f)
                payload.append(("files", (p.name, f, "application/octet-stream")))
            r = self.s.post(f"{self.server}/api/agent/tasks/{task_id}/attachments",
                            files=payload, timeout=300)
            r.raise_for_status()
            self.log(task_id, f"已上传 {len(paths)} 个产出文件")
        except Exception as exc:
            print(f"  [upload failed] {exc}", flush=True)
        finally:
            for f in opened:
                f.close()

    # ------------------------------------------------------------ 轮询
    def heartbeat(self) -> None:
        try:
            self.api("/api/agent/heartbeat")
        except Exception:
            pass

    def claim(self) -> dict | None:
        r = self.api("/api/agent/claim")
        if r.status_code == 409:
            print("已有执行中的任务，跳过本轮", flush=True)
            return None
        r.raise_for_status()
        return r.json().get("task")

    # ------------------------------------------------------------ 执行
    def prepare_workspace(self, task: dict) -> Path:
        """定位工作目录。

        优先级：
        1. 项目的 repo_path（每台电脑上路径可能不同，最精确）
        2. 本节点的 workspace + 项目名
        3. 当前目录下 workspace/项目名
        """
        proj = task.get("project", {})
        repo_path = (proj.get("repo_path") or "").strip()
        if repo_path and Path(repo_path).exists():
            return Path(repo_path)
        if self.workspace:
            return Path(self.workspace) / proj.get("name", "default")
        return Path.cwd() / "workspace" / proj.get("name", "default")

    def fetch_attachments(self, task: dict, dest: Path) -> list[Path]:
        img_dir = dest / ".taskhub" / "input"
        img_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for a in task.get("attachments", []):
            try:
                r = self.s.get(f"{self.server}/api/attachments/{a['id']}", timeout=60)
                r.raise_for_status()
                target = img_dir / a["filename"]
                target.write_bytes(r.content)
                out.append(target)
            except Exception as exc:
                print(f"  [附件下载失败] {a['filename']}: {exc}", flush=True)
        return out

    def write_prompt(self, task: dict, root: Path, images: list[Path]) -> Path:
        d = root / ".taskhub"
        d.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# 任务 #{task['seq']} {task['title']}",
            "",
            f"- 项目：{task['project']['name']}",
            f"- 分支：{task.get('branch') or 'main'}",
            f"- 优先级：{task.get('priority', 0)}",
            f"- 本机身份：{self.name} / {self.identity}",
            "",
            "## 任务描述",
            task.get("description") or "（无）",
            "",
        ]
        if images:
            lines.append("## 附件")
            lines += [f"- {p}" for p in images]
            lines.append("")
        (d / f"task-{task['id']}.md").write_text("\n".join(lines), encoding="utf-8")
        return d / f"task-{task['id']}.md"

    def git_step(self, root: Path, args: list[str], label: str) -> tuple[bool, str]:
        if not (root / ".git").exists():
            return False, f"{label}: 不是 git 仓库，跳过"
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=180)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return False, f"{label} 失败: {out[:500]}"
        return True, out[:800]

    def finish_git(self, task: dict, root: Path, auto_push: bool) -> tuple[str, str]:
        branch = task.get("branch") or "main"
        sha = ""

        # .taskhub 是 TaskHub 的内部工作区，不该进版本库
        if (root / ".git").exists():
            gi = root / ".gitignore"
            if not gi.exists():
                gi.write_text(".taskhub/\n", encoding="utf-8")
            else:
                txt = gi.read_text(encoding="utf-8")
                if ".taskhub" not in txt:
                    gi.write_text(txt.rstrip() + "\n.taskhub/\n", encoding="utf-8")

        ok, msg = self.git_step(root, ["add", "-A"], "git add")
        if not ok:
            return "", msg
        msg += "\n"
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True)
        if r.returncode == 0:
            return "", msg + "工作区无改动，无需提交"
        ok, m = self.git_step(root, ["commit", "-m", f"[taskhub #{task['seq']}] {task['title']}"], "git commit")
        msg += m + "\n"
        if not ok:
            return "", msg
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
        sha = r.stdout.strip()
        if auto_push:
            exists = subprocess.run(["git", "rev-parse", "--verify", branch], cwd=root,
                                    capture_output=True).returncode == 0
            if exists:
                ok, m = self.git_step(root, ["push", "origin", branch], "git push")
                msg += "\n" + m
            else:
                ok, m = self.git_step(root, ["push", "-u", "origin", f"HEAD:{branch}"], "git push")
                msg += "\n" + m
        return sha, msg

    def run_command(self, task: dict, root: Path) -> tuple[str, str, Path | None]:
        """执行项目配置的确定性命令。返回 (状态, 摘要, 产出文件路径)。"""
        cmd = (task.get("project", {}).get("command") or "").strip()
        if not cmd:
            return "review", "项目未配置自动命令，任务已落地为 prompt，等待人工或 WorkBuddy 处理。", None
        self.log(task["id"], f"执行命令：{cmd}")
        try:
            r = subprocess.run(cmd, cwd=root, shell=True, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            return "failed", "", None
        logfile = root / ".taskhub" / f"run-{task['id']}.log"
        logfile.write_text(f"$ {cmd}\n\nSTDOUT\n{r.stdout}\n\nSTDERR\n{r.stderr}", encoding="utf-8")
        combined = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            summary = combined.strip()[-1500:] or f"命令执行成功（无输出）：{cmd}"
        else:
            summary = f"命令返回码 {r.returncode}：\n{combined.strip()[-1200:]}"
        return ("done" if r.returncode == 0 else "failed"), summary, logfile

    # ------------------------------------------------------------ 主循环
    def process(self, task: dict) -> None:
        tid = task["id"]
        print(f"\n=== 领取任务 #{task['seq']} {task['title']} ===", flush=True)
        self.api(f"/api/agent/tasks/{tid}/start")
        self.log(tid, f"节点 {self.name} 开始执行")

        root = self.prepare_workspace(task)
        self.log(tid, f"工作目录：{root}")
        images = self.fetch_attachments(task, root)
        if images:
            self.log(tid, f"下载附件 {len(images)} 个")
        prompt = self.write_prompt(task, root, images)
        self.log(tid, f"任务上下文已写入 {prompt.name}（含 {len(images)} 个附件）")

        status, summary, artifact = (
            "review",
            f"任务 #{task['seq']} 已就绪，等待 WorkBuddy 定期任务接手执行。"
            f"上下文已写入 {prompt.name}（含 {len(images)} 个附件）。",
            None,
        )

        # command 模式：项目配置了 command 时自动跑确定性命令
        proj_cmd = (task.get("project", {}).get("command") or "").strip()
        if proj_cmd:
            status, summary, artifact = self.run_command(task, root)
        else:
            self.log(tid, "本项目未配置 command，已生成 handoff 文件等待 WorkBuddy 定期任务执行", level="warn")

        commit_sha = ""
        auto_push = bool(task.get("agent", {}).get("auto_push", True))
        if status == "done":
            commit_sha, git_msg = self.finish_git(task, root, auto_push)
            if commit_sha:
                self.log(tid, f"git 完成，commit {commit_sha[:8]}")
            elif "无改动" not in git_msg:
                self.log(tid, git_msg.strip().replace("\n", " "), level="warn")

        files = [f for f in [artifact] if f]
        data = {
            "status": status,
            "result_summary": summary,
            "error": "" if status != "failed" else summary[-2000:],
            "commit_sha": commit_sha,
            "branch": task.get("branch") or "",
        }
        if files:
            payload = [("body", (None, json.dumps(data), "application/json"))]
            payload += [("files", (f.name, open(f, "rb"), "application/octet-stream")) for f in files]
            r = self.s.post(f"{self.server}/api/agent/tasks/{tid}/report",
                            files=payload, timeout=300)
            for _, fh in payload[1:]:
                fh[1].close()
        else:
            r = self.s.post(f"{self.server}/api/agent/tasks/{tid}/report",
                            json=data, timeout=120)
        r.raise_for_status()
        print(f"=== 任务 #{task['seq']} 已回写：{status} ===\n", flush=True)

    def loop(self) -> None:
        print(f"TaskHub agent 已启动：{self.name} @ {self.server}", flush=True)
        print(f"  身份 {self.identity} · 标签 {','.join(self.labels) or '无'} · 轮询 {self.poll}s\n", flush=True)
        while self.running:
            self.heartbeat()
            try:
                task = self.claim()
            except SystemExit:
                raise
            except Exception as exc:
                print(f"[轮询失败] {exc}", flush=True)
                time.sleep(self.poll)
                continue
            if not task:
                time.sleep(self.poll)
                continue
            try:
                self.process(task)
            except SystemExit:
                raise
            except Exception as exc:
                print(f"[执行异常] {exc}", flush=True)
                try:
                    self.log(task["id"], f"执行异常：{exc}", level="error")
                    self.s.post(f"{self.server}/api/agent/tasks/{task['id']}/report",
                                json={"status": "failed", "error": str(exc)[:2000]},
                                timeout=60)
                except Exception:
                    pass
            time.sleep(3)


def main() -> None:
    ap = argparse.ArgumentParser(description="TaskHub agent")
    ap.add_argument("--config", default="agent.json", help="配置文件路径")
    ap.add_argument("--once", action="store_true", help="只轮询一次后退出")
    ap.add_argument("--server", help="覆盖配置里的服务器地址")
    ap.add_argument("--token", help="覆盖配置里的 token")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"找不到配置文件 {cfg_path}。可参考 agent.example.json 复制一份。")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if args.server:
        cfg["server"] = args.server
    if args.token:
        cfg["token"] = args.token

    agent = Agent(cfg)
    if args.once:
        agent.heartbeat()
        task = agent.claim()
        if not task:
            print("没有可领取的任务")
            return
        agent.process(task)
        return
    try:
        agent.loop()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()