# -*- coding: utf-8 -*-
"""订阅额度（ChatGPT Plus/Pro 窗口用量）UI 接线测试。

覆盖四件事：
1. 点击入口按两个开关派发（只开额度 / 只开余额 / 都关 = 零请求零 UI 变化）；
2. 余额段与额度段合成**同一个**气泡，任一段失败只降级该段；
3. 灵动岛额度行默认隐藏、有数据才占位加高、清空后高度回落；
4. 30s 内重复点击命中缓存，不发第二次请求。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QWidget

from pet import chatgpt_quota as quota_mod
from pet.app import AppShell, _InfoBridge, _show_info_payload
from pet.config import Config
from pet.dynamic_island import _QUOTA_ROW_HEIGHT, DynamicIsland


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot(primary_used: float = 9, secondary_used: float = 2):
    now = time.time()
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": primary_used,
                "limit_window_seconds": 18000,
                "reset_at": int(now) + 15187,
            },
            "secondary_window": {
                "used_percent": secondary_used,
                "limit_window_seconds": 604800,
                "reset_at": int(now) + 469892,
            },
        },
    }
    return quota_mod.parse_usage_payload(payload, now=now)


class _BubbleWin(QWidget):
    """真 QWidget 桩：``shiboken6.isValid`` 才认，同时记录 show_bubble 调用。"""

    cfg = None

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def show_bubble(self, text, **kwargs):
        self.calls.append((str(text), dict(kwargs)))

    @property
    def texts(self) -> list[str]:
        return [text for text, _kw in self.calls]


class _FakeCfg:
    dir = None

    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)


def _click_host(**attrs):
    """走真实点击入口 ``PetWindow._on_click`` + 最小宿主替身。"""
    from pet.window import PetWindow

    class _Pet:
        _just_dragged = False
        clicks = ["click-1"]
        click_show_balance = False
        click_show_quota = False
        click_show_self_talk = False
        on_show_balance = None
        on_restore_fun_windows = None
        _effects_consume_click = None
        _effects_route_click_golden_spin = None

        _pick = staticmethod(lambda seq: seq[0])
        _cancel_move = lambda self: None  # noqa: E731
        _start_squash = lambda self: None  # noqa: E731
        _switch = lambda self, name: None  # noqa: E731
        _schedule_click_sound = lambda self: None  # noqa: E731

        def __init__(self):
            self.cfg = _FakeCfg({"click_sound_pack": {"kind": "custom"}})
            self.info_calls: list[str] = []
            self.self_talk_calls: list[str] = []
            self.on_show_balance = lambda win: self.info_calls.append("info")
            for key, value in attrs.items():
                setattr(self, key, value)

        def _show_click_self_talk(self, name):
            self.self_talk_calls.append(name)
            return True

        def _schedule_self_talk(self, **kwargs):
            self.self_talk_calls.append("scheduled")

    pet = _Pet()
    PetWindow._on_click(pet)
    return pet


def _island(tmp_path, **overrides) -> DynamicIsland:
    cfg = Config(base=tmp_path)
    data = {
        "enabled": True, "show_icon": True, "show_name": True,
        "show_info": True, "info_mode": "time", "custom_text": "",
        "show_status": True, "style": "dark", "x": 400, "y": 300,
    }
    data.update(overrides)
    cfg.set("dynamic_island", data)
    return DynamicIsland(cfg)


def _shell(tmp_path, **cfg) -> AppShell:
    shell = AppShell.__new__(AppShell)
    shell.config = Config(base=tmp_path)
    for key, value in cfg.items():
        shell.config.set(key, value)
    shell.instance = None
    shell.enable_chat = True
    island = SimpleNamespace(lines=[], set_quota_info=lambda text: island.lines.append(text))
    shell.island = island
    return shell


# --------------------------------------------------------------- 点击入口


def test_click_dispatches_info_when_only_quota_enabled():
    pet = _click_host(click_show_quota=True)
    assert pet.info_calls == ["info"]
    assert pet.self_talk_calls == []


def test_click_dispatches_info_when_only_balance_enabled():
    pet = _click_host(click_show_balance=True)
    assert pet.info_calls == ["info"]


def test_click_does_nothing_when_both_provider_switches_off():
    pet = _click_host()
    assert pet.info_calls == []
    assert pet.self_talk_calls == []


def test_balance_takes_precedence_over_self_talk():
    pet = _click_host(click_show_balance=True, click_show_self_talk=True)
    assert pet.info_calls == ["info"]
    assert pet.self_talk_calls == []


def test_self_talk_still_runs_when_providers_off():
    pet = _click_host(click_show_self_talk=True)
    assert pet.info_calls == []
    assert pet.self_talk_calls == ["click-1", "scheduled"]


# --------------------------------------------------------------- 灵动岛额度行


def test_island_quota_row_hidden_by_default_and_grows_card(tmp_path):
    app = _qapp()
    island = _island(tmp_path)
    try:
        island.show()
        island.expand_card()
        island._finish_animations()
        app.processEvents()
        assert island._card_quota_label.isHidden()
        base = island.height()
        line = "Plus 额度 · 5 小时 剩 91% · 7 天 剩 98%"

        island.set_quota_info(line)
        island._finish_animations()
        app.processEvents()
        assert island._card_quota_label.text() == line
        assert not island._card_quota_label.isHidden()
        assert island.height() == base + _QUOTA_ROW_HEIGHT

        island.set_quota_info("")
        island._finish_animations()
        app.processEvents()
        assert island._card_quota_label.isHidden()
        assert island.height() == base
    finally:
        island.hide()
        island.deleteLater()
        app.processEvents()


# --------------------------------------------------------------- 气泡合成


def test_one_bubble_carries_balance_and_quota_sections():
    app = _qapp()
    win = _BubbleWin()
    try:
        _show_info_payload(win, {"text": "余额 ¥12.34", "info": {}}, _snapshot(), None, None)
        assert len(win.calls) == 1, "两个 provider 必须合成一个气泡"
        text, kwargs = win.calls[0]
        assert "余额 ¥12.34" in text
        assert "Plus 额度" in text
        assert "5 小时：剩 91%" in text
        assert "7 天：剩 98%" in text
        assert kwargs.get("subtitle"), "带余额段时仍要保留峰谷副标题"
    finally:
        win.deleteLater()
        app.processEvents()


def test_quota_only_bubble_has_no_balance_subtitle():
    app = _qapp()
    win = _BubbleWin()
    try:
        _show_info_payload(win, None, _snapshot(), None, None)
        assert len(win.calls) == 1
        text, kwargs = win.calls[0]
        assert "Plus 额度" in text
        assert "余额" not in text
        assert not kwargs.get("subtitle")
    finally:
        win.deleteLater()
        app.processEvents()


def test_quota_failure_degrades_bubble_and_island():
    app = _qapp()
    win = _BubbleWin()
    island = SimpleNamespace(lines=[], set_quota_info=lambda text: island.lines.append(text))
    owner = SimpleNamespace(
        island=island,
        _update_island_quota=lambda snapshot: island.lines.append("row"),
        _update_island_balance=lambda payload: island.lines.append("balance-row"),
    )
    try:
        bridge = _InfoBridge(win, owner=owner)
        bridge.done.emit(True, ({"text": "余额 ¥1.00", "info": {}}, None, None,
                                "额度查询失败：网络连接失败"))
        text = win.calls[-1][0]
        assert "余额 ¥1.00" in text, "额度失败不能吞掉余额段"
        assert "额度查询失败" in text
        assert island.lines[-1] == "balance-row", "余额行照常更新"
        assert "额度查询失败" in island.lines, "额度行给出明确状态，不留旧值"
    finally:
        win.deleteLater()
        app.processEvents()


# --------------------------------------------------------------- shell 派发与缓存


def test_show_click_info_noop_when_both_switches_off(tmp_path):
    shell = _shell(tmp_path)
    calls = []
    shell.show_balance = lambda parent=None: calls.append("balance")
    shell.show_combined = lambda parent=None, **kw: calls.append(("combined", kw))
    win = _BubbleWin()
    try:
        shell.show_click_info(win)
        assert calls == [], "两个开关都关时不得发起任何查询"
        assert win.calls == [], "两个开关都关时不得有任何 UI 变化"
    finally:
        win.deleteLater()
        _qapp().processEvents()


def test_show_click_info_combines_when_quota_on(tmp_path):
    shell = _shell(tmp_path, click_show_quota=True)
    calls = []
    shell.show_balance = lambda parent=None: calls.append("balance")
    shell.show_combined = lambda parent=None, **kw: calls.append(("combined", kw))
    shell.show_click_info(None)
    assert calls == [("combined", {"force_balance": False})]


def test_show_click_info_balance_only_keeps_old_path(tmp_path):
    shell = _shell(tmp_path, click_show_balance=True)
    calls = []
    shell.show_balance = lambda parent=None: calls.append("balance")
    shell.show_combined = lambda parent=None, **kw: calls.append("combined")
    shell.show_click_info(None)
    assert calls == ["balance"]


def test_info_worker_queries_once_then_serves_from_cache(tmp_path, monkeypatch):
    shell = _shell(tmp_path, click_show_quota=True)
    win = _BubbleWin()
    fetches = []
    monkeypatch.setattr(quota_mod, "fetch_quota",
                        lambda: (fetches.append(1), _snapshot())[1])
    try:
        shell._info_worker(_InfoBridge(win, owner=shell), want_balance=False, want_quota=True)
        assert len(fetches) == 1
        assert len(win.calls) == 1, "即便是纯额度查询也只冒一个气泡"
        assert "Plus 额度" in win.calls[0][0]
        assert shell.island.lines[-1].startswith("Plus 额度")
        first = win.calls[0][0]

        shell._info_worker(_InfoBridge(win, owner=shell), want_balance=False, want_quota=True)
        assert len(fetches) == 1, "30s 内重复点击不得再发请求"
        assert win.calls[-1][0] == first
    finally:
        win.deleteLater()
        _qapp().processEvents()


def test_config_roundtrip_quota_switch(tmp_path):
    cfg = Config(base=tmp_path)
    assert cfg.get("click_show_quota", False) is False
    cfg.set("click_show_quota", True)
    cfg.save()
    assert Config(base=tmp_path).get("click_show_quota", False) is True


# --------------------------------------------------------------- 设置写回 / 占位 / 自动刷新


def test_settings_writeback_saves_both_provider_switches():
    from pet.settings_pet_controls import save_click_provider_toggles

    class _Check:
        def __init__(self, checked):
            self._checked = checked

        def isChecked(self):
            return self._checked

    class _Cfg:
        def __init__(self):
            self.written = {}

        def set(self, key, value):
            self.written[key] = value

    dialog = SimpleNamespace(click_balance_check=_Check(True),
                             click_quota_check=_Check(True), config=_Cfg())
    save_click_provider_toggles(dialog)
    assert dialog.config.written == {"click_show_balance": True, "click_show_quota": True}

    # 无 AI 变体：余额控件为 None，不得因此漏写额度键
    plain = SimpleNamespace(click_balance_check=None,
                            click_quota_check=_Check(False), config=_Cfg())
    save_click_provider_toggles(plain)
    assert plain.config.written == {"click_show_quota": False}


def test_island_placeholder_follows_switch_and_cache(tmp_path):
    shell = _shell(tmp_path, click_show_quota=True)
    shell._sync_island_quota_placeholder()
    assert shell.island.lines[-1] == "订阅额度 待查询"

    shell._quota_cache = (time.monotonic(), _snapshot())
    shell._sync_island_quota_placeholder()
    assert shell.island.lines[-1].startswith("Plus 额度")

    off = _shell(tmp_path)
    off._sync_island_quota_placeholder()
    assert off.island.lines == [""], "开关关闭时整行隐藏"


def test_balance_timer_includes_quota_only_when_enabled(tmp_path):
    calls = []
    shell = _shell(tmp_path, click_show_quota=True)
    shell.show_combined = lambda parent=None, **kw: calls.append(("combined", kw))
    shell.show_balance = lambda parent=None: calls.append("balance")
    shell._on_balance_timer()
    assert calls == [("combined", {"force_balance": True})]

    calls.clear()
    plain = _shell(tmp_path)
    plain.show_combined = lambda parent=None, **kw: calls.append("combined")
    plain.show_balance = lambda parent=None: calls.append("balance")
    plain._on_balance_timer()
    assert calls == ["balance"], "额度关闭时自动刷新行为与旧版一致"
