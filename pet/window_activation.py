# -*- coding: utf-8 -*-
"""不抢焦点地显示窗口：平台差异收口。

**为什么需要收口**：`WA_ShowWithoutActivating` 在 Linux/X11 上会带来一个 KWin 陷阱——
带该属性的顶层窗口会被打上 `_NET_WM_STATE_DEMANDS_ATTENTION`（"要求关注"），而本应用
这类窗口同时带 `Qt::WindowDoesNotAcceptFocus`（`WM_HINTS` 的 input hint = False），
窗口管理器永远不会真正激活它，**这个标记因此永不自动清除**：KDE 任务栏条目持续
高亮/脉冲，任务栏被反复唤醒（用户口径："窗口一直高亮、一直唤醒任务栏"）。

**规则**（实机二分验证，见 `docs/KDE-TASKBAR-DEMANDS-ATTENTION-2026-09-22.md`）：

- Linux 上，已经带 `WindowDoesNotAcceptFocus` 的窗口**不要再设** `WA_ShowWithoutActivating`。
  该属性在这里是冗余的——WM 本来就不会给这种窗口键盘焦点（实测去掉后不抢焦点，
  且 hide/show 之后也不再被打标记）。
- 其余情况（非 Linux，或窗口**没有** `WindowDoesNotAcceptFocus`）保持原行为：
  该属性是 Windows/macOS 上"显示但不成为前台/key window"的手段，不能省。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget


def _is_linux() -> bool:
    """平台判定（独立成函数，测试可替换）。"""
    return sys.platform.startswith("linux")


def _does_not_accept_focus(widget: QWidget) -> bool:
    """窗口是否已声明不接受焦点（`WM_HINTS` input hint = False）。"""
    return bool(widget.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)


def apply_show_without_activating(widget: QWidget) -> bool:
    """让 `widget` 显示时不抢键盘焦点；返回是否真的设置了 `WA_ShowWithoutActivating`。

    调用时机：必须在 `setWindowFlags()` **之后**调用——本函数要读窗口 flags 判断
    `WindowDoesNotAcceptFocus` 是否已经在位。
    """
    if _is_linux() and _does_not_accept_focus(widget):
        # 见模块 docstring：Linux 上设了它反而让任务栏条目被永久标记为"要求关注"。
        return False
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    return True
