# -*- coding: utf-8 -*-
"""DSH 联动"看得见"回归：审批/提问常驻提醒 + 状态派发。

背景（2026-09-29 实机排查）：本机 DSH 0.1.7-rc.2 已删除桥接插件依赖的
``/api/events.mux`` 可点击审批通道（只剩 ``/api/remote.mux``），因此
``waiting_approval`` / ``waiting_question`` 在桌宠侧此前**零可见反应**：
主人的审批等着作答，桌宠却一直在发呆。

这里把状态机信号接到既有 alert 队列（sticky + ``resolve_alert`` 精确收口）：
"DSH 在等你处理"变成一行看得见的常驻提醒，处理完自动收掉；并且不污染
legacy 状态簿记（busy 边沿、完成确认仍只由 thinking/working/idle 驱动）。
"""

from __future__ import annotations

from collections import deque

from PySide6.QtWidgets import QApplication

from pet.agent_link import AgentLinkManager
from pet.app import AppShell
from pet.config import Config

_GATES = {"state": 1.0, "activity": 1.0, "done": 1.0, "exec_failed": 1.0, "approval": 1.0}


class _Win:
    """窗口替身：既记录 alert 挂载/收口，也满足 legacy 呈现管线的最小契约。"""

    cats = {"acts": ["写代码", "原地敲击桌面互动", "吃Token", "轻快记录", "漂浮踏步"]}
    idles = ["待机呼吸"]
    scale = 1.0
    _bubble_busy_until = 0.0

    def __init__(self):
        self.shown: list[tuple[str, dict]] = []
        self.resolved: list[str] = []
        self.bubbles: list[str] = []
        self.switched: list[str] = []
        self._alert_current = None
        self._alert_queue = deque()
        self._bubble_suppressed = False
        self._sticky_bubble_active = False

    # --- 提醒队列面 ---
    def show_alert(self, text, **kwargs):
        self.shown.append((text, kwargs))

    def resolve_alert(self, alert_id):
        self.resolved.append(alert_id)

    def hide_bubble(self):
        self.shown.append(("<hide>", {}))

    # --- legacy 呈现面 ---
    def isVisible(self):
        return True

    def _switch(self, name):
        self.switched.append(name)

    def request_link_anim(self, name):
        self.switched.append(name)

    def request_link_idle(self):
        if self.idles:
            self.switched.append(self.idles[0])

    def show_bubble(self, text, *args, **kwargs):
        self.bubbles.append(text)

    def _pick(self, items):
        return items[0]


def _manager(tmp_path):
    QApplication.instance() or QApplication([])
    win = _Win()
    cfg = Config(base=tmp_path)
    cfg.data["agent_link"] = {**cfg.data.get("agent_link", {}), "report_gates": dict(_GATES)}
    cfg.save()
    clock = [1000.0]
    mgr = AgentLinkManager(win, cfg, min_interval=2.0, clock=lambda: clock[0])
    return mgr, win


def _linked(tmp_path):
    mgr, win = _manager(tmp_path)
    mgr.monitors["dsh"]._running = True   # 白盒：等效 agent_link.dsh 已启用
    return mgr, win


# ---------------------------------------------------------------- 常驻提醒


def test_waiting_approval_shows_sticky_alert(tmp_path):
    mgr, win = _linked(tmp_path)
    try:
        mgr.notify_dsh_state("waiting_approval")
    finally:
        mgr.shutdown()
    assert win.shown, "审批等待必须有可见提醒"
    text, kwargs = win.shown[-1]
    assert "批准" in text
    assert kwargs["sticky"] is True
    assert kwargs["alert_id"] == "dsh-waiting"


def test_waiting_question_shares_one_alert_id(tmp_path):
    """两类等待互斥替换：同一个 id，第二个直接接管，不会叠两条气泡。"""
    mgr, win = _linked(tmp_path)
    try:
        mgr.notify_dsh_state("waiting_approval")
        mgr.notify_dsh_state("waiting_question")
    finally:
        mgr.shutdown()
    assert [kw["alert_id"] for _, kw in win.shown] == ["dsh-waiting", "dsh-waiting"]
    assert "回答" in win.shown[-1][0]


def test_dismiss_clears_alert_once(tmp_path):
    mgr, win = _linked(tmp_path)
    try:
        mgr.notify_dsh_state("waiting_approval")
        mgr.dismiss_dsh_waiting()
        mgr.dismiss_dsh_waiting()
    finally:
        mgr.shutdown()
    assert win.resolved == ["dsh-waiting"]


def test_dismiss_without_reminder_touches_nothing(tmp_path):
    """没挂过提醒时收口必须是 no-op，否则会误关别人的提醒。"""
    mgr, win = _linked(tmp_path)
    try:
        mgr.dismiss_dsh_waiting()
    finally:
        mgr.shutdown()
    assert win.resolved == []


def test_waiting_ignored_when_link_disabled(tmp_path):
    mgr, win = _manager(tmp_path)
    try:
        mgr.notify_dsh_state("waiting_approval")
        mgr.dismiss_dsh_waiting()
    finally:
        mgr.shutdown()
    assert win.shown == []


def test_waiting_does_not_pollute_legacy_bookkeeping(tmp_path):
    """等待状态不进 busy 边沿：否则随后的 working 会被当成"新一轮开始"。

    这是把提醒做成 alert（而不是塞进 _on_agent_state）的真正原因。
    """
    mgr, win = _linked(tmp_path)
    try:
        mgr.notify_dsh_state("waiting_approval")
        assert mgr._last_raw.get("dsh") is None
        assert mgr._last_applied.get("dsh") is None
        mgr.notify_dsh_state("thinking")
    finally:
        mgr.shutdown()
    assert any("思考" in b for b in win.bubbles), win.bubbles


# ---------------------------------------------------------------- 应用派发


class _FakeAlm:
    def __init__(self, *, with_dismiss=True):
        self.calls: list[tuple[str, str]] = []
        if with_dismiss:
            self.dismiss_dsh_waiting = lambda: self.calls.append(("dismiss_waiting", ""))

    def notify_dsh_state(self, state):
        self.calls.append(("notify", state))

    def dismiss_all_interactions(self):
        self.calls.append(("dismiss_all", ""))


class _App:
    """只带 _on_dsh_state_changed 所需属性的最小宿主（island 缺席即跳过）。"""

    _on_dsh_state_changed = AppShell._on_dsh_state_changed

    def __init__(self, alm):
        self.island = None
        self._alm = alm

    def _dsh_link_manager(self):
        return self._alm


def test_app_dispatches_waiting_states_to_manager():
    alm = _FakeAlm()
    app = _App(alm)
    app._on_dsh_state_changed("working", "waiting_approval")
    app._on_dsh_state_changed("waiting_approval", "waiting_question")
    assert alm.calls == [("notify", "waiting_approval"), ("notify", "waiting_question")]


def test_app_dismisses_reminder_when_waiting_ends():
    """working/success/idle 等状态只收提醒，不改动"working 不转发"的既有契约。"""
    alm = _FakeAlm()
    app = _App(alm)
    app._on_dsh_state_changed("waiting_approval", "working")
    app._on_dsh_state_changed("working", "success")
    assert alm.calls == [("dismiss_waiting", ""), ("dismiss_waiting", "")]


def test_app_thinking_and_offline_paths_unchanged():
    alm = _FakeAlm()
    app = _App(alm)
    app._on_dsh_state_changed("idle", "thinking")
    app._on_dsh_state_changed("thinking", "offline")
    assert alm.calls == [("notify", "thinking"), ("dismiss_all", "")]


def test_app_tolerates_manager_without_new_method():
    """老桩/第三方注入的 manager 没有新方法时不许崩（hasattr 守卫）。"""
    alm = _FakeAlm(with_dismiss=False)
    app = _App(alm)
    app._on_dsh_state_changed("waiting_approval", "working")
    assert alm.calls == []
