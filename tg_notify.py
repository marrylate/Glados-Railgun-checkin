# -*- coding: utf-8 -*-
"""
Telegram 推送模块（为 GLaDOS / Railgun 签到工作流新增，2026-10-07）

设计要点：
- 使用 Telethon StringSession（纯字符串，直接放 GitHub Secret），不依赖 session 文件。
- 通知频道用显式 InputPeerChannel(id, access_hash) 定位，不依赖 session 实体缓存。
- GitHub runner 可直连 Telegram；本地调试可设 TG_PROXY=socks5://127.0.0.1:7890。
- 推送失败只打印日志，绝不抛异常影响签到主流程。

所需环境变量（GitHub Secrets）：
  TG_API_ID / TG_API_HASH / TG_SESSION_STRING
  TG_NOTIFY_CHANNEL_ID   默认 2772337813
  TG_NOTIFY_CHANNEL_HASH 默认 -4486438815988276894

命令行用法（工作流兜底通知，已推送过则静默跳过）：
  python tg_notify.py --fallback "标题" ["正文"]
"""
import os
import sys
import asyncio
import tempfile

DEFAULT_NOTIFY_ID = "2772337813"
DEFAULT_NOTIFY_HASH = "-4486438815988276894"
FLAG_NAME = "tg_pushed.flag"


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
def _env(name, default=None):
    val = os.environ.get(name)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


def configured():
    """TG 推送所需的凭据是否齐备"""
    return bool(_env("TG_SESSION_STRING") and _env("TG_API_ID") and _env("TG_API_HASH"))


def bj_now():
    """北京时间（runner 为 UTC）"""
    import datetime

    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def run_url():
    """当前 Actions 运行页面地址（非 Actions 环境返回空串）"""
    server = _env("GITHUB_SERVER_URL")
    repo = _env("GITHUB_REPOSITORY")
    run_id = _env("GITHUB_RUN_ID")
    if server and repo and run_id:
        return "%s/%s/actions/runs/%s" % (server, repo, run_id)
    return ""


# --------------------------------------------------------------------------- #
# 已推送标记（避免兜底通知重复刷屏）
# --------------------------------------------------------------------------- #
def _flag_path():
    base = _env("RUNNER_TEMP") or tempfile.gettempdir()
    return os.path.join(base, FLAG_NAME)


def mark_pushed():
    try:
        with open(_flag_path(), "w", encoding="utf-8") as f:
            f.write("1")
    except Exception:
        pass


def already_pushed():
    try:
        return os.path.exists(_flag_path())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def _proxy():
    """TG_PROXY 形如 socks5://127.0.0.1:7890 / http://127.0.0.1:7890；空则直连"""
    raw = _env("TG_PROXY")
    if not raw:
        return None
    try:
        import socks

        scheme, _, hostport = raw.partition("://")
        host, _, port = hostport.rpartition(":")
        scheme = (scheme or "socks5").lower()
        table = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP, "https": socks.HTTP}
        if scheme in table:
            return (table[scheme], host, int(port))
    except Exception as e:
        print("[tg] 代理解析失败(%s)，改为直连: %s" % (raw, e), flush=True)
    return None


async def _send_async(text, attempts=3):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import InputPeerChannel

    client = TelegramClient(
        StringSession(_env("TG_SESSION_STRING")),
        int(_env("TG_API_ID")),
        _env("TG_API_HASH"),
        proxy=_proxy(),
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session 未登录/已失效，请重新导出 StringSession")
        peer = InputPeerChannel(
            int(_env("TG_NOTIFY_CHANNEL_ID", DEFAULT_NOTIFY_ID)),
            int(_env("TG_NOTIFY_CHANNEL_HASH", DEFAULT_NOTIFY_HASH)),
        )
        await client.send_message(peer, text)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def send_message(text, attempts=3):
    """同步发送，失败重试；任何异常都被吞掉只打日志，返回 bool"""
    if not configured():
        print("[tg] 未配置 TG_API_ID/TG_API_HASH/TG_SESSION_STRING，跳过 Telegram 推送", flush=True)
        return False

    last = None
    for i in range(1, attempts + 1):
        try:
            asyncio.run(_send_async(text))
            print("[tg] 已推送到「签到通知」频道（第 %d 次尝试）" % i, flush=True)
            return True
        except Exception as e:
            last = e
            print("[tg] 第 %d 次推送失败: %s: %s" % (i, type(e).__name__, e), flush=True)
            if i < attempts:
                import time

                time.sleep(3 * i)
    print("[tg] 推送最终失败: %s: %s" % (type(last).__name__, last), flush=True)
    return False


# --------------------------------------------------------------------------- #
# 供 checkin.py 调用
# --------------------------------------------------------------------------- #
def push_checkin_to_telegram(title, content):
    """
    把签到结果推到「签到通知」频道。
    title  形如 "GLaDOS 签到, 成功1, 失败0, 重复1"
    content 形如 "#1 P:5 剩余:60 总积分:1234 | 签到成功 | 未兑换"
    """
    if not configured():
        return False

    parts = ["🎫 %s" % (title or "GLaDOS 签到").strip()]
    body = (content or "").strip()
    if body:
        parts.append("")
        parts.append(body)
    parts.append("")
    parts.append("🕒 %s（北京时间）" % bj_now())
    text = "\n".join(parts)

    ok = send_message(text)
    if ok:
        mark_pushed()
    return ok


# --------------------------------------------------------------------------- #
# 兜底通知 CLI
# --------------------------------------------------------------------------- #
def _main(argv):
    if "--fallback" in argv:
        idx = argv.index("--fallback")
        rest = argv[idx + 1:]
        title = rest[0] if rest else "GLaDOS 签到异常"
        body = rest[1] if len(rest) > 1 else ""
        if already_pushed():
            print("[tg] 签到脚本已自行推送过，跳过兜底通知", flush=True)
            return 0
        url = run_url()
        if not body:
            body = "签到脚本未完成推送（可能秒退或崩溃），请查看 Actions 运行日志。"
        if url:
            body += "\n运行日志: %s" % url
        parts = ["⚠️ %s" % title, "", body, "", "🕒 %s（北京时间）" % bj_now()]
        ok = send_message("\n".join(parts))
        if ok:
            mark_pushed()
        return 0 if ok else 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
