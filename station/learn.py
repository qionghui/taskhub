"""小孩学习的主题和卡片。

内容在 station/learn.json。这份文件就是导入导出的格式：
导出原样下载它，导入是整份换上，换上前先检查。页面上的管理可以改、加、删。
每次请求都重读，改文件后刷新即可。
version 用来拒绝以后才认识的格式，避免把一份新文件画成错的页面。
"""

import html
import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote

DATA = Path(__file__).resolve().parent / "learn.json"
LOCK = threading.Lock()
ID_RE = re.compile(r"[a-z0-9-]{1,32}")
PATH_RE = re.compile(r"\d+(?:\.\d+){0,5}")
NAME_MAX = 40
TEXT_MAX = 200
MAX_CARDS = 200
MAX_DEPTH = 6
BODY_NOTE = "点开后的一句"
SPLIT = ("map-split", ("is-bottom to-a", "is-bottom to-b"))
CROSS = (
    "map-cross",
    ("is-top to-a", "is-top to-b", "is-bottom to-a", "is-bottom to-b"),
)


def render(query):
    manage = _first(query, "manage") == "1"
    error = _clean(_first(query, "error"))
    drop = _first(query, "drop")
    if not PATH_RE.fullmatch(drop or ""):
        drop = ""
    try:
        data = load()
    except ValueError as exc:
        return _broken(error or str(exc))
    return _page(data, manage, error, drop)


def handle(content_type, body):
    try:
        fields, upload = _posted(content_type or "", body)
    except ValueError as exc:
        return _back(str(exc))
    if fields.get("action") == "import":
        return _import(upload)
    anchor = fields.get("back", "")
    if not ID_RE.fullmatch(anchor or ""):
        anchor = ""
    try:
        with LOCK:
            data = load()
            error, anchor = _change(data, fields, anchor)
            if error:
                return _back(error, anchor)
            save(data)
    except ValueError as exc:
        return _back(str(exc), anchor)
    return _back("", anchor)


def raw():
    try:
        return DATA.read_bytes()
    except OSError as exc:
        raise ValueError(f"station/learn.json 读不了：{exc}") from exc


def load():
    try:
        # utf-8-sig：从记事本存出来的文件常带 BOM，仍应能导入。
        text = DATA.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"station/learn.json 读不了：{exc}") from exc
    return loads(text)


def loads(text):
    try:
        raw_data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"station/learn.json 读不了：{exc}") from exc
    if not isinstance(raw_data, dict):
        raise ValueError("station/learn.json 不是一份学习内容")
    version = raw_data.get("version", 1)
    if version != 1:
        raise ValueError(f"这份内容的版本读不了：{version}")
    cards = raw_data.get("cards", [])
    if not isinstance(cards, list):
        raise ValueError("cards 不是列表")
    seen = set()
    count = [0]
    return {
        "version": 1,
        "cards": [_card(item, "顶层", seen, True, 1, count) for item in cards],
    }


def save(data):
    text = json.dumps(
        {"version": 1, "cards": [_dump(card) for card in data["cards"]]},
        ensure_ascii=False,
        indent=2,
    )
    _write(text + "\n")


def _card(raw, parent, seen, top, depth, count):
    count[0] += 1
    if count[0] > MAX_CARDS:
        raise ValueError("卡片太多了")
    if depth > MAX_DEPTH:
        raise ValueError("卡片套得太深了")
    if not isinstance(raw, dict):
        raise ValueError(f"{parent}有一张不是卡片")
    name = _plain(raw.get("name"), f"{parent}里一张卡片的 name", NAME_MAX)
    if not name:
        raise ValueError(f"{parent}有一张卡片缺 name")
    text = _plain(raw.get("text", ""), f"{name} 的 text", TEXT_MAX)
    note = _plain(raw.get("note", ""), f"{name} 的 note", TEXT_MAX)
    children = raw.get("cards", [])
    if not isinstance(children, list):
        raise ValueError(f"{name} 的 cards 不是列表")
    card_id = raw.get("id", "")
    if not isinstance(card_id, str):
        raise ValueError(f"{name} 的 id 不是文字")
    card_id = card_id.strip()
    if (top or children) and not card_id:
        raise ValueError(f"{name} 缺 id")
    if card_id:
        if not ID_RE.fullmatch(card_id):
            raise ValueError(f"id 只能用小写英文、数字和短横线：{card_id}")
        if card_id in seen:
            raise ValueError(f"id 重复：{card_id}")
        seen.add(card_id)
    kids = [_card(item, name, seen, False, depth + 1, count) for item in children]
    return {"id": card_id, "name": name, "text": text, "note": note, "cards": kids}


def _typed(value, label, limit, required):
    if not isinstance(value, str):
        return "", f"{label}不对。"
    text = value.strip()
    if "{{" in text:
        return "", f"{label}里不能写花括号。"
    if len(text) > limit:
        return "", f"{label}太长了。"
    if required and not text:
        return "", f"先写下{label}。"
    return text, ""


def _plain(value, label, limit):
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label}不是文字")
    text = value.strip()
    if "{{" in text:
        raise ValueError(f"{label}里不能写 {{{{")
    if len(text) > limit:
        raise ValueError(f"{label}太长了")
    return text


def _page(data, manage, error, drop):
    cards = data["cards"]
    notice = f'<p class="learn-error">{html.escape(error)}</p>' if error else ""
    band = _import_form() if manage else ""
    panes = "".join(_panes(cards, None, "", manage, drop))
    return (
        f'<div class="learn">{_tools(manage)}{notice}{band}'
        f'<section class="learn-home">{_home(cards, manage, drop)}</section>{panes}</div>'
    )


def _home(cards, manage, drop):
    if not cards:
        hint = "在下面加一个主题，或用上面的导入。" if manage else "用「管理」加上主题，或导入一份内容。"
        body = (
            '<section class="placeholder"><p class="kicker">还没有主题</p>'
            f"<h2>这份内容是空的</h2><p>{hint}</p></section>"
        )
    else:
        body = f'<div class="cards">{"".join(_home_card(card, i) for i, card in enumerate(cards, start=1))}</div>'
    if not manage:
        return body
    rows = "".join(_row(card, str(i), drop, "") for i, card in enumerate(cards))
    listing = f'<p class="kicker">删除</p>{rows}' if rows else ""
    return f"{body}<div class=\"learn-manage\">{listing}{_add_form('', '', '加一个主题', '主题标题')}</div>"


def _home_card(card, index):
    text = f'<span>{html.escape(card["text"])}</span>' if card["text"] else ""
    return (
        f'<a class="card" href="#{html.escape(card["id"])}">'
        f'<span class="card-index">{index:02d}</span>'
        f'<span class="card-text"><strong>{html.escape(card["name"])}</strong>{text}</span>'
        f'<span class="card-side"><span>进入</span></span></a>'
    )


def _panes(cards, parent, prefix, manage, drop):
    parts = []
    for index, card in enumerate(cards):
        path = f"{prefix}.{index}" if prefix else str(index)
        if not card["id"]:
            continue
        parts.append(_pane(card, parent, path, manage, drop))
        parts.append(_panes(card["cards"], card, path, manage, drop))
    return "".join(parts)


def _pane(card, parent, path, manage, drop):
    if parent is None:
        href = "/learn/?manage=1" if manage else "/learn/"
        back = f'<a class="learn-back" href="{href}">全部主题</a>'
    else:
        back = (
            f'<a class="learn-back" href="#{html.escape(parent["id"])}">'
            f'{html.escape(parent["name"])}</a>'
        )
    note = f'<p class="map-note">{html.escape(card["note"])}</p>' if card["note"] else ""
    extra = _pane_manage(card, path, drop) if manage else ""
    return (
        f'<section class="learn-pane" id="{html.escape(card["id"])}">'
        f"{_tools(manage, card['id'])}{back}{note}{_map(card)}{extra}</section>"
    )


def _pane_manage(card, path, drop):
    rows = "".join(
        _row(child, f"{path}.{index}", drop, card["id"])
        for index, child in enumerate(card["cards"])
    )
    inside = rows or '<p class="meta">还没有</p>'
    return (
        '<div class="learn-manage">'
        f"{_edit_form(card, path)}"
        f'<p class="kicker">里面的卡片</p>{inside}'
        f"{_add_form(path, card['id'], '加一张', '卡片标题')}"
        '<p class="kicker">删除</p>'
        f"{_row(card, path, drop, card['id'])}"
        "</div>"
    )


def _map(card):
    kids = card["cards"]
    if not kids:
        return f'<div class="map"><h2 class="map-root">{html.escape(card["name"])}</h2></div>'
    kind, places = _layout(len(kids))
    body = "".join(_node(child, place, i) for i, (child, place) in enumerate(zip(kids, places), start=1))
    if kind == "map-stack":
        body = f'<div class="map-branch">{body}</div>'
    label = html.escape(f'{card["name"]}的导图')
    return (
        f'<div class="map {kind}" role="group" aria-label="{label}">'
        f'<h2 class="map-root">{html.escape(card["name"])}</h2>{body}</div>'
    )


def _layout(count):
    if count == 2:
        return SPLIT
    if count == 4:
        return CROSS
    return "map-stack", ("",) * count


def _node(card, place, index):
    klass = f"map-card {place}".strip()
    inner = (
        f'<span class="map-index">{index:02d}</span>'
        f"<strong>{html.escape(card['name'])}</strong>"
    )
    if card["text"]:
        inner += f'<span class="map-text">{html.escape(card["text"])}</span>'
    if card["id"]:
        return f'<a class="{klass}" href="#{html.escape(card["id"])}">{inner}</a>'
    return f'<div class="{klass}">{inner}</div>'


def _tools(manage, card_id=""):
    if manage and card_id:
        first = f'<a href="/learn/#{html.escape(card_id)}">看内容</a>'
        imported = '<a href="/learn/?manage=1">导入</a>'
    elif manage:
        first = '<a href="/learn/">看内容</a>'
        imported = ""
    elif card_id:
        first = f'<a href="/learn/?manage=1#{html.escape(card_id)}">管理</a>'
        imported = ""
    else:
        first = '<a href="/learn/?manage=1">管理</a>'
        imported = ""
    return (
        f'<p class="learn-tools">{first}{imported}'
        '<a href="/learn/?export=1">导出</a></p>'
    )


def _import_form():
    return (
        '<form class="learn-form learn-import" method="post" action="/learn/" enctype="multipart/form-data">'
        '<input type="hidden" name="action" value="import">'
        '<p class="kicker">导入</p>'
        '<label class="field">选一份内容'
        '<input type="file" name="file" accept=".json,application/json" required></label>'
        '<button class="btn btn-primary" type="submit">导入</button>'
        '<p class="meta">整份换上。检查不过，原来的还在。</p></form>'
    )


def _add_form(path, anchor, kicker, label):
    return (
        '<form class="learn-form" method="post" action="/learn/">'
        '<input type="hidden" name="action" value="add">'
        f'<input type="hidden" name="path" value="{html.escape(path)}">'
        f'<input type="hidden" name="back" value="{html.escape(anchor)}">'
        f'<p class="kicker">{html.escape(kicker)}</p>'
        f'<label class="field">{html.escape(label)}'
        '<input type="text" name="name" maxlength="40" required></label>'
        '<label class="field">一行说明'
        '<input type="text" name="text" maxlength="200"></label>'
        '<button class="btn btn-primary" type="submit">加上</button></form>'
    )


def _edit_form(card, path):
    return (
        '<form class="learn-form" method="post" action="/learn/">'
        '<input type="hidden" name="action" value="save">'
        f'<input type="hidden" name="path" value="{html.escape(path)}">'
        f'<input type="hidden" name="back" value="{html.escape(card["id"])}">'
        '<p class="kicker">这张卡片</p>'
        '<label class="field">标题'
        f'<input type="text" name="name" maxlength="40" required value="{html.escape(card["name"])}"></label>'
        '<label class="field">一行说明'
        f'<input type="text" name="text" maxlength="200" value="{html.escape(card["text"])}"></label>'
        f'<label class="field">{BODY_NOTE}'
        f'<input type="text" name="note" maxlength="200" value="{html.escape(card["note"])}"></label>'
        '<button class="btn btn-primary" type="submit">保存</button></form>'
    )


def _row(card, path, drop, anchor):
    if drop == path:
        return _confirm(card, path, anchor)
    href = f"/learn/?manage=1&drop={path}"
    if anchor:
        href += f"#{html.escape(anchor)}"
    return (
        f'<div class="learn-row"><span>{html.escape(card["name"])}</span>'
        f'<a class="btn" href="{href}">删除</a></div>'
    )


def _confirm(card, path, anchor):
    kids = "里面的也会一起去掉。" if card["cards"] else ""
    cancel = "/learn/?manage=1" + (f"#{html.escape(anchor)}" if anchor else "")
    return (
        '<form class="learn-confirm" method="post" action="/learn/">'
        '<input type="hidden" name="action" value="delete">'
        f'<input type="hidden" name="path" value="{html.escape(path)}">'
        f'<p>要删掉「{html.escape(card["name"])}」吗？{kids}</p>'
        '<div class="learn-row"><button class="btn" type="submit">确定删除</button>'
        f'<a class="btn" href="{cancel}">取消</a></div></form>'
    )


def _broken(message):
    tools = '<a href="/learn/?export=1">导出</a>' if DATA.is_file() else ""
    return (
        f'<div class="learn"><p class="learn-tools">{tools}</p>'
        f'<p class="learn-error">{html.escape(_clean(message))}</p>{_import_form()}</div>'
    )


def _change(data, fields, anchor):
    action = fields.get("action", "")
    if action == "delete":
        return _delete(data, fields.get("path", ""))
    if action == "add":
        return _add(data, fields, anchor)
    if action == "save":
        return _save_card(data, fields, anchor)
    return "这项填得不对。", anchor


def _delete(data, path):
    found = _locate(data["cards"], path)
    if found is None:
        return "没有这张卡片。", ""
    parent, listing, index, _card = found
    del listing[index]
    return "", parent["id"] if parent and parent["id"] else ""


def _add(data, fields, anchor):
    name, problem = _typed(fields.get("name", ""), "标题", NAME_MAX, True)
    if problem:
        return problem, anchor
    text, problem = _typed(fields.get("text", ""), "说明", TEXT_MAX, False)
    if problem:
        return problem, anchor
    if _count(data["cards"]) >= MAX_CARDS:
        return "卡片太多了。", anchor
    path = fields.get("path", "")
    card = {"id": "", "name": name, "text": text, "note": "", "cards": []}
    if path == "":
        card["id"] = _fresh(data["cards"])
        data["cards"].append(card)
        return "", ""
    found = _locate(data["cards"], path)
    if found is None:
        return "没有这张卡片。", anchor
    _parent, _listing, _index, node = found
    if path.count(".") + 1 >= MAX_DEPTH:
        return "卡片套得太深了。", anchor
    if not node["id"]:
        node["id"] = _fresh(data["cards"])
    node["cards"].append(card)
    return "", node["id"]


def _save_card(data, fields, anchor):
    found = _locate(data["cards"], fields.get("path", ""))
    if found is None:
        return "没有这张卡片。", anchor
    _parent, _listing, _index, card = found
    stay = card["id"] or anchor
    name, problem = _typed(fields.get("name", ""), "标题", NAME_MAX, True)
    if problem:
        return problem, stay
    text, problem = _typed(fields.get("text", ""), "说明", TEXT_MAX, False)
    if problem:
        return problem, stay
    note, problem = _typed(fields.get("note", ""), BODY_NOTE, TEXT_MAX, False)
    if problem:
        return problem, stay
    card["name"] = name
    card["text"] = text
    card["note"] = note
    return "", stay


def _import(upload):
    if not upload:
        return _back("先选一份内容。")
    try:
        text = upload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _back("这份内容不是 UTF-8。")
    try:
        loads(text)
    except ValueError as exc:
        return _back(f"没换上。{exc}")
    if not text.endswith("\n"):
        text += "\n"
    try:
        with LOCK:
            _write(text)
    except ValueError as exc:
        return _back(str(exc))
    return _back()


def _posted(content_type, body):
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError("提交读不了")
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "application/x-www-form-urlencoded":
        try:
            parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
        except ValueError as exc:
            raise ValueError("提交读不了") from exc
        return {key: values[-1] for key, values in parsed.items()}, b""
    if kind == "multipart/form-data":
        return _multipart(content_type, body)
    raise ValueError("提交读不了")


def _multipart(content_type, body):
    matched = re.search(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', content_type, re.IGNORECASE)
    if not matched:
        raise ValueError("提交读不了")
    token = matched.group(1) if matched.group(1) is not None else matched.group(2)
    if not token or len(token) > 200:
        raise ValueError("提交读不了")
    marker = b"--" + token.encode("ascii", "replace")
    if marker not in body:
        raise ValueError("提交读不了")
    fields = {}
    upload = b""
    for chunk in body.split(marker)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, content = chunk.partition(b"\n\n")
        if not sep:
            continue
        header = head.decode("latin-1", "replace")
        name = _part_param(header, "name")
        if not name:
            continue
        filename = _part_param(header, "filename")
        if filename is not None and name == "file":
            upload = content
        elif filename is None and name not in fields and len(fields) < 12:
            fields[name] = content.decode("utf-8", "replace")
    return fields, upload


def _part_param(header, key):
    for line in header.splitlines():
        if not line.lower().startswith("content-disposition"):
            continue
        found = re.search(
            rf'(?:^|;)\s*{key}\s*=\s*(?:"([^"]*)"|([^;\s]*))',
            line,
            re.IGNORECASE,
        )
        if not found:
            return None
        return found.group(1) if found.group(1) is not None else found.group(2)
    return None


def _locate(cards, path):
    if not PATH_RE.fullmatch(path or ""):
        return None
    parent = None
    listing = cards
    card = None
    index = 0
    parts = [int(part) for part in path.split(".")]
    for step, index in enumerate(parts):
        if index >= len(listing):
            return None
        card = listing[index]
        if step < len(parts) - 1:
            parent = card
            listing = card["cards"]
    return parent, listing, index, card


def _count(cards):
    return sum(1 + _count(card["cards"]) for card in cards)


def _fresh(cards):
    seen = set()
    _collect(cards, seen)
    while True:
        card_id = secrets.token_hex(4)
        if card_id not in seen:
            return card_id


def _collect(cards, seen):
    for card in cards:
        if card["id"]:
            seen.add(card["id"])
        _collect(card["cards"], seen)


def _dump(card):
    item = {}
    if card["id"]:
        item["id"] = card["id"]
    item["name"] = card["name"]
    if card["text"]:
        item["text"] = card["text"]
    if card["note"]:
        item["note"] = card["note"]
    if card["cards"]:
        item["cards"] = [_dump(child) for child in card["cards"]]
    return item


def _write(text):
    tmp = DATA.with_suffix(".json.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(DATA)
    except OSError as exc:
        raise ValueError(f"station/learn.json 写不了：{exc}") from exc


def _back(error="", anchor=""):
    error = _clean(error)
    query = "manage=1"
    if error:
        query += "&error=" + quote(error)
    if anchor and ID_RE.fullmatch(anchor):
        return f"/learn/?{query}#{anchor}"
    return f"/learn/?{query}"


def _first(query, key):
    if not isinstance(query, dict):
        return ""
    values = query.get(key) or []
    return values[-1] if values else ""


def _clean(text):
    text = " ".join(str(text).split())
    return text.replace("{{", "")[:180]
