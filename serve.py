"""局域网工作台。

加功能时改 station/modules.json。页面中间的内容放在 station/pages/<id>.html。
导航和外壳不用改。没有账号。工作计划的事项在 station/work.json。
小孩学习的内容在 station/learn.json。任务调度的数据在 station/taskhub.db，
员工和页面都走 /api/v1。攒钱的记录在 station/piggy.json：/piggy/ 套外壳，
/piggy/app/ 是同一份页面、没有侧栏。
"""

import html
import importlib.util
import json
import re
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
STATION = ROOT / "station"
ASSETS = ROOT / "web" / "assets"
MODULES_FILE = STATION / "modules.json"
HOST = "0.0.0.0"
PORT = 8787


def load_py(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


work = load_py("station_work", STATION / "work.py")
learn = load_py("station_learn", STATION / "learn.py")
taskhub = load_py("station_taskhub", STATION / "taskhub.py")
piggy = load_py("station_piggy", STATION / "piggy.py")
ID_RE = re.compile(r"[a-z0-9-]{1,32}")
ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#efeae1">
  <title>{{title}}</title>
  <link rel="stylesheet" href="/assets/site.css">
</head>
<body>
  <div class="app">
    <aside class="side">
      <a class="brand" href="/">
        <span class="brand-mark">工</span>
        <span class="brand-text">
          <strong>{{station}}</strong>
          <small>同一局域网</small>
        </span>
      </a>
      <nav class="side-nav" aria-label="模块">{{side_nav}}</nav>
    </aside>
    <div class="main">
      <header class="mobile-top">
        <a href="/">{{station}}</a>
        <span>{{section}}</span>
      </header>
      <div class="main-inner">
        {{head}}
        {{body}}
      </div>
    </div>
  </div>
  <nav class="tabbar" aria-label="模块">{{tab_nav}}</nav>
</body>
</html>
"""


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def load_station():
    try:
        data = json.loads(MODULES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"station/modules.json 读不了：{exc}") from exc

    modules = []
    seen = set()
    for item in data.get("modules", []):
        module_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not ID_RE.fullmatch(module_id):
            raise ValueError(f"模块 id 只能用小写英文、数字和短横线：{module_id}")
        if module_id in seen:
            raise ValueError(f"模块 id 重复：{module_id}")
        if not name:
            raise ValueError(f"模块 {module_id} 缺名称")
        seen.add(module_id)
        modules.append({
            "id": module_id,
            "name": name,
            "group": str(item.get("group", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "status": str(item.get("status", "")).strip(),
        })
    return {"name": str(data.get("name", "工作台")).strip() or "工作台", "modules": modules}


def fill(mapping):
    page = PAGE
    for key, value in mapping.items():
        page = page.replace("{{" + key + "}}", value)
    return page


def side_nav(station, current):
    links = [nav_link("首页", "/", current == "home", index="")]
    for index, module in enumerate(station["modules"], start=1):
        links.append(nav_link(
            module["name"],
            f"/{module['id']}/",
            current == module["id"],
            index=f"{index:02d}",
        ))
    return "".join(links)


def tab_nav(station, current):
    links = [tab_link("首页", "/", current == "home")]
    for module in station["modules"]:
        links.append(tab_link(module["name"], f"/{module['id']}/", current == module["id"]))
    return "".join(links)


def nav_link(name, href, current, index):
    css = "nav-link is-current" if current else "nav-link"
    state = ' aria-current="page"' if current else ""
    index_html = f'<span class="nav-index">{index}</span>' if index else '<span class="nav-index"></span>'
    return f'<a class="{css}" href="{href}"{state}>{index_html}<span>{html.escape(name)}</span></a>'


def tab_link(name, href, current):
    css = "is-current" if current else ""
    state = ' aria-current="page"' if current else ""
    klass = f' class="{css}"' if css else ""
    return f'<a href="{href}"{klass}{state}><span>{html.escape(name)}</span></a>'


def page_head(kicker, heading, lead):
    parts = ["<header class=\"page-head\">"]
    if kicker:
        parts.append(f'<p class="kicker">{html.escape(kicker)}</p>')
    parts.append(f"<h1>{html.escape(heading)}</h1>")
    if lead:
        parts.append(f'<p class="lead">{html.escape(lead)}</p>')
    parts.append("</header>")
    return "".join(parts)


def home_body(station):
    if not station["modules"]:
        return (
            '<section class="placeholder"><p class="kicker">还没有模块</p>'
            "<h2>清单是空的</h2><p>在 station/modules.json 里加一项，刷新后就会出现。</p></section>"
        )

    grouped = {}
    for index, module in enumerate(station["modules"], start=1):
        grouped.setdefault(module["group"], []).append((index, module))

    blocks = [f'<p class="meta">共 {len(station["modules"])} 个模块</p>']
    for group, items in grouped.items():
        cards = "".join(card(index, module) for index, module in items)
        label = f'<h2 class="group-name">{html.escape(group)}</h2>' if group else ""
        blocks.append(f'<section class="group">{label}<div class="cards">{cards}</div></section>')
    return "".join(blocks)


def card(index, module):
    badge = f'<span class="badge">{html.escape(module["status"])}</span>' if module["status"] else ""
    return (
        f'<a class="card" href="/{module["id"]}/">'
        f'<span class="card-index">{index:02d}</span>'
        f'<span class="card-text"><strong>{html.escape(module["name"])}</strong>'
        f'<span>{html.escape(module["summary"])}</span></span>'
        f'<span class="card-side">{badge}<span>进入</span></span></a>'
    )


def module_body(module, query):
    # 会变的中间内容不走静态片段：工作计划在 work.json，小孩学习在 learn.json。
    if module["id"] == "work":
        return work.render(query)
    if module["id"] == "learn":
        return learn.render(query)
    path = STATION / "pages" / f"{module['id']}.html"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        '<section class="placeholder"><p class="kicker">准备中</p>'
        "<h2>功能尚未放入</h2><p>入口已经接好。页面内容以后放在这里。</p></section>"
    )


def render(station, current, title, section, head, body):
    return fill({
        "title": html.escape(title),
        "station": html.escape(station["name"]),
        "section": html.escape(section),
        "side_nav": side_nav(station, current),
        "tab_nav": tab_nav(station, current),
        "head": head,
        "body": body,
    })


def render_home(station):
    return render(
        station,
        "home",
        station["name"],
        "首页",
        page_head("家里", station["name"], "同一网络直接打开，不用登录。每个模块都可以单独往下做。"),
        home_body(station),
    )


def render_module(station, module, query):
    # 攒钱自己有整页横幅。外壳只留侧栏和底栏，避免两套标题叠在一起。
    head = ""
    if module["id"] != "piggy":
        head = page_head(module["group"] or "模块", module["name"], module["summary"])
    return render(
        station,
        module["id"],
        f'{module["name"]} · {station["name"]}',
        module["name"],
        head,
        module_body(module, query),
    )


def render_missing(station):
    return render(
        station,
        "",
        f'没有这一页 · {station["name"]}',
        "未找到",
        page_head("", "没有这一页", "地址不对，或这个模块还没写进清单。"),
        '<section class="placeholder"><p>回到首页可以看见现有的模块。</p></section>',
    )


def render_error(message):
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>工作台</title><pre>{html.escape(message)}</pre></html>"
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        split = urlsplit(self.path)
        path = split.path
        if path.startswith("/assets/"):
            self.serve_asset(path[len("/assets/"):])
            return
        if path.startswith("/api/v1/"):
            self.serve_task_api("GET", path, split.query)
            return
        if path.rstrip("/") == "/piggy/app":
            self.serve_piggy_page()
            return
        if path.rstrip("/") == "/piggy/api/data":
            self.serve_piggy_get()
            return
        try:
            station = load_station()
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))
            return

        route = path.strip("/")
        if route == "":
            self.respond(200, render_home(station))
            return

        module_id = route[:-1] if route.endswith("/") else route
        if "/" not in module_id:
            query = parse_qs(split.query)
            for module in station["modules"]:
                if module["id"] == module_id:
                    if module_id == "learn" and query.get("export", [""])[-1] == "1":
                        self.serve_learn_export()
                        return
                    self.respond(200, render_module(station, module, query))
                    return
        self.respond(404, render_missing(station))

    def do_POST(self):
        split = urlsplit(self.path)
        path = split.path
        if path.startswith("/api/v1/"):
            self.serve_task_api("POST", path, split.query)
            return
        if path.rstrip("/") == "/piggy/api/data":
            self.serve_piggy_post()
            return
        if path.rstrip("/") == "/learn":
            self.serve_learn_post()
            return
        try:
            station = load_station()
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))
            return
        if path.rstrip("/") != "/work":
            self.close_connection = True
            self.respond(404, render_missing(station))
            return
        fields = self.form_fields()
        if fields is None:
            self.respond(400, render_error("提交太长了"))
            return
        try:
            self.redirect(work.handle(fields))
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))

    def do_PATCH(self):
        split = urlsplit(self.path)
        if not split.path.startswith("/api/v1/"):
            self.close_connection = True
            self.respond(404, render_error("没有这一页"))
            return
        self.serve_task_api("PATCH", split.path, split.query)

    def serve_task_api(self, method, path, query):
        body = {}
        if method in ("POST", "PATCH"):
            raw = self.read_body(1000000)
            if raw is None:
                self.respond_json(*taskhub.err(400, "提交太长了"))
                return
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.respond_json(*taskhub.err(422, "请求体不是 JSON"))
                    return
                if not isinstance(body, dict):
                    self.respond_json(*taskhub.err(422, "请求体不是 JSON 对象"))
                    return
        try:
            parsed = parse_qs(query, keep_blank_values=True, max_num_fields=50)
        except ValueError:
            self.respond_json(*taskhub.err(400, "查询太长了"))
            return
        try:
            status, payload, headers = taskhub.handle(method, path, parsed, body)
        except Exception as exc:
            status, payload, headers = taskhub.err(500, str(exc))
        self.respond_json(status, payload, headers)

    def read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self.close_connection = True
            return None
        if length < 0 or length > limit:
            self.close_connection = True
            return None
        return self.rfile.read(length)

    def respond_json(self, status, payload, headers=None):
        self.respond(
            status,
            json.dumps(payload, ensure_ascii=False),
            "application/json; charset=utf-8",
            headers,
        )

    def form_fields(self):
        payload = self.read_body(4096)
        if payload is None:
            return None
        payload = payload.decode("utf-8", "replace")
        try:
            parsed = parse_qs(payload, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            return None
        return {key: values[-1] for key, values in parsed.items()}

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_asset(self, rel):
        if not ASSET_RE.fullmatch(rel) or ".." in rel.split("/"):
            self.respond(404, render_error("没有这个文件"))
            return
        target = (ASSETS / rel).resolve()
        if not target.is_relative_to(ASSETS.resolve()) or not target.is_file():
            self.respond(404, render_error("没有这个文件"))
            return
        kind = "text/css; charset=utf-8" if target.suffix == ".css" else "application/octet-stream"
        self.respond(200, target.read_bytes(), kind)

    def serve_piggy_page(self):
        try:
            body = piggy.page()
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))
            return
        self.respond(200, body)

    def serve_piggy_get(self):
        try:
            payload = piggy.read()
        except ValueError as exc:
            self.respond_json(500, {"status": "error", "message": str(exc)})
            return
        self.respond_json(200, payload)

    def serve_piggy_post(self):
        raw = self.read_body(1000000)
        if raw is None:
            self.respond_json(400, {"status": "error", "message": "提交太长了"})
            return
        if not raw:
            self.respond_json(400, {"status": "error", "message": "请求体不是 JSON"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.respond_json(400, {"status": "error", "message": "请求体不是 JSON"})
            return
        try:
            piggy.write(data)
        except ValueError as exc:
            self.respond_json(400, {"status": "error", "message": str(exc)})
            return
        self.respond_json(200, {"status": "ok"})

    def serve_learn_post(self):
        raw = self.read_body(262144)
        if raw is None:
            self.respond(400, render_error("提交太长了"))
            return
        try:
            self.redirect(learn.handle(self.headers.get("Content-Type", ""), raw))
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))

    def serve_learn_export(self):
        try:
            body = learn.raw()
        except ValueError as exc:
            self.respond(500, render_error(str(exc)))
            return
        self.respond(
            200,
            body,
            "application/json; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="learn.json"'},
        )

    def respond(self, code, body, content_type="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    taskhub.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"本机打开  http://127.0.0.1:{PORT}", flush=True)
    ip = lan_ip()
    if ip:
        print(f"手机打开  http://{ip}:{PORT}", flush=True)
    print("手机和电脑要在同一个 Wi-Fi。按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
