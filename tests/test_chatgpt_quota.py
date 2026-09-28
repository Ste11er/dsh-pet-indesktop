# -*- coding: utf-8 -*-
"""订阅额度（ChatGPT Plus/Pro 订阅窗口用量）查询的测试。

真实响应 schema 与抓样说明见 docs/CHATGPT-PLUS-QUOTA-2026-09-28-RESEARCH.md。
本文件与 pet/chatgpt_quota.py 是品牌政策的显式豁免对（同 agent_link.py 的
理由：集成代码必须命名真实主机的凭据路径），豁免登记在
tests/test_desktop_pet_features.py::test_product_copy_has_no_external_brand_reference。
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
from pathlib import Path

import pytest

from pet import chatgpt_quota as q

# 抓样时刻：让 primary 的 reset_at - NOW == 15187 秒，secondary 同理 469892 秒
NOW = 1790577992.0

# 真实抓样（身份字段已替换为明显假值；结构与线上一致）
REAL_PAYLOAD = {
    "user_id": "user-FAKEIDENTITY",
    "account_id": "acct-FAKEIDENTITY",
    "email": "someone@example.invalid",
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 9,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 15187,
            "reset_at": 1790593179,
        },
        "secondary_window": {
            "used_percent": 2,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 469892,
            "reset_at": 1791047884,
        },
    },
    "code_review_rate_limit": None,
    "additional_rate_limits": None,
    "model_usage": {"gpt-6-astra": {"available": True, "available_at": None}},
    "credits": {
        "has_credits": True,
        "unlimited": False,
        "overage_limit_reached": False,
        "balance": "1035.2813390000",
    },
    "spend_control": {"reached": False, "individual_limit": None},
    "rate_limit_reached_type": None,
    "promo": None,
    "rate_limit_reset_credits": {"available_count": 1, "applicable_available_count": 1},
}


def _payload(*, plan_type: str = "plus", **rate_limit) -> dict:
    return {"plan_type": plan_type, "rate_limit": rate_limit}


def _window(used=1, seconds=18000, reset_at=None, reset_after=None) -> dict:
    win = {"used_percent": used, "limit_window_seconds": seconds}
    if reset_at is not None:
        win["reset_at"] = reset_at
    if reset_after is not None:
        win["reset_after_seconds"] = reset_after
    return win


def _jwt(exp: float) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


def _auth_file(tmp_path: Path, *, token: str = "tok-secret", account: str = "acct-1",
               exp: float = NOW + 3600) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": token if token != "jwt" else _jwt(exp),
            "id_token": "id",
            "refresh_token": "refresh",
            "account_id": account,
        },
        "last_refresh": "2026-09-28T06:28:14Z",
    }), encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------- 响应解析


def test_parse_real_payload_windows_and_labels():
    snap = q.parse_usage_payload(REAL_PAYLOAD, now=NOW)
    assert snap.plan_type == "plus"
    assert snap.plan_label() == "Plus 额度"
    assert [w.label for w in snap.windows] == ["5 小时", "7 天"]
    assert [w.window_seconds for w in snap.windows] == [18000, 604800]
    assert [w.used_percent for w in snap.windows] == [9.0, 2.0]
    assert [w.remaining_percent for w in snap.windows] == [91.0, 98.0]
    assert snap.windows[0].reset_at == 1790593179


def test_window_label_follows_server_seconds():
    """绝不写死 5h/7d：服务端给 3600 秒就得写“1 小时”。"""
    snap = q.parse_usage_payload(
        _payload(primary_window=_window(seconds=3600, reset_at=int(NOW) + 600)), now=NOW)
    assert snap.windows[0].label == "1 小时"


def test_used_percent_is_clamped_to_0_100():
    snap = q.parse_usage_payload(_payload(
        primary_window=_window(used=140, reset_at=int(NOW) + 60),
        secondary_window=_window(used=-3, seconds=604800, reset_at=int(NOW) + 60),
    ), now=NOW)
    assert [w.used_percent for w in snap.windows] == [100.0, 0.0]
    assert [w.remaining_percent for w in snap.windows] == [0.0, 100.0]


def test_relative_reset_is_anchored_absolute_and_minute_quantized():
    snap = q.parse_usage_payload(
        _payload(primary_window=_window(reset_after=15187)), now=NOW)
    assert snap.windows[0].reset_at == pytest.approx(round((NOW + 15187) / 60) * 60, abs=1)


def test_expired_window_is_flagged_and_rendered_as_reset():
    snap = q.parse_usage_payload(
        _payload(primary_window=_window(used=5, reset_at=int(NOW) - 10)), now=NOW)
    assert snap.windows[0].expired is True
    bubble = snap.bubble_text(now=NOW)
    assert "已重置" in bubble
    assert "剩 95%" not in bubble


def test_missing_rate_limit_raises_response_error():
    with pytest.raises(q.QuotaResponseError):
        q.parse_usage_payload({"plan_type": "plus", "rate_limit": None}, now=NOW)


def test_all_windows_null_raises_response_error():
    with pytest.raises(q.QuotaResponseError):
        q.parse_usage_payload(_payload(primary_window=None, secondary_window=None), now=NOW)


def test_single_window_payload_keeps_that_window():
    snap = q.parse_usage_payload(
        _payload(secondary_window=_window(used=2, seconds=604800, reset_at=int(NOW) + 60)),
        now=NOW)
    assert [w.label for w in snap.windows] == ["7 天"]


def test_plan_label_falls_back_without_plan_type():
    assert q.parse_usage_payload(
        _payload(plan_type="", primary_window=_window(reset_at=int(NOW) + 60)), now=NOW
    ).plan_label() == "订阅额度"


def test_plan_label_uses_plan_name():
    pay = _payload(primary_window=_window(reset_at=int(NOW) + 60))
    pay["plan_type"] = "pro"
    assert q.parse_usage_payload(pay, now=NOW).plan_label() == "Pro 额度"


def test_reset_countdown_formats_hours_minutes_and_days():
    primary, secondary = REAL_PAYLOAD["rate_limit"]["primary_window"], \
        REAL_PAYLOAD["rate_limit"]["secondary_window"]
    snap = q.parse_usage_payload(_payload(primary_window=primary, secondary_window=secondary),
                                 now=NOW)
    assert q.format_reset_countdown(snap.windows[0].reset_at, now=NOW) == "4 小时 13 分后重置"
    assert q.format_reset_countdown(snap.windows[1].reset_at, now=NOW) == "5 天 10 小时后重置"
    assert q.format_reset_countdown(NOW + 2237, now=NOW) == "37 分钟后重置"
    assert q.format_reset_countdown(NOW - 5, now=NOW) == "已重置"


def test_card_line_lists_windows_with_remaining_percent():
    snap = q.parse_usage_payload(REAL_PAYLOAD, now=NOW)
    assert snap.card_line(now=NOW) == "Plus 额度 · 5 小时 剩 91% · 7 天 剩 98%"


def test_card_line_marks_stale_snapshot():
    snap = q.parse_usage_payload(REAL_PAYLOAD, now=NOW)
    assert "分钟前" in snap.card_line(now=NOW + 600)
    assert "分钟前" not in snap.card_line(now=NOW + 30)


def test_card_line_html_marks_only_warn_windows():
    snap = q.parse_usage_payload(_payload(
        primary_window=_window(used=85, reset_at=int(NOW) + 60),
        secondary_window=_window(used=84, seconds=604800, reset_at=int(NOW) + 60),
    ), now=NOW)
    html = snap.card_line_html(now=NOW, warn_color="#ff0000")
    assert "#ff0000" in html
    assert html.count("#ff0000") == 1
    assert q.WARN_USED_PERCENT == 85.0
    calm = q.parse_usage_payload(_payload(
        primary_window=_window(used=84, reset_at=int(NOW) + 60)), now=NOW)
    assert "#ff0000" not in calm.card_line_html(now=NOW, warn_color="#ff0000")


def test_bubble_text_has_plan_and_each_window():
    snap = q.parse_usage_payload(REAL_PAYLOAD, now=NOW)
    bubble = snap.bubble_text(now=NOW)
    lines = bubble.splitlines()
    assert lines[0] == "Plus 额度"
    assert "5 小时：剩 91%（4 小时 13 分后重置）" in lines
    assert "7 天：剩 98%（5 天 10 小时后重置）" in lines


# --------------------------------------------------------------- 凭据读取


def test_resolve_auth_path_defaults_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv(q.AUTH_DIR_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert q.resolve_auth_path() == tmp_path / ".codex" / "auth.json"


def test_resolve_auth_path_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(q.AUTH_DIR_ENV, str(tmp_path / "custom"))
    assert q.resolve_auth_path() == tmp_path / "custom" / "auth.json"


def test_read_access_token_missing_file(tmp_path):
    with pytest.raises(q.QuotaCredentialMissing):
        q.read_access_token(tmp_path / "nope.json")


def test_read_access_token_bad_json(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(q.QuotaCredentialMissing):
        q.read_access_token(path)


def test_read_access_token_without_tokens(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
    with pytest.raises(q.QuotaCredentialMissing):
        q.read_access_token(path)


def test_read_access_token_returns_token_and_account(tmp_path):
    path = _auth_file(tmp_path, token="tok-secret", account="acct-9")
    assert q.read_access_token(path) == ("tok-secret", "acct-9")


def test_token_expired_detects_jwt_and_fails_open_on_opaque_token():
    assert q.token_expired(_jwt(NOW - 10), now=NOW) is True
    assert q.token_expired(_jwt(NOW + 10), now=NOW) is False
    assert q.token_expired("not-a-jwt", now=NOW) is False


def test_fetch_quota_precheck_avoids_network_when_expired(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="jwt", exp=NOW - 60)
    calls = []

    def fake_urlopen(req, *args, **kwargs):
        calls.append(req)
        raise AssertionError("过期凭据不应发起请求")

    monkeypatch.setattr(q.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(q.QuotaCredentialExpired):
        q.fetch_quota(auth_path=path, now=NOW)
    assert calls == []


# --------------------------------------------------------------- HTTP 层


def test_fetch_quota_request_shape(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="jwt", account="acct-77", exp=NOW + 3600)
    seen = {}

    def fake_urlopen(req, *args, **kwargs):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResponse(json.dumps(REAL_PAYLOAD).encode("utf-8"))

    monkeypatch.setattr(q.urllib.request, "urlopen", fake_urlopen)
    snap = q.fetch_quota(auth_path=path, timeout=3.5, now=NOW)
    assert seen["url"] == q.USAGE_URL
    assert seen["headers"]["authorization"].startswith("Bearer ")
    assert seen["headers"]["chatgpt-account-id"] == "acct-77"
    assert seen["timeout"] == 3.5
    assert [w.remaining_percent for w in snap.windows] == [91.0, 98.0]


def test_fetch_quota_without_account_id_omits_header(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="jwt", account="", exp=NOW + 3600)
    seen = {}

    def fake_urlopen(req, *args, **kwargs):
        seen["headers"] = {k.lower() for k, _value in req.header_items()}
        return _FakeResponse(json.dumps(REAL_PAYLOAD).encode("utf-8"))

    monkeypatch.setattr(q.urllib.request, "urlopen", fake_urlopen)
    q.fetch_quota(auth_path=path, now=NOW)
    assert "chatgpt-account-id" not in seen["headers"]


@pytest.mark.parametrize("code,expected", [(401, q.QuotaCredentialExpired), (403, q.QuotaCredentialExpired)])
def test_fetch_quota_auth_errors_mean_expired(tmp_path, monkeypatch, code, expected):
    path = _auth_file(tmp_path, token="jwt", exp=NOW + 3600)

    def fake_urlopen(req, *args, **kwargs):
        raise urllib.error.HTTPError(q.USAGE_URL, code, "nope", {}, None)

    monkeypatch.setattr(q.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(expected):
        q.fetch_quota(auth_path=path, now=NOW)


@pytest.mark.parametrize("code", [500, 502, 429])
def test_fetch_quota_server_errors_mean_network(tmp_path, monkeypatch, code):
    path = _auth_file(tmp_path, token="jwt", exp=NOW + 3600)

    def fake_urlopen(req, *args, **kwargs):
        raise urllib.error.HTTPError(q.USAGE_URL, code, "boom", {}, None)

    monkeypatch.setattr(q.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(q.QuotaNetworkError):
        q.fetch_quota(auth_path=path, now=NOW)


def test_fetch_quota_timeout_and_unreachable_mean_network(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="jwt", exp=NOW + 3600)
    for exc in (socket.timeout("timed out"), urllib.error.URLError("no route")):
        monkeypatch.setattr(q.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(exc))
        with pytest.raises(q.QuotaNetworkError):
            q.fetch_quota(auth_path=path, now=NOW)


def test_fetch_quota_bad_json_and_missing_windows_are_response_errors(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="jwt", exp=NOW + 3600)
    for body in (b"{not json", json.dumps({"plan_type": "plus"}).encode("utf-8")):
        monkeypatch.setattr(q.urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(body))
        with pytest.raises(q.QuotaResponseError):
            q.fetch_quota(auth_path=path, now=NOW)


# --------------------------------------------------------------- 隐私


def test_formatted_output_never_leaks_identity(tmp_path):
    snap = q.parse_usage_payload(REAL_PAYLOAD, now=NOW)
    rendered = "\n".join([
        snap.card_line(now=NOW),
        snap.card_line_html(now=NOW, warn_color="#f00"),
        snap.bubble_text(now=NOW),
        snap.plan_label(),
        repr(snap),
    ])
    for leak in ("FAKEIDENTITY", "someone@example.invalid", "acct-", "user-"):
        assert leak not in rendered


def test_error_messages_never_contain_the_token(tmp_path, monkeypatch):
    path = _auth_file(tmp_path, token="super-secret-token", exp=NOW + 3600)
    monkeypatch.setattr(q.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x")))
    with pytest.raises(q.QuotaNetworkError) as err:
        q.fetch_quota(auth_path=path, now=NOW)
    assert "super-secret-token" not in str(err.value)
