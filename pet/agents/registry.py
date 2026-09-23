# -*- coding: utf-8 -*-
"""声明式 Agent 接入注册表（单一事实来源）。

一个 Agent 的全部接入事实集中在这里声明，`pet/config.py`（开关默认值与清洗
白名单）、`pet/agent_link.py`（监视器装配 / 显示名 / 启停编排 / 缺失提示）、
`pet/context_menus/shared.py`（右键菜单）、`pet/uninstall_cleanup.py`（卸载
清理）都从这里取值，不再各自硬编码名单——新增一个内置 Agent 只需在本文件加
一条 `AgentSpec` 并实现对应的监视器与注入器。

本模块只放数据与惰性解析工具：**不 import Qt、不 import 监视器实现**，因此可
以被 `pet/config.py` 在导入链最早期安全引用（不产生循环依赖）。监视器工厂与
安装/卸载入口用点分引用（`"模块:属性路径"`）声明，运行时才解析。
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Callable

# 接入方式（仅作声明与 UI/文档用途，运行时不分支）
INTEGRATION_PLUGIN = "plugin"   # 往宿主装一个插件（DSH）
INTEGRATION_HOOK = "hook"       # 往宿主配置注入 hooks/事件命令
INTEGRATION_TAIL = "tail"       # 只读 tail 宿主自己的转写/日志文件
INTEGRATION_DB = "db"           # 只读宿主本地数据库

# 安装耗时形态：sync = 同步装（毫秒级，UI 线程可接受）；
# background = 后台线程装（可能数十秒，如 pnpm 解析）
INSTALL_SYNC = "sync"
INSTALL_BACKGROUND = "background"


@dataclass(frozen=True)
class AgentSpec:
    """一个内置 Agent 的接入声明。"""

    key: str
    name: str
    integration: str
    # 监视器工厂："模块:属性" —— 调用约定 factory(config_dir, parent) -> BaseAgentMonitor
    monitor: str
    order: int
    #: 右键菜单显示名（留空则用 name）。气泡/台词用 name，菜单可用更完整的品牌名。
    menu_label: str = ""
    # 安装/卸载入口："模块:属性" —— install(events_file) / uninstall()；
    # 返回值 bool 或 (bool, str) 均可（字符串为失败原因）。
    install: str = ""
    uninstall: str = ""
    # --uninstall-cleanup 结果字典里的键（空 = 不参与卸载清理）
    uninstall_result_key: str = ""
    # 是否写外部 Agent 配置（写配置必须先弹窗授权）
    needs_consent: bool = False
    consent_title: str = ""
    consent_text: str = ""
    install_mode: str = INSTALL_SYNC
    # 本机安装探测点（相对家目录）：都不存在时提示「勾了但没装」
    detect_paths: tuple[str, ...] = ()
    # 已注入的 hooks 事件名（文档/UI 展示用，安装器自己也是按它写配置）
    hook_events: tuple[str, ...] = ()

    @property
    def writes_external_config(self) -> bool:
        return bool(self.install)


# 统一协议事件名 → 桌宠六态词汇的补充映射（协议见 docs/AGENT_LINK_PROTOCOL.md §2.4）。
# 基表在 pet/agent_link.py 的 DEFAULT_EVENT_STATE_MAP；这里补齐 hook 类 Agent
# 会用到的、桌宠本身没有的宿主事件（clawd 的 juggling/sweeping/notification 等
# 状态在本项目里统一收敛到六态）。
EXTRA_EVENT_STATES: dict[str, str] = {
    # 需要用户注意（权限确认 / 通知）：与 Claude 的 Stop 同档
    "Notification": "attention",
    "PermissionRequest": "attention",
    # 权限确认结束、子代理开工、上下文压缩：都仍在干活
    "PermissionResult": "working",
    "SubagentStart": "working",
    "PreCompact": "working",
    # 压缩完成 / 子代理收工：回合可能收尾，交给完成确认流程
    "PostCompact": "attention",
    # 用户中断（Kimi Code）：立刻回待机
    "Interrupt": "idle",
}


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="dsh",
        name="DSH",
        integration=INTEGRATION_PLUGIN,
        monitor="pet.agents.adapters:make_dsh",
        order=10,
        menu_label="DeepSeek Harness (DSH)",
        install="pet.agents.adapters:install_dsh_bridge",
        uninstall="pet.agents.adapters:uninstall_dsh_bridge",
        uninstall_result_key="dsh_bridge",
        needs_consent=True,
        consent_title="开启 DSH 联动",
        consent_text=(
            "开启联动需要向 DeepSeek Harness 安装一个桥接小插件\n"
            "（把 DSH 的运行状态写到本地文件给桌宠读，仅本地、无网络）。\n\n"
            "是否允许一键安装？（关闭联动时会自动卸载）"
        ),
        install_mode=INSTALL_BACKGROUND,
    ),
    AgentSpec(
        key="claude",
        name="Claude Code",
        integration=INTEGRATION_HOOK,
        monitor="pet.agents.adapters:make_claude",
        order=20,
        install="pet.agent_link:ClaudeCodeMonitor.install_hooks",
        uninstall="pet.agent_link:ClaudeCodeMonitor.uninstall_hooks",
        uninstall_result_key="claude_hooks",
        needs_consent=True,
        consent_title="开启 Claude Code 联动",
        consent_text=(
            "开启联动需要在 ~/.claude/settings.json 中配置事件 hooks，\n"
            "用于在 Agent 干活时同步通知桌宠播放对应动作。\n\n"
            "是否允许注入 hooks 配置？（关闭联动时会自动移除）"
        ),
        detect_paths=(".claude",),
        hook_events=(
            "PreToolUse", "PostToolUse", "PostToolUseFailure",
            "Stop", "SessionStart", "UserPromptSubmit",
        ),
    ),
    AgentSpec(
        key="cursor",
        name="Cursor",
        integration=INTEGRATION_TAIL,
        monitor="pet.agents.adapters:make_cursor",
        order=30,
        detect_paths=(".cursor",),
    ),
    AgentSpec(
        key="opencode",
        name="OpenCode",
        integration=INTEGRATION_DB,
        monitor="pet.agents.adapters:make_opencode",
        order=40,
        detect_paths=(".local/share/opencode",),
    ),
    AgentSpec(
        key="kimi",
        name="Kimi Code",
        integration=INTEGRATION_HOOK,
        monitor="pet.agent_link:KimiMonitor",
        order=50,
        install="pet.agents.kimi:install_hooks",
        uninstall="pet.agents.kimi:uninstall_hooks",
        uninstall_result_key="kimi_hooks",
        needs_consent=True,
        consent_title="开启 Kimi 联动",
        consent_text=(
            "开启联动需要在 Kimi 配置（~/.kimi-code/config.toml，\n"
            "旧版为 ~/.kimi/config.toml）中追加 [[hooks]] 条目，\n"
            "用于在 Kimi 干活时同步通知桌宠播放对应动作。\n\n"
            "是否允许写入 hooks 配置？（关闭联动时会自动移除）"
        ),
        detect_paths=(".kimi-code", ".kimi"),
        hook_events=(
            "SessionStart", "SessionEnd", "UserPromptSubmit",
            "PreToolUse", "PostToolUse", "PostToolUseFailure",
            "Stop", "StopFailure", "SubagentStart", "SubagentStop",
            "PreCompact", "PostCompact", "Notification", "Interrupt",
        ),
    ),
    AgentSpec(
        key="zcode",
        name="ZCode",
        integration=INTEGRATION_HOOK,
        monitor="pet.agent_link:ZCodeMonitor",
        order=60,
        install="pet.agents.zcode:install_hooks",
        uninstall="pet.agents.zcode:uninstall_hooks",
        uninstall_result_key="zcode_hooks",
        needs_consent=True,
        consent_title="开启 ZCode 联动",
        consent_text=(
            "开启联动需要在 ~/.zcode/cli/config.json 的 hooks.events 下\n"
            "注册事件条目（并打开 hooks.enabled），\n"
            "用于在 ZCode 干活时同步通知桌宠播放对应动作。\n\n"
            "是否允许写入 hooks 配置？（关闭联动时会自动移除）"
        ),
        detect_paths=(".zcode",),
        hook_events=(
            "SessionStart", "UserPromptSubmit", "PreToolUse",
            "PostToolUse", "PostToolUseFailure", "Stop",
        ),
    ),
)

_SPEC_BY_KEY: dict[str, AgentSpec] = {spec.key: spec for spec in AGENT_SPECS}


def agent_specs() -> tuple[AgentSpec, ...]:
    """全部内置 Agent（按菜单/展示顺序）。"""
    return AGENT_SPECS


def agent_spec(key: str) -> AgentSpec | None:
    """按键取内置 Agent 声明；自定义联动 Agent 返回 None。"""
    return _SPEC_BY_KEY.get(str(key or ""))


def builtin_agent_keys() -> tuple[str, ...]:
    """内置 Agent 键（配置里每个键就是一个联动开关）。"""
    return tuple(spec.key for spec in AGENT_SPECS)


def agent_display_names() -> dict[str, str]:
    """键 → 显示名（气泡/菜单/设置页共用）。"""
    return {spec.key: spec.name for spec in AGENT_SPECS}


def extra_event_states() -> dict[str, str]:
    """补充事件映射（合并进 agent_link.DEFAULT_EVENT_STATE_MAP）。"""
    return dict(EXTRA_EVENT_STATES)


def uninstallable_specs() -> tuple[AgentSpec, ...]:
    """参与 --uninstall-cleanup 的 Agent（写外部配置的那些）。"""
    return tuple(
        spec for spec in AGENT_SPECS
        if spec.uninstall and spec.uninstall_result_key
    )


def resolve_ref(ref: str) -> Any:
    """解析 `"模块:属性路径"` 点分引用（如 `"pet.agent_link:ZCodeMonitor"`）。

    每次调用都重新 getattr，因此测试里对类属性做的 monkeypatch 依然生效。
    """
    module_name, _, attr_path = str(ref or "").partition(":")
    if not module_name or not attr_path:
        raise ValueError(f"非法引用: {ref!r}")
    target: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        target = getattr(target, part)
    return target


def install_callable(spec: AgentSpec) -> Callable[..., Any] | None:
    return resolve_ref(spec.install) if spec.install else None


def uninstall_callable(spec: AgentSpec) -> Callable[..., Any] | None:
    return resolve_ref(spec.uninstall) if spec.uninstall else None


def monitor_factory(spec: AgentSpec) -> Callable[..., Any]:
    return resolve_ref(spec.monitor)


# Agent key 词法（内置与自定义共用）：小写字母/数字开头，允许 - 与 _，最长 32。
# 自定义通道清洗（pet/config.py::_clean_custom_agents）与通用 hook 命令生成器
# （pet/agents/hook_writer.py）都按它校验，避免两处规则漂移。
AGENT_KEY_PATTERN = r"[a-z0-9][a-z0-9_-]{0,31}"


def is_valid_agent_key(key: object) -> bool:
    return bool(re.fullmatch(AGENT_KEY_PATTERN, str(key or "")))
