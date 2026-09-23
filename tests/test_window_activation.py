# -*- coding: utf-8 -*-
"""Linux/X11 不抢焦点窗口的规则：不再设 `WA_ShowWithoutActivating`。

背景（实机二分验证，见 `docs/KDE-TASKBAR-DEMANDS-ATTENTION-2026-09-22.md`）：
在 KDE/X11 上，带 `WA_ShowWithoutActivating` 的顶层窗口会被打上
`_NET_WM_STATE_DEMANDS_ATTENTION`；而这类窗口同时带 `WindowDoesNotAcceptFocus`
（`WM_HINTS` input hint = False），WM 永远不会激活它，标记因此永不自动清除——
任务栏条目持续高亮、被反复唤醒。修复是把这条平台规则收口到
`pet/window_activation.py`，由 `apply_show_without_activating()` 统一裁决。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from pet import window_activation
from pet.window_activation import apply_show_without_activating

_TOOL_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
)
_NO_FOCUS_FLAGS = _TOOL_FLAGS | Qt.WindowType.WindowDoesNotAcceptFocus


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _window(flags: Qt.WindowType) -> QWidget:
    _qapp()
    widget = QWidget()
    widget.setWindowFlags(flags)
    return widget


def _show_without_activating(widget: QWidget) -> bool:
    return widget.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


# ---------------------------------------------------------------- 规则本身

def test_linux_skips_attribute_when_window_already_refuses_focus(monkeypatch):
    """Linux 上窗口已带 WindowDoesNotAcceptFocus → 不再设该属性（任务栏高亮根因）。"""
    monkeypatch.setattr(window_activation, "_is_linux", lambda: True)
    widget = _window(_NO_FOCUS_FLAGS)

    assert apply_show_without_activating(widget) is False
    assert _show_without_activating(widget) is False


def test_linux_keeps_attribute_when_window_can_take_focus(monkeypatch):
    """兜底：没有 WindowDoesNotAcceptFocus 的窗口必须保留该属性，否则会抢焦点。"""
    monkeypatch.setattr(window_activation, "_is_linux", lambda: True)
    widget = _window(_TOOL_FLAGS)

    assert apply_show_without_activating(widget) is True
    assert _show_without_activating(widget) is True


def test_non_linux_keeps_attribute(monkeypatch):
    """Windows/macOS 上该属性是"显示但不成为前台/key window"的手段，不能省。"""
    monkeypatch.setattr(window_activation, "_is_linux", lambda: False)
    widget = _window(_NO_FOCUS_FLAGS)

    assert apply_show_without_activating(widget) is True
    assert _show_without_activating(widget) is True


def test_platform_detection_follows_sys_platform():
    assert window_activation._is_linux() == sys.platform.startswith("linux")


# ------------------------------------------------- 真实窗口：规则确实生效

@pytest.fixture
def as_linux(monkeypatch):
    monkeypatch.setattr(window_activation, "_is_linux", lambda: True)


@pytest.fixture
def as_macos(monkeypatch):
    monkeypatch.setattr(window_activation, "_is_linux", lambda: False)


def test_dynamic_island_follows_platform_rule(tmp_path: Path, as_linux):
    from pet.config import Config
    from pet.dynamic_island import DynamicIsland

    _qapp()
    island = DynamicIsland(Config(base=tmp_path))

    assert island.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert _show_without_activating(island) is False


def test_dynamic_island_keeps_attribute_off_linux(tmp_path: Path, as_macos):
    from pet.config import Config
    from pet.dynamic_island import DynamicIsland

    _qapp()
    island = DynamicIsland(Config(base=tmp_path))

    assert _show_without_activating(island) is True


def test_desktop_notification_follows_platform_rule(as_linux):
    from pet.desktop_notify import DesktopNotification

    _qapp()
    note = DesktopNotification("标题", "内容")

    assert note.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert _show_without_activating(note) is False


def test_speech_bubble_follows_platform_rule(as_linux):
    from pet.speech_bubble import PetSpeechBubble

    _qapp()
    bubble = PetSpeechBubble()

    assert bubble.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert _show_without_activating(bubble) is False


def test_speech_bubble_keeps_attribute_off_linux(as_macos):
    from pet.speech_bubble import PetSpeechBubble

    _qapp()
    bubble = PetSpeechBubble()

    assert _show_without_activating(bubble) is True
