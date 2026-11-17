"""攒钱大挑战。

页面是一份完整的单页，记录在 station/piggy.json。
/piggy/ 套工作台外壳，里面嵌同一份页面。
/piggy/app/ 没有侧栏，给平板单独打开。两边读写同一份记录。
"""

import json
import threading
from pathlib import Path

DATA = Path(__file__).resolve().parent / "piggy.json"
PAGE = Path(__file__).resolve().parent / "piggy.html"
LOCK = threading.Lock()
DEFAULT = {
    "goal": {"name": "", "amount": 0},
    "records": [],
    "tasks": [],
    "completedTasks": [],
    "startDate": "",
}


def page():
    try:
        return PAGE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"station/piggy.html 读不了：{exc}") from exc


def read():
    with LOCK:
        return load()


def write(data):
    if not isinstance(data, dict):
        raise ValueError("请求体不是 JSON 对象")
    # 页面每次整份提交。类型不对就拒绝，避免把记录写成别的形状。
    if "goal" in data and not isinstance(data["goal"], dict):
        raise ValueError("攒钱目标格式不对")
    for key in ("records", "tasks", "completedTasks"):
        if key in data and not isinstance(data[key], list):
            raise ValueError("攒钱记录格式不对")
    if "startDate" in data and not isinstance(data["startDate"], str):
        raise ValueError("攒钱开始日期格式不对")
    with LOCK:
        save(data)


def load():
    if not DATA.is_file():
        return json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(DATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"station/piggy.json 读不了：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("station/piggy.json 不是一份攒钱记录")
    return raw


def save(data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = DATA.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(DATA)
