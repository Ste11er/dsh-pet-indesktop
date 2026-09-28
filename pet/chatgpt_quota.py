# -*- coding: utf-8 -*-
"""订阅额度（ChatGPT Plus/Pro 订阅窗口用量）查询。

数据来源：本机已登录订阅账户的凭据文件 + ``GET {USAGE_URL}``（服务端用于客户端
限流的同一份 rate_limit 快照）。交互与 DeepSeek 余额一致：点击查询 → 气泡显示 →
灵动岛卡片常驻一行；可在桌宠设置中开启，复用余额自动刷新间隔。

设计取自 clawd-on-desk 的额度子系统（对照与取舍见
docs/CHATGPT-PLUS-QUOTA-2026-09-28-RESEARCH.md）：

- 内部真相统一为“已用百分比”（``used_percent``，0-100），界面显示“剩余”；
- 窗口标签按服务端返回的 ``limit_window_seconds`` 生成，**绝不写死** 5h/7d；
- 重置时刻一律存绝对 epoch 秒，倒计时在渲染时才算；
- 相对倒计时（``reset_after_seconds``）在解析时锚定成绝对时刻并量化到分钟；
- 告警阈值（85%）判定永远用 ``used_percent``，与展示成“剩余”解耦。

隐私：响应里的 ``email`` / ``user_id`` / ``account_id`` 一律不进快照、不进文案、
不进日志与异常消息（tests/test_chatgpt_quota.py 有回归断言）。

品牌政策：本文件是显式豁免文件——集成代码必须命名真实的凭据目录，
豁免登记见 tests/test_desktop_pet_features.py 的品牌守卫测试。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import escape
from pathlib import Path

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# 凭据位置：环境变量优先（CLI 自己也认这个变量），否则用主目录下的默认目录
AUTH_DIR_ENV = "CODEX_HOME"
AUTH_DIR_DEFAULT = ".codex"
AUTH_FILENAME = "auth.json"

# 告警阈值：已用百分比达到该值即视为“快用完了”
WARN_USED_PERCENT = 85.0

# 陈旧阈值：卡片行常驻显示时，超过该秒数补“…分钟前”，不把旧值当现值
STALE_AFTER_SECONDS = 120.0

# 异常 resetAt 上限：最长真实窗口是 7 天，超过 45 天视为异常数据（clawd 同规则）
MAX_RESET_AHEAD_SECONDS = 45 * 24 * 3600

# 解析顺序即展示顺序：滚动短窗在前、周窗在后
_WINDOW_KEYS = ("primary_window", "secondary_window")


class QuotaError(RuntimeError):
    """订阅额度查询的基类错误。"""


class QuotaCredentialMissing(QuotaError):
    """找不到可用的登录凭据（未装 / 未登录 / 文件损坏）。"""


class QuotaCredentialExpired(QuotaError):
    """凭据已过期，需要用户重新登录（本模块不代刷 token）。"""


class QuotaNetworkError(QuotaError):
    """网络层失败（超时 / 连不上 / 服务端错误）。"""


class QuotaResponseError(QuotaError):
    """响应结构与预期不符（缺 rate_limit / JSON 坏）。"""


@dataclass(frozen=True)
class QuotaWindow:
    """一个额度窗口：内部真相是 used_percent，展示时取 remaining_percent。"""

    label: str
    window_seconds: int
    used_percent: float
    reset_at: float | None = None
    expired: bool = False

    @property
    def remaining_percent(self) -> float:
        return 100.0 - self.used_percent

    def is_warning(self, threshold: float = WARN_USED_PERCENT) -> bool:
        return self.used_percent >= threshold

    def card_text(self) -> str:
        if self.expired:
            return f"{self.label} 已重置"
        return f"{self.label} 剩 {self.remaining_percent:.0f}%"

    def bubble_line(self, *, now: float) -> str:
        if self.expired:
            return f"{self.label}：已重置（窗口已滚动）"
        return (f"{self.label}：剩 {self.remaining_percent:.0f}%"
                f"（{format_reset_countdown(self.reset_at, now=now)}）")


@dataclass(frozen=True)
class QuotaSnapshot:
    """一次查询得到的订阅额度快照（不含任何账号标识）。"""

    plan_type: str
    windows: tuple[QuotaWindow, ...]
    fetched_at: float

    def plan_label(self) -> str:
        name = str(self.plan_type or "").strip()
        if not name:
            return "订阅额度"
        return f"{name[:1].upper()}{name[1:]} 额度"

    def warn_windows(self, threshold: float = WARN_USED_PERCENT) -> tuple[QuotaWindow, ...]:
        return tuple(w for w in self.windows if w.is_warning(threshold))

    def stale_minutes(self, *, now: float | None = None) -> int | None:
        """超过陈旧阈值时返回整分钟数（用于“N 分钟前”），否则 None。"""
        stamp = time.time() if now is None else now
        age = stamp - float(self.fetched_at)
        if age < STALE_AFTER_SECONDS:
            return None
        return max(1, int(age // 60))

    def card_line(self, *, now: float | None = None) -> str:
        parts = [self.plan_label()]
        parts.extend(w.card_text() for w in self.windows)
        line = " · ".join(parts)
        minutes = self.stale_minutes(now=now)
        if minutes is not None:
            line = f"{line}（{minutes} 分钟前）"
        return line

    def card_line_html(self, *, now: float | None = None,
                       warn_color: str = "#e5484d") -> str:
        """卡片行富文本：仅“已用 ≥ 阈值”的窗口转告警色，其余保持主题色。"""
        parts = [escape(self.plan_label())]
        for win in self.windows:
            text = escape(win.card_text())
            if win.is_warning():
                text = f'<span style="color:{warn_color}">{text}</span>'
            parts.append(text)
        line = " · ".join(parts)
        minutes = self.stale_minutes(now=now)
        if minutes is not None:
            line = f"{line}（{minutes} 分钟前）"
        return line

    def bubble_text(self, *, now: float | None = None) -> str:
        stamp = time.time() if now is None else now
        lines = [self.plan_label()]
        lines.extend(w.bubble_line(now=stamp) for w in self.windows)
        return "\n".join(lines)


def _window_label(seconds: int) -> str:
    """窗口标签由服务端秒数生成：<1h 用分钟、<48h 用小时、其余用天。"""
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} 分钟"
    if seconds < 48 * 3600:
        return f"{max(1, round(seconds / 3600))} 小时"
    return f"{max(1, round(seconds / 86400))} 天"


def format_reset_countdown(reset_at: float | None, *, now: float | None = None) -> str:
    """绝对重置时刻 → “4 小时 13 分后重置 / 5 天 10 小时后重置 / 37 分钟后重置”。"""
    if reset_at is None:
        return "重置时间未知"
    stamp = time.time() if now is None else now
    remaining = float(reset_at) - stamp
    if remaining <= 0:
        return "已重置"
    total_minutes = int(remaining // 60)
    if total_minutes < 1:
        return "不到 1 分钟后重置"
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时后重置"
    if hours:
        return f"{hours} 小时 {minutes} 分后重置"
    return f"{minutes} 分钟后重置"


def _coerce_percent(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(100.0, number))


def _coerce_seconds(value) -> int | None:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _window_reset_at(raw: dict, *, now: float) -> float | None:
    """重置时刻：优先绝对 ``reset_at``；否则用相对秒数锚定（量化到分钟）。"""
    absolute = raw.get("reset_at")
    if absolute is not None:
        try:
            stamp = float(absolute)
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None and stamp == stamp:
            # 异常数据守卫：reset 超过 45 天视为不可信，宁可显示“未知”
            if stamp > now + MAX_RESET_AHEAD_SECONDS:
                return None
            return stamp
    relative = raw.get("reset_after_seconds")
    if relative is not None:
        try:
            seconds = float(relative)
        except (TypeError, ValueError):
            return None
        if seconds == seconds:
            return round((now + seconds) / 60.0) * 60.0
    return None


def parse_usage_payload(payload: dict, *, now: float | None = None) -> QuotaSnapshot:
    """把 /wham/usage 响应归一成快照；结构与预期不符时抛 QuotaResponseError。"""
    stamp = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise QuotaResponseError("响应不是 JSON 对象")
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise QuotaResponseError("响应缺少额度信息")
    windows: list[QuotaWindow] = []
    for key in _WINDOW_KEYS:
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        used = _coerce_percent(raw.get("used_percent"))
        if used is None:
            continue
        seconds = _coerce_seconds(raw.get("limit_window_seconds")) or 0
        reset_at = _window_reset_at(raw, now=stamp)
        expired = reset_at is not None and reset_at <= stamp
        windows.append(QuotaWindow(
            label=_window_label(seconds) if seconds else "额度窗口",
            window_seconds=seconds,
            used_percent=used,
            reset_at=reset_at,
            expired=expired,
        ))
    if not windows:
        raise QuotaResponseError("响应里没有任何额度窗口")
    return QuotaSnapshot(
        plan_type=str(payload.get("plan_type") or ""),
        windows=tuple(windows),
        fetched_at=stamp,
    )


def _ssl_context(verify: bool):
    """延迟导入：无 Chat 变体排除 pet.chat 模块，顶层 import 会直接 ImportError。"""
    from .chat.providers import _make_ssl_context
    return _make_ssl_context(verify)


def resolve_auth_path(env: dict | None = None, home: Path | None = None) -> Path:
    """凭据文件路径：``$CODEX_HOME/auth.json`` 优先，否则 ``~/<默认目录>/auth.json``。"""
    source = os.environ if env is None else env
    override = str(source.get(AUTH_DIR_ENV) or "").strip()
    base = Path(override) if override else (home or Path.home()) / AUTH_DIR_DEFAULT
    return base / AUTH_FILENAME


def read_access_token(path: Path) -> tuple[str, str]:
    """读取 (access_token, account_id)；文件缺失/损坏/无凭据一律抛 Missing。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise QuotaCredentialMissing("未找到订阅凭据（请先登录本机 CLI）") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QuotaCredentialMissing("订阅凭据文件无法解析") from exc
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        raise QuotaCredentialMissing("订阅凭据里没有登录令牌")
    token = str(tokens.get("access_token") or "").strip()
    if not token:
        raise QuotaCredentialMissing("订阅凭据里没有登录令牌")
    return token, str(tokens.get("account_id") or "").strip()


def token_expired(token: str, *, now: float | None = None, skew: float = 0.0) -> bool:
    """本地解析 JWT 的 exp 做预检；解析不出来一律返回 False（交给服务端判定）。"""
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return False
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(claims, dict):
        return False
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return False
    stamp = time.time() if now is None else now
    return exp <= stamp + float(skew or 0.0)


def fetch_quota(*, auth_path: Path | None = None, timeout: float = 10.0,
                verify_ssl: bool = True, now: float | None = None) -> QuotaSnapshot:
    """查询订阅额度：读凭据 → exp 预检 → 一次 GET → 归一化。

    凭据过期时不发起请求（不代刷 token，避免轮换掉 CLI 自己的 refresh_token）。
    """
    stamp = time.time() if now is None else now
    path = Path(auth_path) if auth_path is not None else resolve_auth_path()
    token, account_id = read_access_token(path)
    if token_expired(token, now=stamp):
        raise QuotaCredentialExpired("订阅凭据已过期，请重新登录后再试")
    from .chat.providers import build_browser_headers  # 延迟导入：无 Chat 变体排除 pet.chat
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if account_id:
        headers["chatgpt-account-id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=build_browser_headers(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context(verify_ssl)) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise QuotaCredentialExpired("订阅凭据已过期或无权访问，请重新登录") from exc
        raise QuotaNetworkError(f"服务端返回 HTTP {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise QuotaNetworkError("请求超时") from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", "") or "")
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise QuotaNetworkError("请求超时") from exc
        raise QuotaNetworkError("网络连接失败") from exc
    except (OSError, ValueError) as exc:
        raise QuotaNetworkError(f"网络连接失败：{exc}") from exc
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise QuotaResponseError("返回数据不是合法 JSON") from exc
    return parse_usage_payload(payload, now=stamp)
