#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上游更新监测（2026-10-07 新增）

背景
----
本仓库 fork 自 Devilstore/Glados-Railgun-checkin，但做过大量自定义改动
（Telegram 推送、只签 glados.cloud、积分不足跳过兑换、cron 调整等）。

fork 是静态副本：上游更新不会自动同步进来。而手工点 "Sync fork" 会因为
两边都改过 checkin.py / gladosCheck.yml 而冲突，选错选项会把我们的改动冲掉。

所以本脚本只做一件事：每周检查上游有没有新提交，有就推一条 Telegram 通知，
由人决定要不要手工合并。**不自动合并，也不向上游写任何东西。**

所需环境变量（与 tg_notify.py 相同）
-----------------------------------
  TG_API_ID / TG_API_HASH / TG_SESSION_STRING
  TG_NOTIFY_CHANNEL_ID   默认 2772337813
  TG_NOTIFY_CHANNEL_HASH 默认 -4486438815988276894
可选：GITHUB_TOKEN（提升 GitHub API 限额；workflow 中自动注入）

退出码：0 = 正常（含"无更新"）；1 = 查询上游失败
"""
import datetime
import json
import os
import pathlib
import sys

import requests

try:
    import tg_notify
except Exception:  # pragma: no cover - 本地缺依赖时不致命
    tg_notify = None

UPSTREAM = "Devilstore/Glados-Railgun-checkin"
UPSTREAM_BRANCH = "master"
STATE_FILE = pathlib.Path(__file__).resolve().parent / "upstream_state.json"
API = "https://api.github.com"
MAX_LIST = 40          # 列表接口最多取多少条
MAX_SHOW = 8           # 通知里最多列几条提交

# 这些文件我们改过，上游一动就必须手工合并
OUR_TOUCHED = (
    "checkin.py",
    "logging_config.py",
    ".github/workflows/gladosCheck.yml",
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _token():
    for key in ("UPSTREAM_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    return None


def gh_get(path, **params):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "glados-upstream-watch",
    }
    tok = _token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    resp = requests.get(API + path, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError("GitHub API %s -> HTTP %s: %s" % (path, resp.status_code, resp.text[:200]))
    return resp.json()


def bj_now():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as err:
            print("状态文件解析失败(%s)，按首次运行处理" % err, flush=True)
    return {}


def save_state(sha, new_count=0):
    data = load_state()
    data["upstream"] = UPSTREAM
    data["last_seen_sha"] = sha
    data["last_check"] = bj_now()
    data["last_new_commits"] = new_count
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify(text):
    if tg_notify is None:
        print("[tg] tg_notify 模块不可用，仅打印：\n%s" % text, flush=True)
        return False
    return tg_notify.send_message(text)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main():
    try:
        commits = gh_get("/repos/%s/commits" % UPSTREAM, sha=UPSTREAM_BRANCH, per_page=MAX_LIST)
    except Exception as err:
        print("查询上游失败: %s" % err, flush=True)
        return 1

    if not isinstance(commits, list) or not commits:
        print("上游提交列表为空，跳过本次检查", flush=True)
        return 0

    latest = commits[0]["sha"]
    state = load_state()
    seen = (state.get("last_seen_sha") or "").strip()

    # 首次运行：只记录基线，不发通知（避免一上线就误报）
    if not seen:
        print("首次运行：记录基线 %s，本次不通知" % latest[:8], flush=True)
        save_state(latest, 0)
        return 0

    if seen == latest:
        print("上游无更新（仍为 %s）" % latest[:8], flush=True)
        save_state(latest, 0)
        return 0

    # 用 compare 一次拿到提交列表与改动文件；上游 force push 导致旧 sha 消失时退回列表接口
    new_commits = []
    changed_files = []
    total = 0
    try:
        cmp = gh_get("/repos/%s/compare/%s...%s" % (UPSTREAM, seen, latest))
        new_commits = cmp.get("commits", []) or []
        changed_files = [f.get("filename", "") for f in (cmp.get("files") or [])]
        total = int(cmp.get("total_commits") or len(new_commits))
    except Exception as err:
        print("compare 接口失败(%s)，退回提交列表" % err, flush=True)
        for c in commits:
            if c["sha"] == seen:
                break
            new_commits.append(c)
        total = len(new_commits)

    print("上游有 %d 个新提交，最新 %s" % (total, latest[:8]), flush=True)

    # 组装通知
    lines = ["🔔 GLaDOS 上游有更新", ""]
    lines.append("上游：%s" % UPSTREAM)
    lines.append("新增：%d 个提交（最新 %s）" % (total if total else len(new_commits), latest[:8]))
    lines.append("")

    show = new_commits[-MAX_SHOW:] if len(new_commits) > MAX_SHOW else new_commits
    for c in show:
        sha = c.get("sha", "")[:8]
        msg = (c.get("commit", {}).get("message") or "").splitlines()[0][:78]
        date = ((c.get("commit", {}).get("author") or {}).get("date") or "")[:10]
        lines.append("· %s %s %s" % (date, sha, msg))
    if len(new_commits) > MAX_SHOW:
        lines.append("· …（共 %d 个，仅列最近 %d 个）" % (len(new_commits), MAX_SHOW))

    hit = [f for f in changed_files if f in OUR_TOUCHED]
    lines.append("")
    if hit:
        lines.append("⚠️ 改到了我们自定义过的文件：")
        for f in hit:
            lines.append("   · %s" % f)
        lines.append("")
        lines.append("需要手工合并，千万别点 Sync fork（会冲突并冲掉我们的改动）。")
    else:
        lines.append("未涉及我们自定义过的文件，暂时可以不动。")

    if changed_files:
        lines.append("")
        lines.append("改动文件（%d 个）：%s" % (
            len(changed_files),
            "、".join(changed_files[:6]) + ("…" if len(changed_files) > 6 else ""),
        ))

    lines.append("")
    lines.append("想要合并时对 WorkBuddy 说一句「看看上游有没有更新」即可。")
    lines.append("")
    lines.append("🕒 %s（北京时间）" % bj_now())

    ok = notify("\n".join(lines))
    print("通知发送: %s" % ("成功" if ok else "失败/跳过"), flush=True)

    save_state(latest, total if total else len(new_commits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
