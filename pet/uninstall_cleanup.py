# -*- coding: utf-8 -*-
"""卸载清理：删除自启项，并移除本桌宠写进各 Agent 的外部配置。

供 `--uninstall-cleanup` 参数使用——安装包（Inno Setup）卸载时调用。
该路径不启动 QApplication/事件循环，只做必要的清理，便于「无 Qt 依赖路径」
（仍可 import Qt 模块，但不建窗口）下执行。

清理哪些 Agent 由声明式注册表（pet/agents/registry.py）决定：凡是声明了
卸载入口与结果键的内置 Agent 都会被清理（当前为 Claude / DSH / Kimi / ZCode）。
多实例占用判断由 agent_link 模块统一提供（语义并集：当前变体
目录 + <base> 下全部变体任一认为在用即保留），本模块只负责消费。

**移除一个内置 Agent 时注意**：本模块按「当前注册表」清理。若某 Agent 曾随
已发布版本装过 hooks，直接删掉它的注册条目会让升级用户的宿主配置残留——
必须按 docs/AGENT-INTEGRATION-REGISTRY-2026-09-22.md §6 的清单保留一次性清理分支。
"""

from __future__ import annotations

import logging

from .agent_link import other_instances_use_agent
from .agents.registry import uninstall_callable, uninstallable_specs

log = logging.getLogger("dsh-pet-standalone")


def run_uninstall_cleanup(config=None) -> dict:
    """执行卸载清理各步骤，返回结果字典（供测试与日志）。

    步骤：
    1. 删除当前变体开机自启项（autostart.disable）；
    2. 逐个内置 Agent：若无其他实例在使用其联动，移除本桌宠注入的外部配置。
    """
    from . import autostart

    if config is None:
        from .config import Config
        config = Config()

    results = {"autostart": bool(autostart.disable())}

    # 其他实例仍在使用对应联动则保留（hooks/桥接插件是全局状态）
    for spec in uninstallable_specs():
        if other_instances_use_agent(config, spec.key):
            results[spec.uninstall_result_key] = "skipped"
            continue
        uninstaller = uninstall_callable(spec)
        try:
            results[spec.uninstall_result_key] = bool(uninstaller()) if uninstaller is not None else True
        except Exception as exc:  # 清理失败只记录，不能让卸载流程中断
            log.warning("%s 联动卸载清理异常: %s", spec.name, exc)
            results[spec.uninstall_result_key] = False

    return results
