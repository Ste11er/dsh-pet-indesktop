# -*- coding: utf-8 -*-
"""设置「把已有窗口叫到前台」通道的聚焦测试。

时序纪律：同进程的 QLocalSocket 往返要靠有界轮询 + processEvents 推事件，
不用固定 sleep 猜时序（CI runner 是本地数倍慢，见 AGENTS.md）。
"""
from __future__ import annotations

import socket as socket_mod
import sys
import time

import pytest
from PySide6.QtCore import QDir
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from pet import settings_channel as channel


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _pump(predicate, *, budget: float = 5.0) -> bool:
    """有界轮询 + processEvents：等到 predicate 成立或超预算。"""
    app = _qapp()
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_channel_name_is_stable_short_and_per_directory(tmp_path):
    first = channel.channel_name(tmp_path / "a")
    assert first == channel.channel_name(tmp_path / "a")
    assert first != channel.channel_name(tmp_path / "b")
    assert first.startswith(channel.CHANNEL_PREFIX + "-")
    # POSIX 上它会落成 /tmp/<name> 的 Unix socket 路径：名字必须短
    assert len(first) <= 60


def test_raise_round_trip(tmp_path):
    _qapp()
    server = channel.SettingsRaiseServer(tmp_path)
    seen = []
    server.raise_requested.connect(lambda: seen.append(1))
    try:
        assert server.start() is True
        assert channel.raise_existing_settings(tmp_path) is True
        assert _pump(lambda: seen)
        assert seen == [1]
    finally:
        server.stop()


def test_raise_without_server_returns_false_within_budget(tmp_path):
    _qapp()
    started = time.monotonic()
    assert channel.raise_existing_settings(tmp_path, timeout_ms=150) is False
    assert time.monotonic() - started < 5.0  # 有硬上限，不无限等


def test_garbage_payload_is_ignored(tmp_path):
    _qapp()
    server = channel.SettingsRaiseServer(tmp_path)
    seen = []
    server.raise_requested.connect(lambda: seen.append(1))
    client = QLocalSocket()
    try:
        assert server.start() is True
        client.connectToServer(channel.channel_name(tmp_path))
        assert client.waitForConnected(2000)
        client.write(b"noise\n")
        client.flush()
        client.waitForBytesWritten(2000)
        _pump(lambda: client.bytesToWrite() == 0)
        assert seen == []
    finally:
        client.disconnectFromServer()
        server.stop()


def test_stop_releases_endpoint_so_a_new_server_can_listen(tmp_path):
    _qapp()
    first = channel.SettingsRaiseServer(tmp_path)
    assert first.start() is True
    first.stop()
    second = channel.SettingsRaiseServer(tmp_path)
    try:
        assert second.start() is True
    finally:
        second.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="陈旧 Unix socket 端点只在 POSIX 上有")
def test_start_reclaims_stale_endpoint(tmp_path):
    """上次进程被 kill 留下的端点文件不许堵死通道（本机实测过同类残留）。"""
    _qapp()
    endpoint = f"{QDir.tempPath()}/{channel.channel_name(tmp_path)}"
    stale = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    try:
        stale.bind(endpoint)
    finally:
        stale.close()
    server = channel.SettingsRaiseServer(tmp_path)
    try:
        assert server.start() is True
    finally:
        server.stop()


def test_stop_is_safe_after_dialog_destroyed(tmp_path):
    """对话框自毁带走 QLocalServer 后：stop() 不抛，且端点不残留（新服务能监听）。"""
    _qapp()
    import shiboken6
    from PySide6.QtWidgets import QDialog

    dialog = QDialog()
    server = channel.SettingsRaiseServer(tmp_path, parent=dialog)
    assert server.start() is True
    shiboken6.delete(dialog)
    server.stop()  # 不许抛
    fresh = channel.SettingsRaiseServer(tmp_path)
    try:
        assert fresh.start() is True  # 陈旧端点没有堵住新服务
    finally:
        fresh.stop()
