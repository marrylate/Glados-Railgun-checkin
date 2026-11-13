# -*- coding: utf-8 -*-
"""Cloudflare Turnstile 探测（GitHub Actions / 任意 Linux + xvfb 环境）

目的：判断云端 runner 能不能过 GLaDOS 签到的 Turnstile。
做法与验证过的本机方案一致 —— 自己取 challenge、自己渲染 widget 拿 token，
然后把 token 丢掉、只用 GET 探一次 challenge 是否放 token。

环境变量：
  GLADOS_COOKIES  必填，Cookie 头字符串
  PROBE_POST=1    可选，拿 workflow_dispatch 手动触发
"""
import hashlib
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

SITE = "https://glados.cloud"
PAGE = SITE + "/console/checkin"
SUFFIX = "glados-observe-v1"

CHALLENGE_JS = r"""
async () => {
  const r = await fetch('/api/user/checkin/challenge', {credentials: 'include', headers: {'accept': 'application/json'}});
  let d = null; try { d = await r.json(); } catch (e) {}
  return {status: r.status, body: d};
}
"""

RENDER_JS = r"""
async (args) => {
  const out = {ok: false};
  window.__tok = null; window.__err = null; window.__int = false;
  try {
    if (!window.turnstile) {
      await new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
        s.async = true; s.onload = res; s.onerror = () => rej(new Error('api.js load failed'));
        document.head.appendChild(s);
      });
    }
    if (!window.turnstile) { out.err = 'turnstile missing'; return out; }
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;top:30%;left:50%;transform:translate(-50%,-50%);z-index:2147483647;background:#fff;padding:8px;';
    document.body.appendChild(box);
    window.turnstile.render(box, {
      sitekey: args.sitekey, action: 'checkin', cData: args.nonce, appearance: 'always',
      callback: (t) => { window.__tok = t; },
      'error-callback': (e) => { window.__err = String(e); },
      'before-interactive-callback': () => { window.__int = true; },
      'timeout-callback': () => { window.__err = (window.__err || '') + '|timeout'; }
    });
    out.ok = true;
  } catch (e) { out.err = String(e); }
  return out;
}
"""

POST_JS = r"""
async (payload) => {
  const r = await fetch('/api/user/checkin', {
    method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const t = await r.text();
  let b = null; try { b = JSON.parse(t); } catch (e) {}
  return {status: r.status, body: b, raw: b ? null : t.slice(0, 200)};
}
"""


def main():
    ck = (os.environ.get("GLADOS_COOKIES") or "").strip()
    if not ck:
        print("MISSING GLADOS_COOKIES")
        return 2
    pairs = dict(p.strip().split("=", 1) for p in ck.split(";") if "=" in p)
    do_post = os.environ.get("PROBE_POST") == "1"
    out = {"headless_env": not bool(os.environ.get("DISPLAY")), "display": os.environ.get("DISPLAY")}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # xvfb 下就是“有头”
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--mute-audio"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="zh-CN", timezone_id="Asia/Shanghai"
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.add_cookies([{"name": k, "value": v, "domain": ".glados.cloud", "path": "/"} for k, v in pairs.items()])
        page = ctx.new_page()
        page.goto(PAGE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        out["url"] = page.url

        ch = page.evaluate(CHALLENGE_JS)
        out["challenge"] = ch.get("body")
        t_ch = time.time()
        body = ch.get("body") or {}
        if body.get("code") != 0:
            out["result"] = "challenge_failed"
            print(json.dumps(out, ensure_ascii=False))
            return 1

        nonce = body.get("nonce") or ""
        sitekey = body.get("turnstile") or ""
        out["sitekey"] = sitekey

        token = ""
        if sitekey:
            r = page.evaluate(RENDER_JS, {"sitekey": sitekey, "nonce": nonce})
            out["render"] = r
            t0 = time.time()
            while time.time() - t0 < 120:
                st = page.evaluate("() => ({tok: window.__tok, err: window.__err, int: window.__int})")
                if st.get("tok"):
                    token = st["tok"]
                    out["token_seconds"] = int(time.time() - t0)
                    break
                if st.get("err"):
                    out["turnstile_error"] = st["err"]
                    break
                page.wait_for_timeout(800)
            out["interactive_required"] = bool(page.evaluate("window.__int"))
        out["got_token"] = bool(token)

        if do_post:
            waited = time.time() - t_ch
            if waited < 2.5:
                time.sleep(2.5 - waited)
            payload = {
                "token": "glados.cloud",
                "nonce": nonce,
                "proof": hashlib.sha256(("%s.%s" % (nonce, SUFFIX)).encode()).hexdigest(),
                "tsState": "ok" if token else "off",
            }
            if token:
                payload["turnstile"] = token
            out["post"] = page.evaluate(POST_JS, payload)

        try:
            page.screenshot(path="/tmp/probe.png", full_page=True)
        except Exception:
            pass
        ctx.close()
        browser.close()

    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("got_token") else 1


if __name__ == "__main__":
    sys.exit(main())
