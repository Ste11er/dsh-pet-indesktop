# -*- coding: utf-8 -*-
"""Agent 接入实现包。

本包承载「一个 Agent 怎么把事件送进桌宠」的全部实现细节：
声明式注册表（registry）、通用 hook 事件写入器（hook_writer）、
各 Agent 的监视器与外部配置注入器（kimi / zcode），
以及既有内置监视器（DSH / Claude / Cursor / OpenCode）的装配适配（adapters）。

注意：本包**不**在 `__init__` 里 re-export 任何子模块，避免
`pet.config` → `pet.agents.registry` → 监视器实现 → `pet.agent_link`
的导入环。需要什么就显式 import 对应子模块。
"""
