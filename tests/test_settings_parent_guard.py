# -*- coding: utf-8 -*-
"""独立设置进程「父死自退」看门狗的聚焦测试。

背景（2026-09-29 实机）：桌宠退出后设置进程被 init 收养，占着 settings.lock
继续活了 30 分钟，于是后续设置入口打不开、桌宠气泡被当成"设置开着"全抑制。
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

from pet import settings_parent_guard as guard


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_gone_when_adopted_by_init():
    assert guard.parent_process_gone(4242, getppid=lambda: 1, pid_alive=lambda _p: True) is True


def test_gone_when_pid_disappeared():
    assert guard.parent_process_gone(4242, getppid=lambda: 4242, pid_alive=lambda _p: False) is True


def test_not_gone_while_parent_alive():
    assert guard.parent_process_gone(4242, getppid=lambda: 4242, pid_alive=lambda _p: True) is False


def test_getppid_failure_falls_back_to_pid_probe():
    def _boom():
        raise OSError("no /proc")

    assert guard.parent_process_gone(4242, getppid=_boom, pid_alive=lambda _p: False) is True
    assert guard.parent_process_gone(4242, getppid=_boom, pid_alive=lambda _p: True) is False


def test_pid_zero_is_never_alive():
    assert guard._pid_alive(0) is False


def test_watch_fires_once_and_is_idempotent(monkeypatch):
    _qapp()
    fired = []
    watch = guard.ParentWatch(lambda: fired.append(1), recorded_ppid=4242, interval_ms=10)
    monkeypatch.setattr(guard, "parent_process_gone", lambda _ppid, **_kw: True)
    watch.start()
    assert watch.fired is True
    assert watch.check() is True  # 幂等：不再重复回调
    assert fired == [1]


def test_watch_stays_quiet_while_parent_alive(monkeypatch):
    _qapp()
    fired = []
    watch = guard.ParentWatch(lambda: fired.append(1), recorded_ppid=4242, interval_ms=10)
    monkeypatch.setattr(guard, "parent_process_gone", lambda _ppid, **_kw: False)
    try:
        watch.start()
        assert watch.check() is False
        assert watch.fired is False
        assert fired == []
    finally:
        watch.stop()


def test_callback_exception_does_not_escape(monkeypatch):
    _qapp()
    watch = guard.ParentWatch(lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                              recorded_ppid=4242, interval_ms=10)
    monkeypatch.setattr(guard, "parent_process_gone", lambda _ppid, **_kw: True)
    assert watch.check() is True
    assert watch.fired is True


def test_stop_is_safe_after_dialog_destroyed():
    """设置窗 ``WA_DeleteOnClose`` 自毁会把 QTimer 一起带走，收尾仍须可调。

    否则 ``_exec_settings`` 的 finally 会在 ``lock.unlock()`` 之前抛出去，
    留下残余锁把后续设置入口堵住（2026-09-29 真机抓到过这条 traceback）。
    """
    _qapp()
    import shiboken6
    from PySide6.QtWidgets import QDialog

    dialog = QDialog()
    watch = guard.ParentWatch(dialog.reject, parent=dialog, interval_ms=10)
    watch.start()
    shiboken6.delete(dialog)  # 等价于对话框关闭后的自毁
    watch.stop()  # 不许抛

