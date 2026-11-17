"""工作计划的事项和周期。

数据在 station/work.json。中间内容随请求生成，不走静态页面片段。
日期用这台电脑的本地日历：家里的「周五」以服务器所在时区为准。
"""

import html
import json
import re
import secrets
import threading
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent / "work.json"
LOCK = threading.Lock()
ID_RE = re.compile(r"[a-z0-9]{1,32}")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
ERRORS = {
    "empty": "先写下事项。",
    "long": "事项太长了，80 字以内。",
    "missing": "没有这条事项。",
    "closed": "完成后不能再加子计划。",
    "bad": "这项填得不对。",
}


def render(query):
    done_view = _first(query, "done") == "1"
    error = ERRORS.get(_first(query, "error"), "")
    finding = " ".join(_first(query, "q").split())[:80]
    splitting = _first(query, "split")
    if not ID_RE.fullmatch(splitting or ""):
        splitting = ""
    try:
        with LOCK:
            data = load()
            today = date.today()
            added = apply_routines(data, today)
            if added:
                save(data)
    except ValueError as exc:
        return (
            '<section class="placeholder"><p class="kicker">读不了</p>'
            f"<h2>工作计划的数据有问题</h2><p>{html.escape(str(exc))}</p></section>"
        )
    return _page(data, done_view, error, finding, added, today, splitting)


def handle(fields):
    back = "/work/?done=1" if fields.get("back") == "done" else "/work/"
    with LOCK:
        data = load()
        today = date.today()
        added = apply_routines(data, today)
        error = _run(data, fields, today)
        if added or error is None:
            save(data)
    if error:
        joiner = "&" if "?" in back else "?"
        return f"{back}{joiner}error={error}"
    return back


def load():
    if not DATA.is_file():
        return {"items": [], "routines": []}
    try:
        raw = json.loads(DATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"station/work.json 读不了：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("station/work.json 不是一份计划")
    items = raw.get("items", [])
    routines = raw.get("routines", [])
    if not isinstance(items, list) or not isinstance(routines, list):
        raise ValueError("station/work.json 不是一份计划")
    return {
        "items": [_item(item) for item in items],
        "routines": [_routine(item) for item in routines],
    }


def save(data):
    payload = {"items": data["items"], "routines": data["routines"]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = DATA.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(DATA)


def apply_routines(data, today):
    added = []
    for routine in data["routines"]:
        due = latest_due(today, routine)
        if due is None or due > today:
            continue
        stamp = due.isoformat()
        if routine["last"] and routine["last"] >= stamp:
            continue
        _append_item(data, routine["text"], stamp)
        routine["last"] = stamp
        added.append(routine["text"])
    return added


def latest_due(today, routine):
    every = routine["every"]
    if every == "day":
        return today
    if every == "week":
        delta = (today.weekday() - routine["weekday"]) % 7
        return today - timedelta(days=delta)
    if every == "month":
        this_month = _date_in_month(today.year, today.month, routine["day"])
        if this_month <= today:
            return this_month
        year = today.year - 1 if today.month == 1 else today.year
        month = 12 if today.month == 1 else today.month - 1
        return _date_in_month(year, month, routine["day"])
    return None


def _date_in_month(year, month, day):
    last = monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _run(data, fields, today):
    action = fields.get("action", "")
    if action == "add":
        text, error = _text(fields.get("text", ""))
        if error:
            return error
        _append_item(data, text, "")
        return None
    if action == "child":
        text, error = _text(fields.get("text", ""))
        if error:
            return error
        return _append_child(data, fields.get("parent", ""), text)
    if action in ("done", "undo"):
        return _set_done(data, fields.get("id", ""), action == "done")
    if action == "routine":
        return _add_routine(data, fields, today)
    if action == "drop":
        return _drop_routine(data, fields.get("id", ""))
    return "bad"


def _append_item(data, text, when):
    data["items"].append({
        "id": _new_id(data),
        "text": text,
        "done": False,
        "when": when,
        "children": [],
    })


def _append_child(data, parent_id, text):
    if not ID_RE.fullmatch(parent_id or ""):
        return "missing"
    for index, item in enumerate(data["items"]):
        if item["id"] != parent_id:
            continue
        if item["done"]:
            return "closed"
        item["children"].append({"id": _new_id(data), "text": text, "done": False})
        # 主计划还没完成，写入子计划后把这项排到最后。
        data["items"].append(data["items"].pop(index))
        return None
    return "missing"


def _set_done(data, ident, done):
    if not ID_RE.fullmatch(ident or ""):
        return "missing"
    for item in data["items"]:
        if item["id"] == ident:
            item["done"] = done
            return None
        for child in item["children"]:
            if child["id"] == ident:
                child["done"] = done
                return None
    return "missing"


def _add_routine(data, fields, today):
    text, error = _text(fields.get("text", ""))
    if error:
        return error
    every = fields.get("every", "")
    if every not in ("day", "week", "month"):
        return "bad"
    weekday = _integer(fields.get("weekday", ""), 0, 6)
    day = _integer(fields.get("day", ""), 1, 31)
    if every == "week" and weekday is None:
        return "bad"
    if every == "month" and day is None:
        return "bad"
    routine = {
        "id": _new_id(data),
        "text": text,
        "every": every,
        "weekday": weekday if weekday is not None else 4,
        "day": day if day is not None else 1,
        "last": "",
    }
    # 今天不是到期日时，把已经过去的那次记上，避免补出过期事项。
    due = latest_due(today, routine)
    if due is not None and due < today:
        routine["last"] = due.isoformat()
    data["routines"].append(routine)
    apply_routines(data, today)
    return None


def _drop_routine(data, ident):
    if not ID_RE.fullmatch(ident or ""):
        return "missing"
    for index, routine in enumerate(data["routines"]):
        if routine["id"] == ident:
            del data["routines"][index]
            return None
    return "missing"


def _page(data, done_view, error, finding, added, today, splitting=""):
    parts = ['<div class="sheet">', _switch(done_view)]
    if error:
        parts.append(f'<p class="plan-error">{html.escape(error)}</p>')
    if added:
        names = "、".join(html.escape(name) for name in added)
        parts.append(f'<p class="plan-note">已加入：{names}</p>')
    if done_view:
        parts.extend(_done_view(data, finding, today))
    else:
        parts.extend(_open_view(data, today, splitting))
    parts.append("</div>")
    return "".join(parts)


def _switch(done_view):
    open_state = "" if done_view else ' aria-current="page"'
    done_state = ' aria-current="page"' if done_view else ""
    open_class = "" if done_view else " is-current"
    done_class = " is-current" if done_view else ""
    return (
        '<nav class="plan-switch" aria-label="计划">'
        f'<a class="plan-tab{open_class}" href="/work/"{open_state}>未完成</a>'
        f'<a class="plan-tab{done_class}" href="/work/?done=1"{done_state}>已完成</a>'
        "</nav>"
    )


def _open_view(data, today, splitting):
    visible = [item for item in data["items"] if not item["done"]]
    parts = [
        '<p class="meta">新事项加在最后。点「拆分任务」加上子计划后，这项也会排到最后。</p>'
    ]
    if visible:
        rows = "".join(
            _open_item(item, index, today, splitting)
            for index, item in enumerate(visible, start=1)
        )
        parts.append(f'<ol class="plans">{rows}</ol>')
    parts.append(
        '<form class="plan-new" method="post" action="/work/" accept-charset="UTF-8">'
        '<input type="hidden" name="action" value="add">'
        '<input type="text" name="text" maxlength="80" required placeholder="新的事项" aria-label="新的事项" autocomplete="off">'
        '<button class="btn btn-primary" type="submit">加到最后</button>'
        "</form>"
    )
    parts.append(_routines(data))
    return parts


def _open_item(item, index, today, splitting):
    children = "".join(_open_child(child) for child in item["children"] if not child["done"])
    subs = f'<ul class="plan-subs">{children}</ul>' if children else ""
    open_split = splitting == item["id"]
    if open_split:
        toggle = '<a class="btn is-current" href="/work/">拆分任务</a>'
        compose = _child_form(item["id"])
    else:
        toggle = f'<a class="btn" href="/work/?split={html.escape(item["id"])}">拆分任务</a>'
        compose = ""
    return (
        '<li class="plan"><div class="plan-line">'
        f'<span class="plan-index">{index:02d}</span>'
        f'<p class="plan-text">{html.escape(item["text"])}{_when(item["when"], today)}</p>'
        f'<div class="plan-actions">{_mark_form(item["id"], "done", "完成", "")}'
        f"{toggle}</div></div>{compose}{subs}</li>"
    )


def _open_child(child):
    return (
        '<li class="plan-sub">'
        f'<p class="plan-text">{html.escape(child["text"])}</p>'
        f'{_mark_form(child["id"], "done", "完成", "")}'
        "</li>"
    )


def _child_form(parent_id):
    return (
        '<form class="plan-compose" method="post" action="/work/" accept-charset="UTF-8">'
        '<input type="hidden" name="action" value="child">'
        f'<input type="hidden" name="parent" value="{html.escape(parent_id)}">'
        '<input type="text" name="text" maxlength="80" required placeholder="子计划" aria-label="子计划" autocomplete="off" autofocus>'
        '<button class="btn" type="submit">添加</button>'
        "</form>"
    )


def _done_view(data, finding, today):
    parts = ['<p class="meta">完成的事项不在未完成里。可以在这里查看，也可以恢复。</p>', _find_form(finding)]
    rows = "".join(_done_blocks(data, finding, today))
    if rows:
        parts.append(f'<ol class="plans">{rows}</ol>')
    elif finding:
        parts.append('<p class="meta">没有符合的事项。</p>')
    else:
        parts.append('<p class="meta">还没有完成的事项。</p>')
    return parts


def _find_form(finding):
    value = f' value="{html.escape(finding)}"' if finding else ""
    return (
        '<form class="plan-new" method="get" action="/work/">'
        '<input type="hidden" name="done" value="1">'
        f'<input type="text" name="q" maxlength="80"{value} placeholder="查询已完成" aria-label="查询" autocomplete="off">'
        '<button class="btn" type="submit">查询</button>'
        "</form>"
    )


def _done_blocks(data, finding, today):
    for item in data["items"]:
        done_children = [child for child in item["children"] if child["done"]]
        parent_hit = _hit(item["text"], finding)
        if item["done"]:
            children = item["children"] if parent_hit or not finding else [
                child for child in item["children"] if _hit(child["text"], finding)
            ]
            child_hit = any(_hit(child["text"], finding) for child in item["children"])
            if finding and not parent_hit and not child_hit:
                continue
            yield _done_parent(item, children, today)
            continue
        shown = done_children if parent_hit or not finding else [
            child for child in done_children if _hit(child["text"], finding)
        ]
        if not shown:
            continue
        if finding and not parent_hit and not any(_hit(child["text"], finding) for child in done_children):
            continue
        yield _context_parent(item, shown)


def _done_parent(item, children, today):
    rows = "".join(_done_child(child) if child["done"] else _open_note(child) for child in children)
    subs = f'<ul class="plan-subs">{rows}</ul>' if rows else ""
    return (
        '<li class="plan"><div class="plan-line">'
        f'<p class="plan-text">{html.escape(item["text"])}{_when(item["when"], today)}</p>'
        f'{_mark_form(item["id"], "undo", "恢复", "done")}'
        f"</div>{subs}</li>"
    )


def _context_parent(item, children):
    rows = "".join(_done_child(child) for child in children)
    return (
        '<li class="plan"><div class="plan-line">'
        f'<p class="plan-text">{html.escape(item["text"])}<span class="badge">未完成</span></p>'
        f'</div><ul class="plan-subs">{rows}</ul></li>'
    )


def _done_child(child):
    return (
        '<li class="plan-sub">'
        f'<p class="plan-text">{html.escape(child["text"])}</p>'
        f'{_mark_form(child["id"], "undo", "恢复", "done")}'
        "</li>"
    )


def _open_note(child):
    return (
        '<li class="plan-sub">'
        f'<p class="plan-text">{html.escape(child["text"])}<span class="badge">未完成</span></p>'
        "</li>"
    )


def _routines(data):
    rows = "".join(_routine_row(routine) for routine in data["routines"])
    listing = f'<ul class="routine-list">{rows}</ul>' if rows else ""
    return (
        '<section class="routine-block"><h2>周期加入</h2>'
        '<p class="meta">到点后打开页面，事项会加到最后。同一天只加一次，漏掉的只补最近一次。'
        "每周看周几，每月看日期，例如每周五盘点。今天还没到点的，从下一次开始。</p>"
        f"{listing}{_routine_form()}</section>"
    )


def _routine_row(routine):
    return (
        '<li class="routine">'
        f'<p class="plan-text">{html.escape(_routine_label(routine))}</p>'
        '<form method="post" action="/work/" accept-charset="UTF-8">'
        '<input type="hidden" name="action" value="drop">'
        f'<input type="hidden" name="id" value="{html.escape(routine["id"])}">'
        '<button class="btn" type="submit">停止</button>'
        "</form></li>"
    )


def _routine_form():
    weeks = _options([(str(index), name) for index, name in enumerate(WEEKDAYS)], "4")
    days = _options([(str(day), f"{day}日") for day in range(1, 32)], "1")
    cycles = _options([("day", "每天"), ("week", "每周"), ("month", "每月")], "week")
    return (
        '<form class="routine-form" method="post" action="/work/" accept-charset="UTF-8">'
        '<input type="hidden" name="action" value="routine">'
        '<label class="field">事项'
        '<input type="text" name="text" maxlength="80" required placeholder="例如盘点" autocomplete="off">'
        "</label>"
        f'<label class="field">周期<select name="every">{cycles}</select></label>'
        f'<label class="field">周几<select name="weekday">{weeks}</select></label>'
        f'<label class="field">日期<select name="day">{days}</select></label>'
        '<button class="btn btn-primary" type="submit">加入周期</button>'
        "</form>"
    )


def _mark_form(ident, action, label, back):
    back_input = '<input type="hidden" name="back" value="done">' if back == "done" else ""
    return (
        '<form method="post" action="/work/" accept-charset="UTF-8">'
        f'<input type="hidden" name="action" value="{action}">'
        f'<input type="hidden" name="id" value="{html.escape(ident)}">'
        f"{back_input}"
        f'<button class="btn" type="submit">{label}</button>'
        "</form>"
    )


def _routine_label(routine):
    if routine["every"] == "day":
        when = "每天"
    elif routine["every"] == "week":
        when = "每" + WEEKDAYS[routine["weekday"]]
    else:
        when = f"每月{routine['day']}日"
    return f"{when} · {routine['text']}"


def _when(stamp, today):
    if not stamp:
        return ""
    try:
        year, month, day = (int(part) for part in stamp.split("-"))
        happened = date(year, month, day)
    except (TypeError, ValueError):
        return ""
    if happened.year == today.year:
        label = f"{happened.month}月{happened.day}日"
    else:
        label = f"{happened.year}年{happened.month}月{happened.day}日"
    return f'<span class="plan-when">{label}加入</span>'


def _options(pairs, selected):
    return "".join(
        f'<option value="{html.escape(value)}"{(" selected" if value == selected else "")}>{html.escape(label)}</option>'
        for value, label in pairs
    )


def _hit(text, finding):
    return not finding or finding in text


def _text(value):
    text = " ".join(str(value).split())
    if not text:
        return "", "empty"
    if len(text) > 80:
        return "", "long"
    return text, None


def _integer(value, low, high):
    try:
        number = int(str(value), 10)
    except (TypeError, ValueError):
        return None
    if low <= number <= high:
        return number
    return None


def _new_id(data):
    taken = set()
    for item in data["items"]:
        taken.add(item["id"])
        for child in item["children"]:
            taken.add(child["id"])
    for routine in data["routines"]:
        taken.add(routine["id"])
    while True:
        ident = secrets.token_hex(4)
        if ident not in taken:
            return ident


def _item(item):
    if not isinstance(item, dict):
        raise ValueError("station/work.json 里有一条事项读不懂")
    ident = item.get("id")
    text = item.get("text")
    done = item.get("done", False)
    when = item.get("when") or ""
    children = item.get("children") or []
    if (
        not isinstance(ident, str)
        or not ident
        or not isinstance(text, str)
        or not isinstance(done, bool)
        or not isinstance(when, str)
        or not isinstance(children, list)
    ):
        raise ValueError("station/work.json 里有一条事项读不懂")
    return {
        "id": ident,
        "text": text,
        "done": done,
        "when": when,
        "children": [_child(child) for child in children],
    }


def _child(child):
    if not isinstance(child, dict):
        raise ValueError("station/work.json 里有一条子计划读不懂")
    ident = child.get("id")
    text = child.get("text")
    done = child.get("done", False)
    if not isinstance(ident, str) or not ident or not isinstance(text, str) or not isinstance(done, bool):
        raise ValueError("station/work.json 里有一条子计划读不懂")
    return {"id": ident, "text": text, "done": done}


def _routine(routine):
    if not isinstance(routine, dict):
        raise ValueError("station/work.json 里有一条周期读不懂")
    ident = routine.get("id")
    text = routine.get("text")
    every = routine.get("every")
    weekday = routine.get("weekday", 0)
    day = routine.get("day", 1)
    last = routine.get("last") or ""
    if (
        not isinstance(ident, str)
        or not ident
        or not isinstance(text, str)
        or every not in ("day", "week", "month")
        or isinstance(weekday, bool)
        or not isinstance(weekday, int)
        or not 0 <= weekday <= 6
        or isinstance(day, bool)
        or not isinstance(day, int)
        or not 1 <= day <= 31
        or not isinstance(last, str)
    ):
        raise ValueError("station/work.json 里有一条周期读不懂")
    return {
        "id": ident,
        "text": text,
        "every": every,
        "weekday": weekday,
        "day": day,
        "last": last,
    }


def _first(query, key):
    values = query.get(key) or []
    return values[-1] if values else ""
