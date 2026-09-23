# -*- coding: utf-8 -*-
"""既有内置监视器（DSH / Claude / Cursor / OpenCode）的注册表适配。

这四个监视器实现留在 `pet.agent_link`（其构造签名各不相同、且被既有测试与
`--uninstall-cleanup` 按类名引用），注册表通过本模块的薄工厂与安装适配把它们
接进统一声明。DSH 的安装入口是「无参 → (ok, msg)」，这里适配成统一契约
`install(events_file)`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def make_dsh(config_dir: Path, parent: Any = None):
    from ..agent_link import DshMonitor

    return DshMonitor("dsh", config_dir, parent)


def make_claude(config_dir: Path, parent: Any = None):
    from ..agent_link import ClaudeCodeMonitor

    return ClaudeCodeMonitor("claude", config_dir, parent)


def make_cursor(config_dir: Path, parent: Any = None):
    from ..agent_link import CursorMonitor

    return CursorMonitor(config_dir, parent)


def make_opencode(config_dir: Path, parent: Any = None):
    from ..agent_link import OpenCodeMonitor

    return OpenCodeMonitor(config_dir, parent)


def install_dsh_bridge(events_file: Path | None = None) -> tuple[bool, str]:
    """DSH 桥接插件安装（忽略 events_file：插件自己写共享桥目录）。"""
    from ..agent_link import DshMonitor

    return DshMonitor.install_bridge()


def uninstall_dsh_bridge(events_file: Path | None = None) -> bool:
    from ..agent_link import DshMonitor

    return DshMonitor.uninstall_bridge()
