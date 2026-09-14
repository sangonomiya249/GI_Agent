"""版本检测：本地版本 vs GitHub 上的最新 release。

为什么用 release 而不是比对提交：玩家拿到的多是**解压出来的目录**（没有 `.git`），
release 的 tag（`v1.0.1`）是唯一稳定的版本号；仓库有没有新东西，看 `releases/latest` 最省事。

三条口径：
* **绝不阻塞**：离线 / 被墙 / 限流 / 仓库还没发布过 release，都只影响这一行提示，
  不抛异常、不影响 Agent 跑图（调用方拿到的永远是一个可打印的结果）；
* **不频繁打网络**：结果写进 `memory/update_check.json`，
  `UPDATE_CHECK_HOURS`（默认 6 小时）内直接复用缓存，`force=True` 才重新问；
* **只读**：只看 release 信息，不下载、不改任何文件 —— 要不要更新、怎么更新由玩家决定，
  这里只给出命令与链接。

本地版本从哪来（`local_version()`）：仓库根的 `VERSION` 文件优先；没有就退回
`git describe --tags --always`（只读，5 秒超时）；都没有就报"未知"，此时只显示 release 信息、
不下"有没有新版"的结论。

命令行：`python -m skills.update_check [--force] [--json]`（等价 `python main.py update`）。
"""
import argparse
import datetime
import json
import os
import re
import subprocess

import config

API_ROOT = "https://api.github.com"
# 默认检查的仓库；fork 出去的人改 .env 的 UPDATE_REPO 即可（不用改代码）
DEFAULT_REPO = "sangonomiya249/GI_Agent"
CACHE_PARTS = ("memory", "update_check.json")
USER_AGENT = "GI-Agent-update-check"
_NOTES_MAX_LINES = 12
_NOTES_MAX_CHARS = 160

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.\-]+))?$")
_DESCRIBE_RE = re.compile(r"^v?\d+(?:\.\d+)*-\d+-g")


# ==========================================
# 🌟 本地版本
# ==========================================

def repo_slug():
    """要检查的仓库（owner/repo）。默认取 `config.UPDATE_REPO`，方便 fork 的用户改。"""
    slug = str(getattr(config, "UPDATE_REPO", "") or "").strip()
    return slug or DEFAULT_REPO


def cache_path():
    """检查结果的缓存文件（`memory/update_check.json`）。"""
    return config.project_path(*CACHE_PARTS)


def _git_describe():
    """`git describe --tags --always`（只读）。没有 git / 不是仓库时给空串。"""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=config.PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def local_version():
    """→ (版本号, 来源)。`VERSION` 文件优先，其次 git describe，都没有给 ("", "未知")。"""
    path = config.project_path("VERSION")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        text = ""
    if text:
        return text.splitlines()[0].strip(), "VERSION"
    described = _git_describe()
    if described:
        return described, "git describe"
    return "", "未知"


# ==========================================
# 🌟 版本号比较
# ==========================================

def parse_version(text):
    """版本号 → ((主, 次, 修), 预发布后缀)。认不出来给 ((), "")。

    `v1.0.0` / `1.0` / `v1.0.1-rc.1` 都认；`v1.0.0-3-gabc1234`（git describe）
    取开头的 `1.0.0`，后缀留空。
    """
    text = str(text or "").strip()
    if not text:
        return (), ""
    match = _VERSION_RE.match(text)
    if match:
        return tuple(int(part) for part in match.group(1).split(".")), (match.group(2) or "")
    match = re.match(r"^v?(\d+(?:\.\d+)*)", text)
    if match:
        return tuple(int(part) for part in match.group(1).split(".")), ""
    return (), ""


def compare_versions(latest, local):
    """→ (状态, 说明)。状态取 `newer` / `same` / `older` / `unknown`。

    `older` = 本地比 release 还新（自己在 tag 之后又提交了），这时不该提示"有新版"。
    """
    latest_nums, _latest_pre = parse_version(latest)
    local_nums, _local_pre = parse_version(local)
    if not latest_nums or not local_nums:
        return "unknown", "版本号认不出来，只能你自己看着办"
    if latest_nums > local_nums:
        return "newer", f"GitHub 上是 {latest}，本地是 {local}"
    if latest_nums < local_nums:
        return "older", f"本地（{local}）比 release（{latest}）还新"
    if _DESCRIBE_RE.match(str(local or "")):
        return "same", "本地是在这个 tag 之后自己编译/提交的，比它多几个提交"
    return "same", "本地版本和最新 release 一致"


# ==========================================
# 🌟 问 GitHub
# ==========================================

def _trim_notes(body):
    """release 说明掐一段能看的（前 12 个非空行、每行 160 字）。"""
    lines = [line.rstrip() for line in str(body or "").replace("\r\n", "\n").split("\n")]
    kept = [line for line in lines if line.strip()][:_NOTES_MAX_LINES]
    return "\n".join(line[:_NOTES_MAX_CHARS] for line in kept)


def _explain_404(slug, session, timeout):
    """404 到底是"仓库没发布过 release"还是"仓库不存在/私有" —— 再问一次仓库接口。"""
    try:
        probe = session.get(f"{API_ROOT}/repos/{slug}", timeout=timeout)
    except Exception as exc:            # noqa: BLE001
        return f"拿不到 release 信息（{type(exc).__name__}）"
    if probe.status_code == 200:
        return "这个仓库还没有发布过 release（发布时打的 tag 形如 v1.0.0）"
    if probe.status_code == 404:
        return f"仓库 {slug} 不存在或不是公开仓库（检查 UPDATE_REPO 填对没有）"
    return f"拿不到 release（仓库接口返回 {probe.status_code}）"


def fetch_latest(repo=None, timeout=None):
    """问 GitHub 要最新 release → (release 字典 或 None, 错误说明)。

    ⚠️ 只用 GitHub 公开 API（匿名每小时 60 次），所以调用方必须缓存 —— 见 `check()`。
    """
    slug = str(repo or repo_slug()).strip()
    if not slug or "/" not in slug:
        return None, "仓库地址要写成 owner/repo（例如 sangonomiya249/GI_Agent）"

    try:
        import requests
    except Exception as exc:            # noqa: BLE001 —— 依赖缺失不该让更新检查变成崩溃
        return None, f"没有 requests 库：{exc}"

    timeout = int(timeout or getattr(config, "UPDATE_CHECK_TIMEOUT", 6) or 6)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    session = requests.Session()
    session.headers.update(headers)
    try:
        response = session.get(f"{API_ROOT}/repos/{slug}/releases/latest", timeout=timeout)
    except Exception as exc:            # noqa: BLE001 —— 离线/被墙/代理问题都走这里
        return None, f"连不上 GitHub（{type(exc).__name__}）—— 离线或代理没配好时正常"

    if response.status_code == 404:
        return None, _explain_404(slug, session, timeout)
    if response.status_code == 403:
        return None, "GitHub 限流了（匿名每小时 60 次）—— 过一会儿再试"
    if response.status_code != 200:
        return None, f"GitHub 返回 {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return None, "GitHub 返回的不是 JSON"
    if not isinstance(payload, dict):
        return None, "GitHub 返回的结构不对"

    return {
        "tag": str(payload.get("tag_name") or ""),
        "name": str(payload.get("name") or ""),
        "url": str(payload.get("html_url") or ""),
        "notes": _trim_notes(payload.get("body")),
        "published_at": str(payload.get("published_at") or ""),
        "prerelease": bool(payload.get("prerelease")),
    }, ""


# ==========================================
# 🌟 缓存
# ==========================================

def load_cache():
    """读缓存（坏文件/不存在都给空字典）。"""
    try:
        with open(cache_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(payload):
    """写缓存（尽力而为：写不进去也不影响检查结果）。"""
    target = cache_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def _cache_age_hours(cached, now):
    raw = str((cached or {}).get("checked_at") or "")
    try:
        stamp = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, (now - stamp).total_seconds() / 3600)


def update_hint():
    """给玩家一句"怎么更新"（git 仓库和 zip 用户不一样）。"""
    if os.path.isdir(config.project_path(".git")):
        return "在项目目录里执行 git pull（有本地改动先 git stash）；或者到 release 页面下载 zip 覆盖"
    return "到 release 页面下载 zip，解压覆盖到项目目录（.env、memory\\ 别覆盖）"


# ==========================================
# 🌟 检查
# ==========================================

def _result(**kwargs):
    base = {
        "ok": True,
        "status": "error",
        "update_available": False,
        "local_version": "",
        "local_source": "",
        "latest_version": "",
        "latest_name": "",
        "latest_url": "",
        "published_at": "",
        "notes": "",
        "prerelease": False,
        "headline": "",
        "detail": "",
        "error": "",
        "checked_at": "",
        "from_cache": False,
        "cache_age_hours": None,
        "repo": repo_slug(),
        "update_hint": update_hint(),
    }
    base.update(kwargs)
    return base


def check(force=False, repo=None, now=None, fetch=None):
    """检查有没有新版本（默认吃 6 小时缓存）。**永不抛异常**。

    `fetch` 只是为了测试：签名 `fetch(repo) -> (release 或 None, 错误说明)`。
    """
    now = now or datetime.datetime.now()
    slug = str(repo or repo_slug()).strip()
    local, local_source = local_version()

    if not getattr(config, "UPDATE_CHECK", True):
        return _result(
            status="disabled", headline="📴 更新检查已关闭（UPDATE_CHECK=0）",
            local_version=local, local_source=local_source, repo=slug,
            checked_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        )

    ttl = int(getattr(config, "UPDATE_CHECK_HOURS", 6) or 6)
    cached = load_cache()
    age = _cache_age_hours(cached, now)
    if not force and cached and cached.get("repo") == slug and age is not None and age < ttl:
        return _result(
            status=str(cached.get("status") or "unknown"),
            update_available=bool(cached.get("update_available")),
            local_version=local or str(cached.get("local_version") or ""),
            local_source=local_source,
            latest_version=str(cached.get("latest_version") or ""),
            latest_name=str(cached.get("latest_name") or ""),
            latest_url=str(cached.get("latest_url") or ""),
            published_at=str(cached.get("published_at") or ""),
            notes=str(cached.get("notes") or ""),
            prerelease=bool(cached.get("prerelease")),
            headline=str(cached.get("headline") or ""),
            detail=str(cached.get("detail") or ""),
            checked_at=str(cached.get("checked_at") or ""),
            from_cache=True,
            cache_age_hours=round(age, 2),
            repo=slug,
        )

    release, error = None, ""
    try:
        release, error = (fetch or fetch_latest)(slug)
    except Exception as exc:            # noqa: BLE001 —— 检查更新永远不该把上层带崩
        release, error = None, f"检查时出错了（{type(exc).__name__}: {exc}）"
    if not release:
        return _result(
            ok=False, status="error", local_version=local, local_source=local_source,
            error=error, headline=f"📡 检查更新失败：{error}",
            checked_at=now.strftime("%Y-%m-%d %H:%M:%S"), repo=slug,
        )

    latest = release.get("tag") or ""
    status, detail = compare_versions(latest, local)
    if status == "newer":
        headline = f"🎉 有新版本 {latest}（本地 {local or '未知'}）"
    elif status == "same":
        headline = f"✅ 已是最新（{local or latest}）"
    elif status == "older":
        headline = f"🧪 本地比 release 还新（本地 {local} > {latest}）"
    else:
        headline = f"❔ 版本号认不出来（本地 {local or '未知'} / 最新 {latest or '未知'}）"

    payload = {
        "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": slug,
        "status": status,
        "update_available": status == "newer",
        "local_version": local,
        "latest_version": latest,
        "latest_name": release.get("name") or "",
        "latest_url": release.get("url") or "",
        "published_at": release.get("published_at") or "",
        "notes": release.get("notes") or "",
        "prerelease": bool(release.get("prerelease")),
        "headline": headline,
        "detail": detail,
    }
    save_cache(payload)

    return _result(
        status=status,
        update_available=status == "newer",
        local_version=local,
        local_source=local_source,
        latest_version=latest,
        latest_name=str(release.get("name") or ""),
        latest_url=str(release.get("url") or ""),
        published_at=str(release.get("published_at") or ""),
        notes=str(release.get("notes") or ""),
        prerelease=bool(release.get("prerelease")),
        headline=headline,
        detail=detail,
        checked_at=payload["checked_at"],
        repo=slug,
    )


def format_lines(result):
    """给人看的多行文本（CLI / 终端 / 体检都用它）。"""
    result = result or {}
    lines = [str(result.get("headline") or "未知")]
    if result.get("detail") and result.get("status") in ("newer", "older", "unknown"):
        lines.append(f"   {result['detail']}")
    if result.get("latest_version"):
        when = str(result.get("published_at") or "")[:10]
        name = str(result.get("latest_name") or "")
        suffix = f"「{name}」" if name and name != result["latest_version"] else ""
        lines.append(f"   最新 release：{result['latest_version']}{suffix}"
                     f"{f'（{when} 发布）' if when else ''}")
    if result.get("latest_url"):
        lines.append(f"   发布页：{result['latest_url']}")
    if result.get("status") == "newer":
        lines.append(f"   更新方式：{result.get('update_hint') or update_hint()}")
    if result.get("notes"):
        lines.append("   更新说明：")
        lines.extend(f"     {line}" for line in str(result["notes"]).splitlines())
    if result.get("from_cache"):
        age = result.get("cache_age_hours")
        lines.append(f"   （来自缓存，{age:.1f} 小时前查的；加 --force 立刻重查）" if isinstance(age, float)
                     else "   （来自缓存；加 --force 立刻重查）")
    if result.get("status") == "error":
        lines.append("   ↳ 只有这一行受影响：Agent / Studio / 跑图都不需要网络，照常用。")
    return lines


def main(argv=None):
    """命令行入口：`python -m skills.update_check [--force] [--json]`。"""
    parser = argparse.ArgumentParser(description="检查有没有新版本（GitHub release）")
    parser.add_argument("--force", action="store_true", help="忽略缓存，立刻问一次 GitHub")
    parser.add_argument("--repo", default="", help="覆盖 UPDATE_REPO（owner/repo）")
    parser.add_argument("--json", action="store_true", help="按 JSON 输出（给脚本用）")
    args = parser.parse_args(argv)

    result = check(force=args.force, repo=args.repo or None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("📦 GI_Agent 版本检查")
        print(f"   本地版本：{result.get('local_version') or '未知'}"
              f"（{result.get('local_source') or '未知来源'}）")
        for line in format_lines(result):
            print(line if line.startswith("   ") else f"   {line}")
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
