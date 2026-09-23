# -*- coding: utf-8 -*-
"""ZCode 联动：`hooks.events.*` 注入（纯逻辑，无 Qt）。

ZCode（智谱桌面 ADE）读 `~/.zcode/cli/config.json`，hooks 结构与 Claude 的
**不同**：

- 嵌在 `hooks.events.<EventName>` 下（不是 `hooks.<EventName>`）；
- 必须 `hooks.enabled: true` 才生效；用户显式写 false 时尊重用户选择；
- 条目形状严格：容器只允许 `matcher` / `hooks` 两个键，内层 hook 只允许
  `type` / `command` / `args` / `timeoutMs`（多一个键会让整份 config.json 加载失败）；
- 只支持 7 个事件（无 SessionEnd / Notification）。本项目注册 6 个状态类事件；
  `PermissionRequest` 是阻塞式审批通道，本项目没有回写能力，不注册。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .hook_writer import ensure_event_hook

log = logging.getLogger(__name__)

AGENT_KEY = "zcode"
HOOK_MARKER = "zcode_event_hook"
HOOK_TIMEOUT_MS = 8000

HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse",
    "PostToolUse", "PostToolUseFailure", "Stop",
)


def zcode_home() -> Path:
    return Path.home() / ".zcode"


def config_path() -> Path:
    return zcode_home() / "cli" / "config.json"


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _is_our_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    if HOOK_MARKER in str(hook.get("command") or ""):
        return True
    args = hook.get("args")
    return isinstance(args, list) and any(HOOK_MARKER in str(arg) for arg in args)


def _is_our_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(_is_our_hook(hook) for hook in hooks)


def install_hooks(events_file: Path) -> tuple[bool, str]:
    """注入 ZCode hooks.events 条目（幂等；只动带本桌宠脚本标记的条目）。"""
    try:
        script = ensure_event_hook(Path(events_file).parent, AGENT_KEY)
    except OSError as exc:
        return False, f"落地 hook 脚本失败: {exc}"

    path = config_path()
    settings: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return False, f"config.json 解析失败（未改动原文件）: {exc}"
        if not isinstance(loaded, dict):
            return False, "config.json 根节点不是对象（未改动原文件）"
        settings = loaded

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    if hooks.get("enabled") is False:
        return False, "config.json 里 hooks.enabled = false（用户已显式关闭 ZCode hooks）"
    hooks["enabled"] = True
    events = hooks.get("events")
    if not isinstance(events, dict):
        events = {}
        hooks["events"] = events

    for event in HOOK_EVENTS:
        existing = events.get(event)
        kept = [g for g in existing if not _is_our_entry(g)] if isinstance(existing, list) else []
        kept.append({
            "hooks": [{
                "type": "process",
                "command": script.executable,
                "args": script.args_for(event),
                "timeoutMs": HOOK_TIMEOUT_MS,
            }],
        })
        events[event] = kept

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        return False, f"写入 config.json 失败: {exc}"
    return True, ""


def uninstall_hooks() -> bool:
    """移除本桌宠注入的条目（用户自有条目与 hooks.enabled 原样保留）。"""
    path = config_path()
    if not path.is_file():
        return True
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("ZCode config.json 解析失败，跳过卸载: %s", exc)
        return False
    if not isinstance(settings, dict):
        return True
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return True
    events = hooks.get("events")
    if not isinstance(events, dict):
        return True
    for event in list(events.keys()):
        entries = events.get(event)
        if not isinstance(entries, list):
            continue
        kept = [g for g in entries if not _is_our_entry(g)]
        if kept:
            events[event] = kept
        else:
            del events[event]
    try:
        _write_text_atomic(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("ZCode config.json 写回失败: %s", exc)
        return False
    return True
