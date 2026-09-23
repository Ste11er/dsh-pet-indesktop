# -*- coding: utf-8 -*-
"""Kimi 联动：`[[hooks]]` 注入（纯逻辑，无 Qt）。

两代 Kimi 都是同一套 TOML 形状，但目标文件与事件集不同：

- Kimi Code（TS，现行）：`$KIMI_CODE_HOME|~/.kimi-code/config.toml`，
  16 个事件（含 `Interrupt`），schema 是 **strict**——多一个未知键、事件名不认识
  或 timeout 越界，运行时会**整段丢弃 hooks**（连用户自己写的也一起丢）。因此
  写盘前必须自校验。
- 旧版 Kimi CLI（Python）：`~/.kimi/config.toml`，13 个事件，schema 宽松。

只注入**已存在的配置目录**（没装的那一代不碰）。桌宠只写状态类事件；
`PermissionRequest` / `PermissionResult` 是阻塞式审批通道，本项目没有审批回写
能力，不注册。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .hook_writer import ensure_event_hook

log = logging.getLogger(__name__)

AGENT_KEY = "kimi"
HOOK_MARKER = "kimi_event_hook"
HOOK_TIMEOUT_S = 30
TIMEOUT_MIN = 1
TIMEOUT_MAX = 600

LEGACY_EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "Stop", "StopFailure", "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact", "Notification",
)
CODE_EVENTS = (*LEGACY_EVENTS, "Interrupt")

_ALLOWED_KEYS = ("event", "matcher", "command", "timeout")
_HOOKS_HEADER_RE = re.compile(r"^\s*\[\[hooks\]\]\s*(?:#.*)?$")
_ANY_TABLE_RE = re.compile(r"^\s*\[")
_EMPTY_HOOKS_RE = re.compile(r"^\s*hooks\s*=\s*\[\s*\]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class KimiTarget:
    config_path: Path
    events: tuple[str, ...]
    strict: bool
    #: 配置目录是否存在（不存在 = 这一代没装，不注入）
    present: bool


def kimi_code_home() -> Path:
    env = str(os.environ.get("KIMI_CODE_HOME") or "").strip()
    return Path(env).expanduser() if env else (Path.home() / ".kimi-code")


def legacy_home() -> Path:
    return Path.home() / ".kimi"


def kimi_targets() -> list[KimiTarget]:
    """两代 Kimi 的注入目标（不论是否安装，`present` 标明配置目录是否存在）。"""
    code_home = kimi_code_home()
    legacy = legacy_home()
    return [
        KimiTarget(code_home / "config.toml", CODE_EVENTS, True, code_home.is_dir()),
        KimiTarget(legacy / "config.toml", LEGACY_EVENTS, False, legacy.is_dir()),
    ]


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _toml_basic_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_blocks(events: tuple[str, ...], command_literal: str) -> str:
    return "\n".join(
        f'[[hooks]]\nevent = "{event}"\ncommand = {command_literal}\n'
        f'matcher = ""\ntimeout = {HOOK_TIMEOUT_S}'
        for event in events
    )


def validate_blocks(blocks_text: str, events: tuple[str, ...]) -> None:
    """Kimi Code strict schema 自校验：不合格就抛错，绝不写坏用户配置。"""
    allowed = set(events)
    chunks = re.split(r"(?m)^\[\[hooks\]\]\s*$", blocks_text)[1:]
    if len(chunks) != len(events):
        raise ValueError(f"kimi hook 校验：生成的 block 数 {len(chunks)} != 事件数 {len(events)}")
    for chunk in chunks:
        keys: dict[str, str] = {}
        for line in chunk.strip().splitlines():
            key, _, value = line.partition("=")
            keys[key.strip()] = value.strip()
        illegal = set(keys) - set(_ALLOWED_KEYS)
        if illegal:
            raise ValueError(f"kimi hook 校验：非法键 {sorted(illegal)}（strict schema 会丢弃整段 hooks）")
        missing = set(_ALLOWED_KEYS) - set(keys)
        if missing:
            raise ValueError(f"kimi hook 校验：缺少键 {sorted(missing)}")
        event_value = keys["event"].strip('"')
        if event_value not in allowed:
            raise ValueError(f"kimi hook 校验：未知事件 {event_value!r}")
        timeout_value = int(keys["timeout"])
        if not TIMEOUT_MIN <= timeout_value <= TIMEOUT_MAX:
            raise ValueError(f"kimi hook 校验：timeout 越界 {timeout_value}")
        command_value = keys["command"]
        if not re.fullmatch(r'"(?:\\.|[^"\\])*"', command_value):
            raise ValueError("kimi hook 校验：command 不是单行 TOML 字符串")


def strip_our_blocks(text: str) -> tuple[str, int]:
    """删掉本桌宠注入的 `[[hooks]]` block（用户自己的 block 原样保留）。"""
    lines = text.splitlines()
    kept: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        if _HOOKS_HEADER_RE.match(lines[i]):
            j = i + 1
            while j < len(lines) and not _ANY_TABLE_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if any(HOOK_MARKER in line for line in block):
                removed += 1
                if j < len(lines) and not lines[j].strip():
                    j += 1  # 顺带吞掉块后的空行，避免卸载后留一堆空行
                i = j
                continue
            kept.extend(block)
            i = j
            continue
        kept.append(lines[i])
        i += 1
    return "\n".join(kept), removed


def install_at(target: KimiTarget, script_command: str) -> tuple[bool, str]:
    """在单个目标写 hooks。返回 (是否成功, 失败原因)。"""
    path = target.config_path
    try:
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError as exc:
        return False, f"读取 {path} 失败: {exc}"

    stripped, _ = strip_our_blocks(text)
    stripped = _EMPTY_HOOKS_RE.sub("", stripped)  # 空 hooks = [] 会让 [[hooks]] 解析冲突
    blocks = build_blocks(target.events, _toml_basic_string(script_command))
    if target.strict:
        try:
            validate_blocks(blocks, target.events)
        except ValueError as exc:
            return False, str(exc)

    body = stripped.strip()
    new_text = (body + "\n\n" if body else "") + blocks + "\n"
    if new_text == text:
        return True, ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(path, new_text)
    except OSError as exc:
        return False, f"写入 {path} 失败: {exc}"
    return True, ""


def install_hooks(events_file: Path) -> tuple[bool, str]:
    """给已安装的 Kimi 代际注入 hooks；一代都没装则如实报失败。"""
    try:
        script = ensure_event_hook(Path(events_file).parent, AGENT_KEY)
    except OSError as exc:
        return False, f"落地 hook 脚本失败: {exc}"

    present = [t for t in kimi_targets() if t.present]
    if not present:
        return False, "未检测到 Kimi 配置目录（~/.kimi-code 或 ~/.kimi）"

    failures: list[str] = []
    for target in present:
        ok, detail = install_at(target, script.command)
        if not ok:
            failures.append(detail)
    if failures:
        return False, "；".join(failures)
    return True, ""


def uninstall_hooks() -> bool:
    """从所有 Kimi 配置里移除本桌宠注入的 block。"""
    ok = True
    for target in kimi_targets():
        path = target.config_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            log.warning("Kimi 配置读取失败，跳过卸载 %s: %s", path, exc)
            ok = False
            continue
        stripped, removed = strip_our_blocks(text)
        if not removed:
            continue
        try:
            _write_text_atomic(path, stripped.rstrip() + "\n" if stripped.strip() else "")
        except OSError as exc:
            log.warning("Kimi 配置写回失败 %s: %s", path, exc)
            ok = False
    return ok
