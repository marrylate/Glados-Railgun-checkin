import hashlib
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import requests
from pypushdeer import PushDeer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logging_config import init_logger

# ---- Telegram 推送（fork 定制，2026-10-07）----
try:
    from tg_notify import (
        checkin_done_today,
        mark_checkin_done,
        mark_pushed,
        mark_waiting,
        push_checkin_to_telegram,
        push_plain_text,
        pushed_today,
        waiting_today,
    )
except Exception:  # 模块缺失时不阻塞签到主流程
    push_checkin_to_telegram = None
    push_plain_text = None
    pushed_today = None
    mark_pushed = None
    checkin_done_today = None
    mark_checkin_done = None
    mark_waiting = None
    waiting_today = None


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    SKIPPED = 2  # 2026-11-13：线路加成档位不够，本次主动不签（不是失败，也不写「今日已完成」）
    FAILURE = -2


class ExchangePlan(Enum):
    """兑换计划"""

    PLAN100 = "plan100"
    PLAN200 = "plan200"
    PLAN500 = "plan500"


class APIEndpoint(Enum):
    """API端点"""

    CHECKIN = "/api/user/checkin"
    CHECKIN_CHALLENGE = "/api/user/checkin/challenge"
    NETWORK_INSIGHT = "/api/user/network-insight"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    PENDING = "⏳"
    CHECKIN = "🎫"
    STATUS = "📊"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️"
    ERROR = "🔴"
    INFO = "ℹ️"


def log_method(func):
    """日志装饰器"""

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji_map = {
            "checkin": LogEmoji.CHECKIN,
            "get_status": LogEmoji.STATUS,
            "get_points": LogEmoji.POINTS,
            "exchange": LogEmoji.EXCHANGE,
        }
        emoji = emoji_map.get(method_name, LogEmoji.INFO)
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {LogEmoji.ERROR} {method_name} 执行失败: {e}")

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": ""},
                "get_status": ("None 天", -2),
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                error_template = DEFAULT_ERRORS[method_name]
                if isinstance(error_template, dict):
                    error_result = error_template.copy()
                    error_result["message"] = f"执行失败: {e}"
                    return error_result
                return error_template
            raise

    return wrapper


class Config:
    """应用配置"""

    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_SERVERCHAN_KEY = "SERVERCHAN_SENDKEY"
    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
    ENV_VERBOSE = "GLADOS_VERBOSE"

    # 2026-11-13 新增：线路加成（Cable）闸门
    #   服务端结算 = Base + 套餐 + 连续 + Cable(+1~+9) + Lucky + Site + ExitIP。
    #   Cable 按 China Network Insight 指数分档（<90→9、90-94→7、95-99→5、
    #   100-109→3、110-130→2、>130→1），线路越差给得越多。
    #   所以签到前先探当前档位，档位不够就先不签，留到下一个 cron 点再探。
    # 2026-11-14 新增：Cloudflare Turnstile 打码服务（云端签到唯一可行路径）
    #   GLaDOS 签到加了 Turnstile：token 只能由真浏览器拿。GitHub Actions 上实测
    #   Turnstile 直接判「需要人工交互」且不建 iframe，云端自己算不出来；
    #   唯一能把签到留在云端的办法是把这一小步外包给打码平台。
    #   配置 SOLVER_API_KEY 即自动启用（默认 capsolver），不配就回到「交本机处理」。
    ENV_SOLVER = "GLADOS_SOLVER"          # capsolver / 2captcha（留空自动判断）
    ENV_SOLVER_KEY = "SOLVER_API_KEY"     # 打码平台 API key

    ENV_CABLE_MODE = "GLADOS_CABLE_MODE"
    ENV_CABLE_MIN = "GLADOS_CABLE_MIN"
    ENV_CABLE_FALLBACK_HOUR = "GLADOS_CABLE_FALLBACK_HOUR"

    """闸门开关：on 时启用「档位不够就等」；off 恢复原来的「到点就签」"""
    DEFAULT_CABLE_MODE = True
    """档位阈值：探到的 Cable 加成 >= 该值才签（7 = 指数 <95 档）"""
    DEFAULT_CABLE_MIN = 7
    """保底小时（北京时间）：到这个点之后不管档位一律签，避免断连续天数"""
    DEFAULT_CABLE_FALLBACK_HOUR = 20

    """默认兑换计划（none 表示不自动兑换）"""
    DEFAULT_EXCHANGE_PLAN = "none"

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    """默认域名（fork 定制：可用环境变量 GLADOS_DOMAINS 覆盖，逗号分隔）
    说明：railgun.info 与 glados.cloud 是两套独立账号体系，需要各自的 Cookie；
    默认只跑 glados.cloud，需要 railgun 时设置 GLADOS_DOMAINS=glados.cloud,railgun.info
    """
    DEFAULT_DOMAINS = ["glados.cloud"]
    ENV_DOMAINS = "GLADOS_DOMAINS"
    DOMAINS = DEFAULT_DOMAINS

    """兑换计划列表"""
    EXCHANGE_PLANS = {
        ExchangePlan.PLAN100.value: 100,
        ExchangePlan.PLAN200.value: 200,
        ExchangePlan.PLAN500.value: 500,
    }

    def __init__(self):
        self.push_key: str = ""
        self.serverchan_key: str = ""
        self.cookies_list: List[str] = []
        self.exchange_plan: str = self.DEFAULT_EXCHANGE_PLAN
        self.verbose: bool = self.DEFAULT_VERBOSE
        self.cable_mode: bool = self.DEFAULT_CABLE_MODE
        self.cable_min: int = self.DEFAULT_CABLE_MIN
        self.cable_fallback_hour: int = self.DEFAULT_CABLE_FALLBACK_HOUR
        self.solver_provider: str = ""
        self.solver_key: str = ""
        self._load_config()

    def _load_config(self) -> None:
        """加载配置"""
        push_key_env: Optional[str] = os.environ.get(self.ENV_PUSH_KEY)
        serverchan_key_env: Optional[str] = os.environ.get(self.ENV_SERVERCHAN_KEY)
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        exchange_plan_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLAN)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)

        if not push_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_PUSH_KEY}' 未设置。")
            self.push_key = ""
        else:
            self.push_key = push_key_env

        if not serverchan_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_SERVERCHAN_KEY}' 未设置。")
            self.serverchan_key = ""
        else:
            self.serverchan_key = serverchan_key_env

        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        if not exchange_plan_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 未设置，将使用默认设置（不自动兑换）。")
            self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN
        else:
            if exchange_plan_env in self.EXCHANGE_PLANS:
                self.exchange_plan = exchange_plan_env
                logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换计划: {self.exchange_plan}")
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将跳过自动兑换。")
                self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie 用于签到。")

        # fork 定制：域名可配置（GLADOS_DOMAINS）
        raw_domains: Optional[str] = os.environ.get(self.ENV_DOMAINS, "")
        if raw_domains and raw_domains.strip():
            parsed = [d.strip() for d in raw_domains.split(",") if d.strip()]
            self.DOMAINS = parsed or list(self.DEFAULT_DOMAINS)
        else:
            self.DOMAINS = list(self.DEFAULT_DOMAINS)
        logger.info(f"{LogEmoji.INFO} 签到域名: {', '.join(self.DOMAINS)}")

        # 打码服务（Turnstile）
        self.solver_provider = (os.environ.get(self.ENV_SOLVER) or "").strip().lower()
        self.solver_key = (os.environ.get(self.ENV_SOLVER_KEY) or "").strip()
        if self.solver_key and not self.solver_provider:
            self.solver_provider = "capsolver"
        if self.solver_key:
            logger.info(f"{LogEmoji.SUCCESS} Turnstile 打码服务已启用：{self.solver_provider}")
        else:
            logger.info(f"{LogEmoji.INFO} 未配置 {self.ENV_SOLVER_KEY}，遇到 Turnstile 将跳过（交本机浏览器签到）。")
        self.solver = TurnstileSolver(self.solver_provider, self.solver_key, verbose=self.verbose)

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}，{self.ENV_SERVERCHAN_KEY} {'已设置' if serverchan_key_env else '未设置'}。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_EXCHANGE_PLAN}: {self.exchange_plan}。")

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")

        # 2026-11-13：线路加成闸门配置
        mode_env = (os.environ.get(self.ENV_CABLE_MODE) or "").strip().lower()
        if mode_env in ("off", "false", "0", "no", "n"):
            self.cable_mode = False
        elif mode_env in ("on", "true", "1", "yes", "y"):
            self.cable_mode = True
        elif mode_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_CABLE_MODE}' 的值 '{mode_env}' 无效，按默认开启处理。")

        min_env = (os.environ.get(self.ENV_CABLE_MIN) or "").strip()
        if min_env:
            try:
                parsed_min = int(min_env)
                if 0 <= parsed_min <= 9:
                    self.cable_min = parsed_min
                else:
                    logger.warning(f"{LogEmoji.WARNING} '{self.ENV_CABLE_MIN}' 取值应在 0~9，已忽略。")
            except ValueError:
                logger.warning(f"{LogEmoji.WARNING} '{self.ENV_CABLE_MIN}' 不是整数，已忽略。")

        hour_env = (os.environ.get(self.ENV_CABLE_FALLBACK_HOUR) or "").strip()
        if hour_env:
            try:
                parsed_hour = int(hour_env)
                if 0 <= parsed_hour <= 23:
                    self.cable_fallback_hour = parsed_hour
                else:
                    logger.warning(f"{LogEmoji.WARNING} '{self.ENV_CABLE_FALLBACK_HOUR}' 取值应在 0~23，已忽略。")
            except ValueError:
                logger.warning(f"{LogEmoji.WARNING} '{self.ENV_CABLE_FALLBACK_HOUR}' 不是整数，已忽略。")

        if self.cable_mode:
            logger.info(
                f"{LogEmoji.INFO} 线路加成闸门：开启（Cable >= {self.cable_min} 才签，"
                f"北京 {self.cable_fallback_hour}:00 之后保底签）"
            )
        else:
            logger.info(f"{LogEmoji.INFO} 线路加成闸门：关闭（到点就签）")


# 各平台合法 User-Agent（版本号无关，仅平台 token 关键）
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

"""签到 challenge 的 proof 后缀与提交前最小等待（与 GLaDOS 网页端当前实现一致）"""
CHALLENGE_SUFFIX = "glados-observe-v1"
CHALLENGE_DELAY_SECONDS = 2.5

"""自定义 User-Agent（2026-11 合并上游 b9d4a91b）

GLaDOS 会校验 UA 与账号登录设备，不一致时签到被拒（code 4 / device-mismatch）。
填写「提取 Cookie 时那个浏览器」的 UA 最稳；留空则用默认 UA + loginDevice 自适应重试。
"""
ENV_USER_AGENT = "GLADOS_USER_AGENT"


class TurnstileSolver:
    """Cloudflare Turnstile 打码封装（2026-11-14）

    GLaDOS 给签到 /api/user/checkin 加了 Turnstile：challenge 响应里带 sitekey，
    提交必须带真实 token，否则回 {"code":5,"reason":"turnstile"}。
    实测结论：
      - 纯 HTTP：无解；
      - 无头浏览器（含 xvfb 有头 Chromium，在 GitHub Actions 上）：Turnstile 判
        「需要人工交互」，连验证 iframe 都不建，拿不到 token；
      - 本机真浏览器：15s 出 token（所以本机脚本能签）。
    因此想让签到继续跑在云端，只能把「过验证」这一小步外包给打码平台：
    云端拿到 token 后仍由 Actions 自己 POST，积分/流水/通知全保持原样。
    """

    CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
    CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"
    TWOCAPTCHA_IN = "https://2captcha.com/in.php"
    TWOCAPTCHA_RES = "https://2captcha.com/res.php"

    def __init__(self, provider: str = "", api_key: str = "", verbose: bool = False):
        self.provider = (provider or "").strip().lower()
        self.api_key = (api_key or "").strip()
        self.verbose = verbose
        if not self.provider and self.api_key:
            self.provider = "capsolver"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.provider in ("capsolver", "2captcha")

    def solve(
        self, sitekey: str, page_url: str, action: str = "checkin", cdata: str = "", timeout: int = 120
    ) -> Optional[str]:
        """返回 Turnstile token；失败返回 None（不抛异常，主流程走跳过分支）"""
        if not self.enabled:
            return None
        try:
            if self.provider == "capsolver":
                return self._capsolver(sitekey, page_url, action, cdata, timeout)
            return self._two_captcha(sitekey, page_url, action, cdata, timeout)
        except Exception as e:
            logger.warning(f"{LogEmoji.WARNING} 打码服务调用异常：{e}")
            return None

    def _capsolver(self, sitekey: str, page_url: str, action: str, cdata: str, timeout: int) -> Optional[str]:
        task: Dict[str, object] = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
        meta: Dict[str, str] = {}
        if action:
            meta["action"] = action
        if cdata:
            meta["cdata"] = cdata
        if meta:
            task["metadata"] = meta

        logger.info(f"{LogEmoji.INFO} 提交 Turnstile 打码任务（capsolver）…")
        resp = requests.post(
            self.CAPSOLVER_CREATE, json={"clientKey": self.api_key, "task": task}, timeout=30
        ).json()
        if resp.get("errorId"):
            logger.warning(
                f"{LogEmoji.WARNING} 打码任务创建失败：{resp.get('errorCode')} {resp.get('errorDescription')}"
            )
            return None
        task_id = resp.get("taskId")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            r = requests.post(
                self.CAPSOLVER_RESULT, json={"clientKey": self.api_key, "taskId": task_id}, timeout=30
            ).json()
            if r.get("errorId"):
                logger.warning(
                    f"{LogEmoji.WARNING} 打码结果查询失败：{r.get('errorCode')} {r.get('errorDescription')}"
                )
                return None
            if r.get("status") == "ready":
                token = (r.get("solution") or {}).get("token")
                if token:
                    logger.info(f"{LogEmoji.SUCCESS} 打码完成，已拿到 Turnstile token")
                return token
        logger.warning(f"{LogEmoji.WARNING} 打码超时（{timeout}s）")
        return None

    def _two_captcha(self, sitekey: str, page_url: str, action: str, cdata: str, timeout: int) -> Optional[str]:
        data = {
            "method": "turnstile",
            "key": self.api_key,
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        if action:
            data["action"] = action
        if cdata:
            data["data"] = cdata
        logger.info(f"{LogEmoji.INFO} 提交 Turnstile 打码任务（2captcha）…")
        resp = requests.post(self.TWOCAPTCHA_IN, data=data, timeout=30).json()
        if resp.get("status") != 1:
            logger.warning(f"{LogEmoji.WARNING} 打码任务创建失败：{resp.get('request')}")
            return None
        task_id = resp.get("request")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            r = requests.get(
                self.TWOCAPTCHA_RES,
                params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                timeout=30,
            ).json()
            if r.get("status") == 1:
                logger.info(f"{LogEmoji.SUCCESS} 打码完成，已拿到 Turnstile token")
                return r.get("request")
            if r.get("request") != "CAPCHA_NOT_READY":
                logger.warning(f"{LogEmoji.WARNING} 打码失败：{r.get('request')}")
                return None
        logger.warning(f"{LogEmoji.WARNING} 打码超时（{timeout}s）")
        return None


class API:
    """API 调用"""

    CHECKIN_URL = APIEndpoint.CHECKIN.value
    STATUS_URL = APIEndpoint.STATUS.value
    POINTS_URL = APIEndpoint.POINTS.value
    EXCHANGE_URL = APIEndpoint.EXCHANGE.value

    def __init__(self, domain: str, cookie_index: int = 0, verbose: bool = False, user_agent: Optional[str] = None):
        self.domain: str = domain
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        # 合并上游 b9d4a91b：自定义 UA 优先，其次环境变量，都没有则留空（走平台默认 + 自适应重试）
        env_ua = (os.environ.get(ENV_USER_AGENT) or "").strip()
        self.user_agent: str = (user_agent or env_ua or "").strip()
        self.headers: Dict[str, str] = self._get_headers()
        self._device_ua_cache: Dict[str, str] = {}
        # 2026-11-14：Turnstile 打码（由 main 注入；未配置则为 None → 跳过签到）
        self.solver: Optional["TurnstileSolver"] = None
        self._need_turnstile: bool = False
        self._ts_sitekey: str = ""
        self._ts_nonce: str = ""
        self._ts_page_url: str = ""
        # fork 定制：账号画像（get_status 顺手填充），供通知展示 email / 套餐
        self.profile: Dict[str, object] = {"email": "", "vip": None, "expire_at": ""}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        # 网络抖动时自动重试（指数退避），避免误报失败
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def __del__(self):
        """关闭 session"""
        self.close()

    def close(self) -> None:
        """关闭 session"""
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        base_ua = getattr(self, "user_agent", "") or PLATFORM_UA["Windows"]
        return {
            "origin": f"https://{self.domain}",
            "user-agent": base_ua,
        }

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        prefix = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}]"
        log_message = f"{prefix} {emoji} {message}"

        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _get_full_url(self, path: str) -> str:
        """获取完整 URL"""
        return f"https://{self.domain}{path}"

    def _make_request(self, url: str, method: str, data: Optional[Dict] = None, cookies: str = "", user_agent: Optional[str] = None, json_body: bool = False) -> Optional[requests.Response]:
        """发送 HTTP 请求

        json_body=True 时以 JSON 提交（新版签到带 challenge 时服务端要求 JSON body，
        老版表单提交会被当成旧协议 → 只给 1 积分）。
        """
        session_headers = self.headers.copy()
        if user_agent:
            session_headers["user-agent"] = user_agent
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                if json_body:
                    response = self.session.post(url, headers=session_headers, json=data, timeout=(60, 120))
                else:
                    response = self.session.post(url, headers=session_headers, data=data, timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            if not response.ok:
                self._log("warning", LogEmoji.WARNING, f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text}", force=True)
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        """获取签到数据"""
        return {"token": self.domain}

    def _get_checkin_challenge(self, cookies: str, user_agent: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """获取签到 challenge 并计算 proof（合并上游 7bdf8225）

        GLaDOS 新版签到要求带 challenge 提交；拿不到就回退旧协议，
        但旧协议服务端判定为自动签到，只给 1 积分、连续天数不累加。
        失败返回 None，由调用方回退。
        """
        url = self._get_full_url(APIEndpoint.CHECKIN_CHALLENGE.value)
        response = self._make_request(url, "GET", cookies=cookies, user_agent=user_agent)
        if not response:
            return None

        try:
            data = response.json()
        except ValueError:
            self._log("warning", LogEmoji.WARNING, "签到 challenge 响应解析失败，按旧协议提交", force=True)
            return None

        if not isinstance(data, dict) or data.get("code") != 0:
            self._log("warning", LogEmoji.WARNING, f"签到 challenge 响应异常: {data}", force=True)
            return None

        nonce = data.get("nonce")
        if not nonce:
            self._log("warning", LogEmoji.WARNING, "签到 challenge 缺少 nonce，按旧协议提交", force=True)
            return None

        # 2026-11-14：challenge 里带 turnstile sitekey = 服务端要求真实 Cloudflare
        # Turnstile token。纯 HTTP 客户端拿不到（无头浏览器也过不去），POST 必被拒：
        #   {"code":5,"reason":"turnstile","message":"Cloudflare verification failed..."}
        # 所以这里直接不打签到接口，标记「需浏览器」交给本机脚本补签，避免每天刷失败。
        if data.get("turnstile"):
            self._need_turnstile = True
            self._ts_sitekey = str(data.get("turnstile") or "")
            self._ts_nonce = str(nonce)
            self._ts_page_url = f"https://{self.domain}/console/checkin"
            self._log(
                "warning",
                LogEmoji.PENDING,
                f"服务端要求 Cloudflare Turnstile 验证（sitekey={self._ts_sitekey}）",
                force=True,
            )
            return None

        # 与网页端一致：拿到 challenge 后等待一小段再提交，太快会被判定为脚本
        time.sleep(CHALLENGE_DELAY_SECONDS)
        proof = hashlib.sha256(f"{nonce}.{CHALLENGE_SUFFIX}".encode()).hexdigest()
        self._log("info", LogEmoji.SUCCESS, "已获取签到 challenge", force=True)
        return str(nonce), proof

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到，含设备平台自适应重试"""
        url = self._get_full_url(self.CHECKIN_URL)
        checkin_data = self._get_checkin_data()

        # 起始 UA 优先复用已学得的设备 UA，否则用会话默认 UA
        start_ua = self._device_ua_cache.get("_last") or self.headers.get("user-agent")

        raw = self._checkin_attempt(url, checkin_data, cookies, start_ua)

        # 设备不匹配(code 4)时按服务端 loginDevice 切换 UA 重试一次
        if raw is not None and raw.get("code") == 4 and raw.get("reason") == "device-mismatch":
            login_device = raw.get("loginDevice")
            recover_ua = self._device_ua_cache.get(login_device) or PLATFORM_UA.get(login_device)
            if recover_ua and recover_ua != start_ua:
                self._log("warning", LogEmoji.WARNING, f"设备平台不匹配 (loginDevice={login_device})，切换 UA 重试", force=True)
                raw = self._checkin_attempt(url, checkin_data, cookies, recover_ua)

        return self._parse_checkin(raw)

    def _checkin_attempt(self, url: str, data: Dict, cookies: str, user_agent: str) -> Optional[Dict]:
        """发起一次签到请求并返回解析后的响应体（失败返回 None）

        合并上游 7bdf8225：先取 challenge，拿到就带 nonce/proof 且用 JSON 提交；
        拿不到回退旧表单提交（服务端仍放行，但只给 1 积分）。
        """
        payload: Dict[str, str] = dict(data or {})
        self._need_turnstile = False
        challenge = self._get_checkin_challenge(cookies, user_agent)
        if getattr(self, "_need_turnstile", False):
            # 服务端要 Turnstile token。配了打码服务就让它算，算不到再跳过
            # （跳过交给 _parse_checkin 归类成「待处理」，既不算失败也不推送）。
            token = None
            if self.solver is not None and self.solver.enabled:
                token = self.solver.solve(
                    sitekey=self._ts_sitekey,
                    page_url=self._ts_page_url,
                    action="checkin",
                    cdata=self._ts_nonce,
                )
            if not token:
                return {
                    "code": 5,
                    "reason": "turnstile",
                    "message": (
                        "服务端要求 Cloudflare Turnstile，且未配置/未取到打码 token"
                        if self.solver is None or not self.solver.enabled
                        else "服务端要求 Cloudflare Turnstile，打码服务未返回 token"
                    ),
                }
            nonce = self._ts_nonce
            payload["nonce"] = nonce
            payload["proof"] = hashlib.sha256(f"{nonce}.{CHALLENGE_SUFFIX}".encode()).hexdigest()
            payload["tsState"] = "ok"
            payload["turnstile"] = token
            challenge = (nonce, payload["proof"])
        if challenge:
            nonce, proof = challenge
            payload["nonce"] = nonce
            payload["proof"] = proof
        else:
            self._log(
                "warning",
                LogEmoji.WARNING,
                "未取得签到 challenge，按旧协议提交（只给 1 积分、不计连续签到）",
                force=True,
            )

        response = self._make_request(
            url,
            "POST",
            payload,
            cookies,
            user_agent=user_agent,
            json_body=bool(challenge),
        )
        if not response:
            return None
        try:
            raw = response.json()
        except ValueError:
            self._log("error", LogEmoji.ERROR, "签到响应解析失败", force=True)
            return None

        # 缓存学得的设备 UA，供本次运行后续请求复用
        if raw.get("code") == 4 and raw.get("reason") == "device-mismatch":
            login_device = raw.get("loginDevice")
            if login_device in PLATFORM_UA:
                self._device_ua_cache[login_device] = PLATFORM_UA[login_device]
        elif raw.get("code") in (CheckinStatus.SUCCESS.value, CheckinStatus.REPEAT.value):
            self._device_ua_cache["_last"] = user_agent

        return raw

    def _parse_checkin(self, raw: Optional[Dict]) -> Dict[str, Union[str, CheckinStatus]]:
        """解析签到响应为结果字典"""
        result: Dict[str, Union[str, CheckinStatus]] = {
            "status": "签到失败",
            "points": "0",
            "message": "",
            "code": CheckinStatus.FAILURE,
            "streak": 0,
        }

        if not raw:
            result["message"] = "网络请求失败"
            return result

        code = raw.get("code", -2)
        message = raw.get("message", "无消息字段")
        points = str(raw.get("points", 0))
        try:
            streak = int(float(raw.get("streak") or 0))
        except (TypeError, ValueError):
            streak = 0
        result["streak"] = streak

        # 2026-11-13：服务端把积分构成放在响应的 breakdown 里（网页端也是读这个字段），
        # 打进日志用于校准「指数档位 → 实发分」，别再把结算当黑盒。
        result["breakdown"] = _format_breakdown(raw.get("breakdown"))

        if code == CheckinStatus.SUCCESS.value:
            self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
            result["code"] = CheckinStatus.SUCCESS
            result["status"] = "签到成功"
            result["points"] = points
            result["message"] = message
            if result["breakdown"]:
                self._log("info", LogEmoji.POINTS, f"积分构成：{result['breakdown']}", force=True)
        elif code == CheckinStatus.REPEAT.value:
            self._log("info", LogEmoji.REPEAT, f"{{ code : {code}, message : {message} }}", force=True)
            result["code"] = CheckinStatus.REPEAT
            result["status"] = "重复签到"
            result["points"] = "0"
            result["message"] = message
        else:
            # 失败分支：按 code 细分诊断，便于快速定位根因
            if code == 5 or raw.get("reason") == "turnstile":
                # 2026-11-14：GLaDOS 给签到加了 Cloudflare Turnstile，token 只能由真浏览器
                # 在页面里拿，HTTP/无头都不行。这不算失败（服务端提示 Nothing was recorded，
                # 连续天数不受影响），分类成 SKIPPED 让本机浏览器脚本继续补签、也不误报。
                self._log(
                    "warning",
                    LogEmoji.PENDING,
                    f"服务端要求 Cloudflare Turnstile 验证(code={code}, reason={raw.get('reason')})，"
                    "本次跳过不推送 → 交本机浏览器签到",
                    force=True,
                )
                result["code"] = CheckinStatus.SKIPPED
                result["status"] = "待浏览器验证"
                result["points"] = "0"
                result["message"] = message
                result["need_browser"] = True
                return result
            if code == 4:
                reason = raw.get("reason")
                if reason == "device-mismatch":
                    login_device = raw.get("loginDevice") or "未知"
                    if login_device in PLATFORM_UA:
                        self._log("error", LogEmoji.ERROR, f"设备平台不匹配(loginDevice={login_device})，重试后仍失败，请确认登录设备平台", force=True)
                    else:
                        self._log("error", LogEmoji.ERROR, f"设备平台不匹配(loginDevice={login_device} 不在支持列表)，无法自动切换 UA；请提交相关截图到本项目 issue 以便作者更新设备列表", force=True)
                else:
                    self._log("error", LogEmoji.ERROR, f"服务端拒绝签到(code=4, reason={reason})，请检查账户风控状态", force=True)
            elif code == -2:
                self._log("error", LogEmoji.ERROR, f"无权限(code=-2)：Cookie 可能已失效，请重新登录 {self.domain} 刷新 Cookie 并更新仓库 Secret GLADOS_COOKIES 后重试", force=True)
            else:
                self._log("error", LogEmoji.ERROR, f"未预期响应(code={code}, message={message})，请检查 GLaDOS 服务状态或 Cookie", force=True)
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["points"] = "0"
            result["message"] = message

        return result

    @log_method
    def get_status(self, cookies: str) -> Tuple[str, int]:
        """获取状态"""

        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            info = data.get("data") or {}
            left_days = info.get("leftDays", None)
            # fork 定制：顺带记录账号邮箱与 vip 等级，供 TG 通知展示（不额外发请求）
            self.profile["email"] = info.get("email") or ""
            self.profile["vip"] = info.get("vip", None)
            # fork 定制：顺带算出套餐到期日（leftDays + 服务端时间反推）
            self.profile["expire_at"] = _expire_date(left_days, info.get("system_time"))

            if left_days is not None:
                left_days_int = int(float(left_days))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, leftDays : {left_days_int} 天}}")
                return f"{left_days_int} 天", code
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, leftDays : {left_days} 天}}")
                return "None 天", code
        else:
            self._log("warning", LogEmoji.WARNING, "获取状态失败")
            return "None 天", -2

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int]:
        """获取积分"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            points = data.get("points", None)

            if points is not None:
                points_int = int(float(points))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points_int} 积分}}")
                points_str = f"{points_int} 积分"
                points_num = points_int
                return points_str, points_num
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, points : {points} 积分}}")
                return "None 积分", 0
        else:
            self._log("warning", LogEmoji.WARNING, "获取积分失败")
            return "None 积分", 0

    @log_method
    def get_cable_points(self, cookies: str) -> Optional[Dict[str, Union[int, str]]]:
        """读取当前线路加成档位（2026-11-13 新增）

        数据源 /api/user/network-insight（官方「China Network Insight」页面同源），
        current.cablePoints 就是网页端 Cable 明细用的那个档位分值：
            指数 < 90 → 9 · 90-94 → 7 · 95-99 → 5 · 100-109 → 3 · 110-130 → 2 · > 130 → 1
        线路越差给得越多，所以挑指数差的时刻签到才能拿到 8~9 分。

        失败返回 None（由调用方决定是否放行，绝不因为探测失败就漏签）。
        """
        url = self._get_full_url(APIEndpoint.NETWORK_INSIGHT.value)
        response = self._make_request(url, "GET", cookies=cookies)
        if not response:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("code") not in (0, "0"):
            return None

        cur = ((data.get("data") or {}).get("current") or {})
        score = cur.get("score")
        cable = cur.get("cablePoints")
        if cable is None and score is not None:
            # 兜底：接口没给 cablePoints 时按官方分档自己算
            try:
                s = float(score)
            except (TypeError, ValueError):
                s = 999.0
            if s < 90:
                cable = 9
            elif s < 95:
                cable = 7
            elif s < 100:
                cable = 5
            elif s < 110:
                cable = 3
            elif s <= 130:
                cable = 2
            else:
                cable = 1
        try:
            cable_int = int(cable or 0)
        except (TypeError, ValueError):
            cable_int = 0
        try:
            score_int = int(score) if score is not None else -1
        except (TypeError, ValueError):
            score_int = -1

        info: Dict[str, Union[int, str]] = {
            "score": score_int,
            "points": cable_int,
            "level": str(cur.get("level") or ""),
            "comment": str(cur.get("comment") or ""),
        }
        self._log(
            "info",
            LogEmoji.POINTS,
            f"线路加成档位：指数 {score_int}（{info['level']}）→ Cable +{cable_int}",
            force=True,
        )
        return info

    @log_method
    def has_checked_in_today(self, cookies: str) -> Tuple[bool, int, str, int]:
        """查积分明细里今天（北京日期）是否已有 system:checkin 记录。

        fork 定制（2026-11-08）：一天有多个定时触发点，已签到就**不再调用签到接口**，
        只发只读 GET，避免反复 POST /user/checkin 触发平台风控。

        返回 (今日已签到, 总积分, 总积分文本, 连续签到天数)
        """
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        # 查询失败时一律按「未签到」处理：宁可多签一次（服务端会返回重复，无副作用），
        # 也不能因为只读接口抖动就漏签。
        if not response:
            return False, 0, "None 积分", 0

        data = response.json()
        if data.get("code", -2) != 0:
            return False, 0, "None 积分", 0

        try:
            total = int(float(data.get("points") or 0))
        except (TypeError, ValueError):
            total = 0

        # 2026-11-10 修复：原来严格匹配 business == "system:checkin"，
        # 服务端字段写法一变就会「每天都判定未签到」→ 一天 6 个触发点全部真 POST，
        # 既刷积分记录也容易吃风控。改为包含匹配 + detail 取日期前缀。
        dates = set()
        for it in data.get("history") or []:
            biz = str(it.get("business") or "").lower()
            if "checkin" not in biz:
                continue
            day = str(it.get("detail") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                dates.add(day)
        today = _bj_date()
        already = today in dates

        # 从今天往前数连续签到天数（明细里的 detail 是北京日期）
        import datetime

        streak = 0
        cursor = datetime.date.fromisoformat(today)
        while cursor.isoformat() in dates:
            streak += 1
            cursor -= datetime.timedelta(days=1)

        points_str = f"{total} 积分"
        if already:
            self._log("info", LogEmoji.SUCCESS, f"{{ 今日(北京)已有签到记录，连续 {streak} 天 }}")
        else:
            self._log("info", LogEmoji.INFO, "{ 今日(北京)尚无签到记录 }")
        return already, total, points_str, streak

    @log_method
    def exchange(self, cookies: str, plan: str, required_points: int) -> str:
        """执行兑换"""
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "未知错误")

            if code == 0:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, message : {message} }}")
                return f"兑换成功: {plan}"
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                return f"兑换失败: {message}"
        else:
            self._log("warning", LogEmoji.WARNING, "兑换失败", force=True)
            return "兑换失败"


# fork 定制：vip 等级 → 套餐名。当前账号 vip=11 在控制台显示为「Edu」，未知等级回退成 VIPn。
VIP_PLAN_NAMES = {11: "Edu"}


def plan_name(vip) -> str:
    """把 /api/user/status 的 vip 等级转成套餐名"""
    if vip is None:
        return "未知"
    try:
        v = int(vip)
    except (TypeError, ValueError):
        return "未知"
    return VIP_PLAN_NAMES.get(v, f"VIP{v}")


@dataclass()
class CheckinResult:
    """签到结果"""

    cookie_index: int
    domain: str
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败
    email: str = ""
    plan: str = ""
    streak: int = 0
    message: str = ""
    expire: str = ""
    # 2026-11-13：线路加成闸门相关
    cable_score: int = -1
    cable_points: int = -1
    breakdown: str = ""

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        result_dict = asdict(self)
        return result_dict


def render_payload(payload: dict, line_sep: str = "\n") -> str:
    """将结构化签到结果渲染为文本。line_sep 控制行分隔：
    PushDeer 用 \\n（原生换行），Server酱 用 '  \\n'（Markdown 硬换行，避免段间空行）。"""
    lines = []
    for r in payload.get("rows", []):
        line = (
            f"#{r['index']:<2}  {r['emoji']} {r['status']:<6}  {r['domain']:<13}  "
            f"获得 {r['points']} 积分 · 剩余 {r['days']} · 总 {r['points_total']}"
        )
        if r.get("exchange"):
            line += f" · {r['exchange']}"
        lines.append(line)
    note = payload.get("note")
    if note:
        lines.append(note)
    return line_sep.join(lines)


def _bj_now() -> str:
    """北京时间（GitHub runner 为 UTC）"""
    import datetime

    bj = datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return bj.strftime("%Y-%m-%d %H:%M")


def _bj_date() -> str:
    """北京时间日期 YYYY-MM-DD（GLaDOS 积分明细的 detail 字段用的就是北京日期）"""
    import datetime

    bj = datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return bj.strftime("%Y-%m-%d")


def _bj_hour() -> int:
    """北京时间当前小时（0-23），用于判断是否进入保底时段"""
    import datetime

    bj = datetime.datetime.now(datetime.timezone.utc).astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return bj.hour


def _num(text) -> str:
    """从 '303 积分' / '388 天' 取数字；含 None 的脏值返回 '未知'"""
    s = str(text or "")
    if not s or "None" in s:
        return "未知"
    m = re.findall(r"-?\d+", s)
    return m[0] if m else s


def _fmt_pt(value) -> str:
    """整数就省掉小数位（9 而不是 9.0），小数保留一位（1.5）"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "0"
    return str(int(num)) if num == int(num) else f"{num:.1f}"


def _format_breakdown(raw_breakdown) -> str:
    """把服务端 breakdown 渲染成一行：Base +1 · 套餐 +0 · 连续 +1 · Cable +9 ...

    GLaDOS /api/user/checkin 响应里的 breakdown 字段（网页端「Got/Base/Plan/Streak/
    Cable/Lucky/Site/Exit IP/Free cap」那张卡就是读它）。字段缺失就跳过，不做假设。
    """
    if not isinstance(raw_breakdown, dict):
        return ""
    label_map = [
        ("base", "Base"),
        ("level", "套餐"),
        ("streak", "连续"),
        ("cable", "Cable"),
        ("lucky", "幸运"),
        ("site", "站点"),
        ("exitIp", "出口IP"),
    ]
    parts = []
    for key, label in label_map:
        if raw_breakdown.get(key) is None:
            continue
        parts.append(f"{label} +{_fmt_pt(raw_breakdown.get(key))}")

    extras = []
    if raw_breakdown.get("cableScore") is not None:
        extras.append(f"指数 {raw_breakdown.get('cableScore')}")
    if raw_breakdown.get("streakDays") is not None:
        extras.append(f"连续 {raw_breakdown.get('streakDays')} 天")
    if raw_breakdown.get("freeCap") is not None:
        extras.append(f"免费上限 {_fmt_pt(raw_breakdown.get('freeCap'))}")

    text = " · ".join(parts)
    if extras:
        text = f"{text}（{'，'.join(extras)}）" if text else "，".join(extras)
    return text


def _expire_date(left_days, system_time=None) -> str:
    """fork 定制：由 leftDays（浮点天数）推算套餐到期日（北京时间 YYYY-MM-DD）

    system_time 为 /api/user/status 返回的服务端毫秒时间戳，缺失时用当前时间。
    接口本身不直接给到期时间戳，只能这样反推。
    """
    import datetime

    try:
        days = float(left_days)
    except (TypeError, ValueError):
        return ""
    try:
        base = (
            datetime.datetime.fromtimestamp(int(system_time) / 1000, datetime.timezone.utc)
            if system_time
            else datetime.datetime.now(datetime.timezone.utc)
        )
    except (TypeError, ValueError, OSError):
        base = datetime.datetime.now(datetime.timezone.utc)
    bj = (base + datetime.timedelta(days=days)).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))
    )
    return bj.strftime("%Y-%m-%d")


def render_tg(payload: dict) -> str:
    """fork 定制（2026-10-30 用户指定格式）：Telegram「签到通知」频道排版

        ✅ GLaDOS 签到成功
        账号：xx@gmail.com
        结果：<服务端原话>
        今日：+10 分（日历已打卡）
        积分：303
        剩余天数：388 天
        连续签到：2 天
        套餐：Edu
        套餐到期：2027-11-21
        签到时间：2026-10-30 00:20
    """
    now = _bj_now()
    blocks = []
    for r in payload.get("rows", []):
        status = r.get("status", "")
        lines = [f"{r.get('emoji', '')} GLaDOS {status}".strip()]
        if r.get("email"):
            lines.append(f"账号：{r['email']}")
        if r.get("message"):
            lines.append(f"结果：{r['message']}")
        if status == "签到成功":
            lines.append(f"今日：+{_num(r.get('points'))} 分（日历已打卡）")
            if r.get("breakdown"):
                lines.append(f"明细：{r['breakdown']}")
        elif status == "重复签到":
            lines.append("今日：+0 分（今日已签到）")
        elif status == "今日已签到，跳过":
            lines.append("今日：已签到（本次未重复请求）")
        elif status == "等待线路加成，跳过":
            lines.append(f"今日：本轮未签（{r.get('message') or '等线路加成'}）")
        if r.get("points_total"):
            total = _num(r.get("points_total"))
            lines.append("积分：未知" if total == "未知" else f"积分：{total}")
        if r.get("days"):
            days = _num(r.get("days"))
            lines.append("剩余天数：未知" if days == "未知" else f"剩余天数：{days} 天")
        if status in ("签到成功", "重复签到", "今日已签到，跳过"):
            lines.append(f"连续签到：{r.get('streak', 0)} 天")
        if r.get("plan"):
            lines.append(f"套餐：{r['plan']}")
        if r.get("expire"):
            lines.append(f"套餐到期：{r['expire']}")
        lines.append(f"签到时间：{now}")
        blocks.append("\n".join(lines))
    note = payload.get("note")
    if note:
        blocks.append(str(note))
    return "\n\n".join(blocks)


class PushBackend:
    """推送后端抽象基类（策略模式）"""

    name: str = "base"

    def send(self, title: str, payload: dict) -> bool:
        """发送推送，返回是否成功（由子类实现）"""
        raise NotImplementedError

    def _render(self, payload: dict) -> str:
        """将结构化结果渲染为本后端适用的文本（子类可重写行分隔方式）"""
        return render_payload(payload, "\n")


class PushDeerBackend(PushBackend):
    """PushDeer 后端（官方 pypushdeer 库）"""

    name = "PushDeer"

    def __init__(self, push_key: str):
        self.push_key = push_key

    def send(self, title: str, payload: dict) -> bool:
        try:
            PushDeer(pushkey=self.push_key).send_text(title, desp=self._render(payload))
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class ServerChanBackend(PushBackend):
    """Server酱 后端（官方 serverchan_sdk）"""

    name = "Server酱"

    def __init__(self, send_key: str):
        self.send_key = send_key

    def _render(self, payload: dict) -> str:
        # Server酱 按 Markdown 渲染，单 \n 不换行；用行尾两空格+\n 做硬换行，避免段间空行
        return render_payload(payload, "  \n")

    def send(self, title: str, payload: dict) -> bool:
        # 延迟导入：未安装 SDK 时不阻断其他后端（如 PushDeer）
        try:
            from serverchan_sdk import sc_send
        except ImportError:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 未安装 serverchan_sdk，请先执行 pip install serverchan-sdk。")
            return False
        try:
            resp = sc_send(sendkey=self.send_key, title=title, desp=self._render(payload))
            if isinstance(resp, dict) and resp.get("code") == 0:
                return True
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 接口返回 {resp}")
            return False
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class TelegramBackend(PushBackend):
    """Telegram 后端（fork 定制：推送到「签到通知」频道）

    不依赖 Config，只要 tg_notify 模块可用就注册——保证签到结果一定会推到 TG。
    """

    name = "Telegram"

    def send(self, title: str, payload: dict) -> bool:
        if push_checkin_to_telegram is None:
            logger.warning(f"{LogEmoji.WARNING} [{self.name}] tg_notify 模块不可用，跳过 Telegram 推送。")
            return False
        # fork 定制（2026-11-06）：同一天只推第一条，避免多个兜底 cron 点重复刷屏
        try:
            if pushed_today is not None and pushed_today():
                logger.info(f"{LogEmoji.INFO} [{self.name}] 今天已推送过签到通知，本次跳过（多点兜底去重）。")
                # 同时打上本次运行的已推送标记，避免 workflow 的兜底通知再补一条
                if mark_pushed is not None:
                    mark_pushed()
                return False
        except Exception:
            pass
        try:
            # fork 定制：TG 用固定字段排版（render_tg），不再复用 PushDeer 的单行格式
            if push_plain_text is not None:
                text = render_tg(payload)
                if push_plain_text(text):
                    return True
                logger.warning(f"{LogEmoji.WARNING} [{self.name}] 推送未成功（未配置凭据或发送失败）。")
                return False
            if push_checkin_to_telegram(title, self._render(payload)):
                return True
            logger.warning(f"{LogEmoji.WARNING} [{self.name}] 推送未成功（未配置凭据或发送失败）。")
            return False
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class PushService:
    """推送服务：聚合多个后端，向全部已配置后端发送（都发）"""

    def __init__(self, config: Optional[Config] = None):
        self.backends: List[PushBackend] = []
        if config:
            if getattr(config, "push_key", ""):
                self.backends.append(PushDeerBackend(config.push_key))
            if getattr(config, "serverchan_key", ""):
                self.backends.append(ServerChanBackend(config.serverchan_key))
        # fork 定制：Telegram 无条件注册（模块导入失败时自身会返回 False）
        self.backends.append(TelegramBackend())

    def send(self, title: str, payload: dict) -> bool:
        """向所有已配置后端发送推送，只要有一个成功即视为成功"""
        if not self.backends:
            logger.info(f"{LogEmoji.WARNING} 未配置推送密钥，跳过。")
            return False

        backend_names = "、".join(b.name for b in self.backends)
        logger.info(f"{LogEmoji.INFO} 已配置 {len(self.backends)} 个后端（{backend_names}），开始发送")
        results = [backend.send(title, payload) for backend in self.backends]
        if any(results):
            logger.info(f"{LogEmoji.SUCCESS} 发送完成：{sum(results)}/{len(results)} 个后端成功")
            return True
        logger.warning(f"{LogEmoji.WARNING} 本次未成功推送（今日已推送过去重，或后端未配置）")
        return False


class Checker:
    """签到"""

    def __init__(self, config: Config):
        self.config = config
        self.results = []

    def _log(self, cookie_idx: int, domain: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}")

    def checkin_all(self):
        """执行所有签到任务"""
        cookie_count = len(self.config.cookies_list)
        domain_count = len(self.config.DOMAINS)
        total_tasks = cookie_count * domain_count
        task_idx = 0

        logger.info(f"{LogEmoji.INFO} 共 {cookie_count} 个 Cookie, {domain_count} 个域名, 共 {total_tasks} 个任务")

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            logger.info(f"{LogEmoji.START} ========== 开始处理 Cookie {cookie_idx} ==========")

            for domain in self.config.DOMAINS:
                task_idx += 1
                logger.info(f"{LogEmoji.INFO} ----- 任务 {task_idx}/{total_tasks}: {LogEmoji.COOKIE}[{cookie_idx}] on {LogEmoji.DOMAIN}[{domain}] -----")

                result = self._checkin_on_domain(cookie, cookie_idx, domain)
                self.results.append(result)

                result_message = f"结果: {result.status}"
                if result.code == CheckinStatus.SUCCESS:
                    if self.config.verbose:
                        result_message = f"结果: {result.status}, 获得 {result.points} 积分, 剩余 {result.days}, 总 {result.points_total}, {result.exchange}"
                    self._log(cookie_idx, domain, LogEmoji.SUCCESS, result_message, force=True)
                else:
                    self._log(cookie_idx, domain, LogEmoji.WARNING, result_message, force=True)

    def _cable_gate(self, api: "API", cookie: str, cookie_idx: int, domain: str) -> Tuple[bool, Dict]:
        """线路加成闸门：返回 (是否放行本次签到, 探测到的档位信息)

        规则（2026-11-13）：
          - 闸门关闭 / 探测失败 → 放行（宁可按原样签，也不能因为探测抖动漏签）
          - 已进入保底时段（北京 >= fallback_hour）→ 放行（保连续天数优先）
          - 档位 >= cable_min → 放行；否则跳过，留给下一个 cron 点
        """
        if not self.config.cable_mode:
            return True, {}

        info = api.get_cable_points(cookie)
        if not info:
            self._log(
                cookie_idx,
                domain,
                LogEmoji.WARNING,
                "线路加成档位探测失败，按原逻辑直接签到（不阻断）",
                force=True,
            )
            return True, {}

        hour = _bj_hour()
        if hour >= self.config.cable_fallback_hour:
            self._log(
                cookie_idx,
                domain,
                LogEmoji.WARNING,
                f"已进入保底时段（北京 {hour} 点 >= {self.config.cable_fallback_hour} 点），"
                f"不再等档位，直接签到（当前 Cable +{info.get('points')}）",
                force=True,
            )
            return True, info

        if int(info.get("points", 0)) >= self.config.cable_min:
            self._log(
                cookie_idx,
                domain,
                LogEmoji.SUCCESS,
                f"线路加成达标（指数 {info.get('score')} → Cable +{info.get('points')} >= "
                f"+{self.config.cable_min}），本次签到",
                force=True,
            )
            return True, info

        self._log(
            cookie_idx,
            domain,
            LogEmoji.PENDING,
            f"线路加成不足（指数 {info.get('score')} → Cable +{info.get('points')} < "
            f"+{self.config.cable_min}），跳过本轮，等下一个定时点",
            force=True,
        )
        return False, info

    def _checkin_on_domain(self, cookie: str, cookie_idx: int, domain: str) -> CheckinResult:
        result = CheckinResult(cookie_idx, domain)

        with API(domain, cookie_idx, verbose=self.config.verbose) as api:
            # 2026-11-14：Turnstile 打码服务（未配置时 None → 遇到验证就跳过）
            api.solver = getattr(self.config, "solver", None)
            self._log(cookie_idx, domain, LogEmoji.STATUS, "查询剩余天数")
            days_str, status_code = api.get_status(cookie)
            result.days = days_str
            # fork 定制：get_status 顺手带回了 email / vip，用于通知里的「账号」「套餐」
            result.email = str(api.profile.get("email") or "")
            result.plan = plan_name(api.profile.get("vip"))
            result.expire = str(api.profile.get("expire_at") or "")

            self._log(cookie_idx, domain, LogEmoji.CHECKIN, "检查今日签到状态")
            # fork 定制（2026-11-08 用户要求）：已签到就不再 POST 签到接口，
            # 一天里的其余定时点只做只读查询，避免重复请求触发平台风控。
            already, points_num, points_str, streak = api.has_checked_in_today(cookie)

            if already:
                result.status = "今日已签到，跳过"
                result.code = CheckinStatus.REPEAT
                result.points = "0"
                result.streak = streak
                result.message = "今日已有签到记录，未重复调用签到接口"
                result.points_total = points_str
                self._log(
                    cookie_idx,
                    domain,
                    LogEmoji.REPEAT,
                    "今日已签到 → 跳过签到请求（仅只读查询，未触发 POST）",
                    force=True,
                )
            else:
                # 2026-11-13：签到前先看线路加成档位。服务端结算里 Cable 占 +1~+9，
                # 档位不够（指数太好看）就先不签，等下一个 cron 点再探；
                # 到保底小时之后不管档位一律签，避免断连续天数。
                allowed, cable_info = self._cable_gate(api, cookie, cookie_idx, domain)
                if cable_info:
                    result.cable_score = int(cable_info.get("score", -1))
                    result.cable_points = int(cable_info.get("points", -1))
                if not allowed:
                    result.status = "等待线路加成，跳过"
                    result.code = CheckinStatus.SKIPPED
                    result.points = "0"
                    result.streak = streak
                    result.message = (
                        f"当前线路指数 {result.cable_score}（Cable +{result.cable_points}），"
                        f"未达阈值 +{self.config.cable_min}，本次不签，等下一轮再探"
                    )
                    result.points_total = points_str
                    result.exchange = "未执行（本轮跳过签到）"
                    self._log(cookie_idx, domain, LogEmoji.PENDING, result.message, force=True)
                    return result

                self._log(cookie_idx, domain, LogEmoji.CHECKIN, "执行签到")
                checkin_result = api.checkin(cookie)
                result.status = checkin_result["status"]
                result.code = checkin_result.get("code", CheckinStatus.FAILURE)
                result.streak = int(checkin_result.get("streak") or 0)
                result.message = str(checkin_result.get("message") or "")
                result.breakdown = str(checkin_result.get("breakdown") or "")
                # 2026-11-10 修复：原来这里没回填 points，通知里永远显示「今日：+0 分」
                result.points = str(checkin_result.get("points") or "0")

                self._log(cookie_idx, domain, LogEmoji.POINTS, "查询总积分")
                points_str, points_num = api.get_points(cookie)
                result.points_total = points_str

            # fork 定制：积分不足时也跳过兑换，避免无谓请求
            if self.config.exchange_plan in self.config.EXCHANGE_PLANS:
                required_points = self.config.EXCHANGE_PLANS[self.config.exchange_plan]
                if points_num < required_points:
                    result.exchange = f"积分不足({points_num}/{required_points})，跳过兑换"
                    self._log(
                        cookie_idx,
                        domain,
                        LogEmoji.EXCHANGE,
                        f"积分不足（{points_num}/{required_points}），跳过兑换 {self.config.exchange_plan}",
                        force=True,
                    )
                else:
                    self._log(
                        cookie_idx,
                        domain,
                        LogEmoji.EXCHANGE,
                        f"开始兑换 {self.config.exchange_plan} (需要 {required_points} 积分)",
                    )
                    result.exchange = api.exchange(cookie, self.config.exchange_plan, required_points)
            else:
                result.exchange = "未配置兑换计划，跳过自动兑换"
                self._log(cookie_idx, domain, LogEmoji.INFO, "未配置兑换计划，跳过自动兑换")

        return result

    def get_results(self) -> List[Dict[str, str]]:
        """获取所有结果"""
        return [result.to_dict() for result in self.results]

    def format_results(self) -> dict:
        """格式化结果为结构化 payload（表现交给各推送后端/日志各自渲染）"""
        results = self.get_results()

        success_count = sum(1 for r in results if r["code"] == CheckinStatus.SUCCESS)
        repeat_count = sum(1 for r in results if r["code"] == CheckinStatus.REPEAT)
        fail_count = sum(1 for r in results if r["code"] == CheckinStatus.FAILURE)
        skipped_count = sum(1 for r in results if r["code"] == CheckinStatus.SKIPPED)

        status_emoji_map = {
            CheckinStatus.SUCCESS: LogEmoji.SUCCESS,
            CheckinStatus.REPEAT: LogEmoji.REPEAT,
            CheckinStatus.SKIPPED: LogEmoji.PENDING,
            CheckinStatus.FAILURE: LogEmoji.ERROR,
        }
        title = f"GLaDOS 签到 · 成功 {success_count} · 失败 {fail_count} · 重复 {repeat_count}"
        if skipped_count:
            title += f" · 待处理 {skipped_count}"

        rows = []
        for i, res in enumerate(results, 1):
            rows.append({
                "index": i,
                "emoji": status_emoji_map.get(res["code"], LogEmoji.ERROR),
                "status": res["status"],
                "domain": res["domain"],
                "points": res["points"],
                "days": res["days"],
                "points_total": res["points_total"],
                "exchange": res["exchange"],
                "email": res.get("email", ""),
                "plan": res.get("plan", ""),
                "streak": res.get("streak", 0),
                "message": res.get("message", ""),
                "expire": res.get("expire", ""),
                "cable_score": res.get("cable_score", -1),
                "cable_points": res.get("cable_points", -1),
                "breakdown": res.get("breakdown", ""),
            })

        return {
            "title": title,
            "rows": rows,
            "success": success_count,
            "fail": fail_count,
            "repeat": repeat_count,
            "skipped": skipped_count,
        }


logger = init_logger()


def main():
    """主函数"""
    exit_code = 0
    config = None

    # 2026-11-10 用户要求：只要今天签到成功过（已推送过 TG），后面所有兜底触发点
    # 进来就直接退出 —— 不登录、不查状态、不 POST 签到接口、不连 TG，
    # 避免一天 6 个 cron 点反复打签到接口把账号搞进风控。
    # 只有当天还没签成功（失败 / 没跑过）才会往下执行。
    # 需要强制重跑时加环境变量 GLADOS_FORCE=1（手动排查用）。
    force = (os.environ.get("GLADOS_FORCE") or "").strip() in ("1", "true", "yes")
    if not force and checkin_done_today and checkin_done_today():
        logger.info(f"{LogEmoji.INFO} 今日已签到成功（状态标记存在），本次不发起任何请求，直接退出")
        logger.info(f"{LogEmoji.END} 签到完成（跳过：今日已成功）")
        return

    try:
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            payload = {"title": "# 未找到 cookies!", "rows": []}
        else:
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            checker = Checker(config)
            checker.checkin_all()

            logger.info(f"{LogEmoji.START} 步骤 3: 格式化结果")
            payload = checker.format_results()

            # 2026-11-13：全部任务都在「等线路加成」→ 本轮刻意不签。
            # 写等待标记（让工作流兜底通知不误报异常），不推送、不写「今日已完成」，
            # 让后面的 cron 点继续探档位；真签成功了才写完成标记。
            all_skipped = (
                payload.get("skipped", 0) > 0
                and payload.get("success", 0) == 0
                and payload.get("fail", 0) == 0
                and payload.get("repeat", 0) == 0
            )
            if all_skipped:
                logger.info(
                    f"{LogEmoji.PENDING} 本轮全部为「等线路加成」跳过，未签到、未推送；"
                    f"等下一个定时点继续探档位"
                )
                if mark_waiting:
                    mark_waiting()
                logger.info(f"{LogEmoji.END} 签到完成（本轮跳过：等加成档位 / 需浏览器验证，均交后续处理）")
                return

            # fork 定制（2026-10-11 用户要求）：本次「全是重复签到」也要推送，
            # 标题标注「重复运行」，便于核对是不是又多跑了一次；tg_notify 已推送会打标记，
            # 工作流兜底通知不会重复刷屏。
            all_repeat = (
                payload.get("success", 0) == 0
                and payload.get("fail", 0) == 0
                and payload.get("repeat", 0) > 0
            )
            if all_repeat:
                payload["title"] = f"🔁 {payload['title']}（重复运行：今日已签到）"
                logger.info(f"{LogEmoji.INFO} 本次全部为「重复签到」，按用户要求仍推送通知（标注为重复运行）。")

            logger.info(f"\n{LogEmoji.END} ========== 签到总结 ==========\n{payload['title']}\n{render_payload(payload)}")

            # 2026-11-10 用户要求：今天已经签成功（或服务端显示已签到）就写「今日已完成」标记。
            # 后面的兜底 cron 点读到标记就直接退出，不再打接口；只要有失败就不写，
            # 保持「没成功就继续补签」的行为。
            if mark_checkin_done:
                if payload.get("fail", 0) == 0 and (payload.get("success", 0) + payload.get("repeat", 0)) > 0:
                    mark_checkin_done()
                    logger.info(f"{LogEmoji.SUCCESS} 今日签到已完成，已写入完成标记（后续触发点将直接退出，不再请求签到接口）")
                else:
                    logger.info(f"{LogEmoji.WARNING} 本次仍有失败任务，不写完成标记，后续触发点会继续补签")

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        payload = {"title": "# 脚本执行出错", "rows": [], "note": str(e)}
        exit_code = 1

    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    push_service = PushService(config)
    push_service.send(payload["title"], payload)
    logger.info(f"{LogEmoji.END} 签到完成")

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
