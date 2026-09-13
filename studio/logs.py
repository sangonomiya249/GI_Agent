"""读 BetterGI 自己的日志（`BetterGI\\log\\*.log`）—— 排查"为什么没打起来"最有用的一手材料。

Studio 的「日志」页就是靠它：列出日志文件、取最新一份的尾部若干行、
把已知的坑高亮出来（这几条都是实测踩过的）：

* `战斗策略文件不存在` → 组里配的策略名在 `User\\AutoFight` 下没有对应 txt；
* `目标传送点位于不可点击区域，传送失败` → 个别锚点点不到，该条路线会失败；
* `脚本 "X" 状态为禁用，跳过执行` → 大组里躺着大量 Disabled，日志秒级爆发；
* `当前获取焦点的窗口不是原神` → 跑图时切了窗口，BetterGI 会暂停甚至中止；
* `0xc00000fd` / `StackOverflow` → WPF 层栈溢出崩溃（日志爆发之后的连锁反应）。
"""

import os
import re

HIGHLIGHT_RULES = (
    (("战斗策略文件不存在",), "error", "战斗策略缺失：组里的策略名在 User\\AutoFight 下找不到 txt"),
    (("0xc00000fd", "StackOverflow", "栈溢出"), "error", "BetterGI 崩溃（WPF 栈溢出）"),
    (("目标传送点位于不可点击区域", "传送失败"), "warn", "传送点不可点击：该条路线会失败"),
    (("不是原神", "获取焦点的窗口"), "warn", "窗口焦点不在原神：BetterGI 会暂停/中止"),
    (("状态为禁用，跳过执行",), "info", "大量禁用脚本：大组精简能减少这类日志爆发"),
    (("异常", "Exception", "错误"), "warn", "异常/错误"),
)

# 泛泛的命中（日志里到处是「错误」两个字）：找重点问题时排在最后
GENERIC_NOTES = ("异常/错误",)
LOG_SUFFIXES = (".log",)


def log_dir(bgi_dir):
    return os.path.join(str(bgi_dir), "log")


def list_log_files(bgi_dir, limit=50):
    """按修改时间倒序列出**日志文件**。

    只认 `.log`：BetterGI 的 log 目录里还混着各种识别失败的截图
    （`avatar_side_classify_error.png` 之类），不过滤的话下拉框里全是图片。
    """
    directory = log_dir(bgi_dir)
    if not os.path.isdir(directory):
        return []
    rows = []
    for name in os.listdir(directory):
        if not name.lower().endswith(LOG_SUFFIXES):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        rows.append({"name": name, "size": stat.st_size, "mtime": stat.st_mtime, "path": path})
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit]


def rule_for(line):
    """一条日志命中哪条已知规则 → (级别, 说明)；没命中返回 (None, "")。"""
    for keywords, level, note in HIGHLIGHT_RULES:
        if any(keyword in line for keyword in keywords):
            return level, note
    return None, ""


def read_tail(path, lines=200, keyword=""):
    """取日志尾部若干行（带行号与命中的问题标记）。

    `keyword` 非空时只保留包含它的行（大小写不敏感），但**行号保留原文件里的行号**，
    方便玩家拿行号去 BetterGI 的日志窗口里对照。
    """
    lines = max(1, min(int(lines or 200), 5000))
    path = str(path)
    if not os.path.isfile(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError:
        return []

    keyword = str(keyword or "").strip().lower()
    start = max(0, len(content) - lines)
    rows = []
    for offset, raw in enumerate(content[start:], start=start + 1):
        text = raw.rstrip("\r\n")
        if keyword and keyword not in text.lower():
            continue
        level, note = rule_for(text)
        rows.append({"n": offset, "text": text, "level": level, "note": note})
    return rows


def summarise(rows):
    """统计尾部日志里各类问题的条数（给"最近有问题"的角标用）。"""
    counter = {}
    for row in rows:
        if row.get("note"):
            counter[row["note"]] = counter.get(row["note"], 0) + 1
    return counter


def latest_log_path(bgi_dir):
    files = list_log_files(bgi_dir, limit=1)
    return files[0]["path"] if files else None


def parse_progress(bgi_dir):
    """尝试读 BetterGI 的 task_progress（有些版本会写进度），读不到就返回空。"""
    directory = os.path.join(log_dir(bgi_dir), "task_progress")
    if not os.path.isdir(directory):
        return []
    rows = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                rows.append({"name": name, "content": handle.read()[:2000]})
        except OSError:
            continue
    return rows


def find_first_problem(rows):
    """尾部日志里最值得先看的一条。

    优先"说得出原因"的具体问题（战斗策略缺失 / 崩溃 / 传送失败 / 焦点丢失），
    泛泛的「异常/错误」只在没有其它命中时才拿出来。
    """
    def specific(row):
        return row.get("level") in ("error", "warn") and row.get("note") not in GENERIC_NOTES

    for predicate in (
        lambda row: specific(row) and row.get("level") == "error",
        lambda row: specific(row),
        lambda row: row.get("level") == "error",
        lambda row: row.get("level") == "warn",
    ):
        for row in reversed(rows):
            if predicate(row):
                return row
    return None


__all__ = [
    "HIGHLIGHT_RULES",
    "find_first_problem",
    "latest_log_path",
    "list_log_files",
    "log_dir",
    "parse_progress",
    "read_tail",
    "rule_for",
    "summarise",
]
