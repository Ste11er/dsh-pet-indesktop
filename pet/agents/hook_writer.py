# -*- coding: utf-8 -*-
"""通用 hook 事件写入器：把宿主 hook 收到的事件落进统一协议 JSONL。

桌宠往 `<config.dir>/agent-events/` 落地一个**按 Agent 参数化**的写入脚本
（POSIX 用 Python、Windows 用 PowerShell），宿主的 hook 配置只引用它。脚本：

1. 事件名优先取 argv[1]（ZCode 这类按参数传事件的宿主），否则取 stdin JSON 的
   `hook_event_name` / `event`（Kimi / Claude 这类按 stdin 传的宿主）；
2. 工具名取 stdin JSON 的 `tool_name` / `tool`（没有就不写该字段）；
3. 追加一行 `{"ts", "agent", "event", "tool"?}` 到 `<agent>.jsonl`；
4. 任何异常都静默吞掉并 exit 0——联动是锦上添花，绝不阻塞宿主
   （协议写方红线见 docs/AGENT_LINK_PROTOCOL.md §2.5）。

隐私：只写状态/事件名/工具名，绝不写命令全文、文件内容或代码。

本模块同时提供**通用接入助手**（`python -m pet.agents.hook_writer --help`）：
给没有内置适配器的宿主生成同一条命令，配合 `agent_link.custom_agents`
零代码接入（见 docs/AGENT_LINK_PROTOCOL.md §4）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# stdin 读取上限：宿主正常会立刻关闭 stdin；给一个宽裕但有限的上限，
# 避免极端情况下（stdin 一直开着）挂住宿主的 hook 进程。
STDIN_READ_TIMEOUT_S = 0.3


@dataclass(frozen=True)
class HookScript:
    """已落地的 hook 写入脚本及其三种引用形态。"""

    path: Path
    #: 完整 shell 命令（JSON/TOML 里以字符串保存的宿主配置用）
    command: str
    #: 进程可执行文件（ZCode 这类 command + args 分离的宿主配置用）
    executable: str
    #: 事件名之前的固定参数（含脚本路径）
    base_args: tuple[str, ...]

    def args_for(self, event: str) -> list[str]:
        """command + args 分离形态的完整 args（末尾追加事件名）。"""
        return [*self.base_args, str(event)]


def events_file_for(events_dir: Path, agent_key: str) -> Path:
    return Path(events_dir) / f"{agent_key}.jsonl"


def _posix_interpreter() -> str:
    # 打包（frozen）后 sys.executable 是桌宠自身，不能拿来跑脚本，
    # 退化为 python3（与 Claude hooks 既有口径一致）。
    return "python3" if getattr(sys, "frozen", False) else (sys.executable or "python3")


_POSIX_TEMPLATE = '''# -*- coding: utf-8 -*-
"""dsh-pet 统一协议事件写入器（桌宠自动生成，请勿手改）。

宿主 hook 把事件名（argv[1] 或 stdin JSON 的 hook_event_name）与工具名写进
同目录的 %(agent)s.jsonl。本脚本不联网、不阻塞宿主、失败静默退出 0。
协议见 docs/AGENT_LINK_PROTOCOL.md §2。
"""
import json
import select
import sys
import time
from pathlib import Path

AGENT = %(agent)r
OUT = Path(__file__).with_name(%(out)r)


def _read_stdin(timeout):
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    tool = ""
    raw = _read_stdin(%(timeout)r)
    if raw.strip():
        try:
            payload = json.loads(raw)
            if not event:
                event = str(payload.get("hook_event_name") or payload.get("event") or "")
            tool = str(payload.get("tool_name") or payload.get("tool") or "")
        except Exception:
            pass
    if not event:
        return
    record = {"ts": time.time(), "agent": AGENT, "event": event}
    if tool:
        record["tool"] = tool
    with OUT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\\n")


try:
    main()
except Exception:
    pass
'''

_WINDOWS_TEMPLATE = """# dsh-pet 统一协议事件写入器（桌宠自动生成，请勿手改）
# 宿主 hook 把事件名（argv 参数或 stdin JSON 的 hook_event_name）与工具名写进
# 同目录的 %(agent)s.jsonl。不联网、不阻塞宿主、失败静默退出 0。
param([string]$EventName = '')
$ErrorActionPreference = 'SilentlyContinue'
$tool = ''
if ([Console]::IsInputRedirected) {
  try {
    $raw = [Console]::In.ReadToEnd()
    if ($raw) {
      $payload = $raw | ConvertFrom-Json
      if (-not $EventName) { $EventName = [string]$payload.hook_event_name }
      if (-not $EventName) { $EventName = [string]$payload.event }
      if ($payload.tool_name) { $tool = [string]$payload.tool_name }
      elseif ($payload.tool) { $tool = [string]$payload.tool }
    }
  } catch {}
}
if (-not $EventName) { exit 0 }
$record = [ordered]@{ ts = [DateTimeOffset]::Now.ToUnixTimeMilliseconds() / 1000.0; agent = '%(agent)s'; event = $EventName }
if ($tool) { $record['tool'] = $tool }
try {
  Add-Content -Path (Join-Path $PSScriptRoot '%(out)s') -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
} catch {}
exit 0
"""


def ensure_event_hook(events_dir: Path, agent_key: str, out_name: str | None = None) -> HookScript:
    """把统一协议写入脚本落地到 events_dir，返回其引用形态。

    `out_name` 为事件文件名（默认 `<agent_key>.jsonl`）；自定义联动的
    `agent_link.custom_agents[].path` 可以是任意文件名，所以生成器会显式传入。

    脚本整体归桌宠所有：每次启动/安装都覆盖重写，升级版本自动生效。
    """
    events_dir = Path(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    out_name = out_name or f"{agent_key}.jsonl"

    if sys.platform == "win32":
        script = events_dir / f"{agent_key}_event_hook.ps1"
        script.write_text(
            _WINDOWS_TEMPLATE % {"agent": agent_key, "out": out_name},
            encoding="utf-8",
        )
        flags = ("-NoProfile", "-ExecutionPolicy", "Bypass", "-File")
        command = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
        return HookScript(
            path=script,
            command=command,
            executable="powershell",
            base_args=(*flags, str(script)),
        )

    script = events_dir / f"{agent_key}_event_hook.py"
    script.write_text(
        _POSIX_TEMPLATE % {
            "agent": agent_key,
            "out": out_name,
            "timeout": STDIN_READ_TIMEOUT_S,
        },
        encoding="utf-8",
    )
    executable = _posix_interpreter()
    return HookScript(
        path=script,
        command=f'"{executable}" "{script}"',
        executable=executable,
        base_args=(str(script),),
    )


# ---------------------------------------------------------------------------
# 通用接入助手：给「没有内置适配器」的宿主生成 hook 命令（自定义联动通道）
# ---------------------------------------------------------------------------

def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m pet.agents.hook_writer",
        description=(
            "为任意 Agent 宿主生成统一协议 hook 命令：把它粘进宿主的 hook/事件配置，"
            "宿主干活时桌宠就能感知（配合 config.json 的 agent_link.custom_agents）。"
        ),
        epilog=(
            "示例：python -m pet.agents.hook_writer --agent mycli --out ~/.mycli/pet-events.jsonl\n"
            "宿主按参数传事件名时再加 --event <事件名>（每次事件一条配置）。"
        ),
    )
    parser.add_argument("--agent", required=True, help="自定义联动 key（与 custom_agents[].key 一致）")
    parser.add_argument("--out", required=True, help="事件文件路径（与 custom_agents[].path 一致）")
    parser.add_argument("--event", default="", help="把事件名作为参数拼进命令（宿主用 argv 传事件时使用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（command/executable/args/script）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：生成并打印 hook 命令。纯逻辑、不依赖 Qt，可脱离桌宠直接跑。"""
    from .registry import builtin_agent_keys, is_valid_agent_key

    args = _build_parser().parse_args(argv)
    key = str(args.agent).strip().lower()
    if not is_valid_agent_key(key):
        print(f"error: agent key 非法（需匹配 {AGENT_KEY_PATTERN_HINT}）: {args.agent!r}", file=sys.stderr)
        return 2
    if key in builtin_agent_keys():
        print(
            f"error: {key!r} 是内置 Agent，直接右键菜单开启即可（无需自定义通道）",
            file=sys.stderr,
        )
        return 2

    out_path = Path(str(args.out)).expanduser()
    script = ensure_event_hook(out_path.parent, key, out_name=out_path.name)
    command = script.command if not args.event else f"{script.command} {args.event}"

    if args.json:
        import json

        print(json.dumps({
            "agent": key,
            "path": str(out_path),
            "script": str(script.path),
            "command": command,
            "executable": script.executable,
            "args": script.args_for(args.event) if args.event else list(script.base_args),
        }, ensure_ascii=False, indent=2))
        return 0

    print("# 1) 事件文件（写进 config.json 的 agent_link.custom_agents）")
    print(f'{{"key": "{key}", "name": "{key}", "path": "{out_path}"}}')
    print()
    print("# 2) 粘进宿主 hook 配置的命令")
    print(command)
    return 0


# 仅用于 CLI 报错提示：与 registry.AGENT_KEY_PATTERN 同源的展示串
AGENT_KEY_PATTERN_HINT = "小写字母/数字开头，允许 - 和 _，最长 32 位"


if __name__ == "__main__":
    raise SystemExit(main())
