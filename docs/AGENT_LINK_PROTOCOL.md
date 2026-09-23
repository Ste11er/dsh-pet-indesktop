# 多 Agent 联动统一事件协议与扩展指南

> 文档用途：说明桌宠「Agent 联动」的底层事件协议，以及第三方如何接入任意 AI Agent（不改桌宠代码，或新增一个内置 Agent 各需要做什么）。
> 读者：想把自家 Agent / CLI 工具接入桌宠联动的开发者与集成方。

---

## 1. 架构总览：本地文件事件总线（不访问外网）

```
Agent 侧（写方）                          桌宠侧（读方）
┌──────────────────────────┐            ┌────────────────────────────────────┐
│ 插件 / hooks / 直读适配    │   追加写    │ ByteOffsetTailer 字节偏移增量 tail   │
│ DSH 桥接插件 (Node)       │  ────────▶ │ （1.5s QTimer，不回放历史）          │
│ Claude Code hooks 脚本    │  JSONL 文件 │   ↓ normalize_event_state 归一      │
│ Cursor/OpenCode 直读适配  │            │   ↓ 六态词汇                        │
└──────────────────────────┘            │ AgentLinkManager 状态机             │
                                        │   → 动画切换 / 气泡 / 音效           │
                                        └────────────────────────────────────┘
```

- **通信方式**：事件主通道是纯本地文件追加写 + 增量读；交互回写另走**本机回环**的 WebSocket/HTTP——DSH 桥接插件订阅 DSH 的 `ws://127.0.0.1:<port>/api/events.mux`，桌宠侧 POST `http://127.0.0.1:<port>/api/respond`。两者都只连 `127.0.0.1`，全程不访问外网。
- **读方保证**：byte-offset tail 不回放历史事件；文件不存在时静默空转；半行缓冲不丢事件；单次读取有界（64KB）。
- **低功耗**：联动默认全关；桌宠隐藏时监视器全线 pause，显示时 resume。

## 2. 统一事件协议（写方契约）

### 2.1 事件文件位置

走统一通道的 Agent，事件文件固定为：

```
<config.dir>/agent-events/<agent>.jsonl
```

`<config.dir>` 按平台（`<变体>` 为 onedir 变体后缀，源码运行无后缀）：

| 平台 | 路径 |
|---|---|
| Windows | `%APPDATA%\dsh-pet-standalone[-变体]\agent-events\` |
| macOS | `~/Library/Application Support/dsh-pet-standalone[-变体]/agent-events/` |
| Linux | `~/.config/dsh-pet-standalone[-变体]/agent-events/` |

### 2.2 JSON 行格式

文件每行一个 JSON 对象（JSON Lines，UTF-8 追加写）：

```jsonc
// 形态一：事件名（推荐，桌宠按内置映射归一为状态）
{"ts": 1750000000.0, "agent": "myagent", "event": "PreToolUse", "tool": "bash"}

// 形态二：显式状态（直接给六态词汇，优先级最高）
{"ts": 1750000000.0, "agent": "myagent", "state": "working"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `ts` | 建议 | 事件时间戳（秒，浮点）。桌宠当前不校验，但写方应带，便于排查 |
| `agent` | 建议 | Agent 标识（与文件名一致即可）；桌宠以监视器的 agent_key 为准 |
| `event` | 二选一 | 事件名，见 §2.4 内置映射；不认识的会被忽略 |
| `state` | 二选一 | 显式状态，必须是 §2.3 六态词汇之一；优先于 `event` |
| `tool` | 可选 | 工具名（小写），用于「过程汇报」气泡（如 `bash` → 「正在跑命令」） |

### 2.3 六态状态词汇（读方统一词汇）

| 状态 | 含义 | 桌宠反应 |
|---|---|---|
| `thinking` | 思考中/刚接受指令 | 联动动作池轮换（写代码/吃Token…），可选「开始干活」气泡 |
| `working` | 执行中 | 同上 |
| `attention` | 需要用户注意（如回合结束待确认） | 重要气泡「需要你看一眼～」或并入完成确认 |
| `error` | 出错 | 重要气泡「好像遇到报错了…」+ error 音效 |
| `idle` | 空闲 | 恢复待机动画；busy→idle 触发「干完活啦」完成通知（默认开） |
| `sleeping` | 休眠 | 同 idle |

### 2.4 内置事件名 → 状态映射

写方若不想直接给 `state`，可使用以下事件名（与 Claude Code hooks 事件名兼容）：

| 事件名 | 状态 |
|---|---|
| `SessionStart` / `SessionEnd` | `idle` |
| `UserPromptSubmit` | `thinking` |
| `PreToolUse` / `PostToolUse` | `working` |
| `PostToolUseFailure` | `error` |
| `Stop` / `SubagentStop` | `attention` |
| `StopFailure` | `error` |
| `Notification` / `PermissionRequest` | `attention` |
| `PermissionResult` / `SubagentStart` / `PreCompact` | `working` |
| `PostCompact` | `attention` |
| `Interrupt` | `idle` |
| `error` / `idle` / `thinking` | 同名状态 |

不在表内的事件名**一律忽略**（绝不默认当成 working，防止 transcript 类密集写入过度触发）。

基表（与宿主无关的通用事件）在 `pet/agent_link.py` 的 `DEFAULT_EVENT_STATE_MAP`；
各 hook 类宿主的专有事件（上表后半段）由注册表
`pet/agents/registry.py` 的 `EXTRA_EVENT_STATES` 补充，新增一个宿主不必改状态机。

### 2.5 写方注意事项（对接清单）

1. **追加写**（append），UTF-8 编码；PowerShell 写入方建议 `-Encoding UTF8`（读方已兼容首行 BOM）。
2. **去重**：连续相同状态不要重复落盘（状态切换瞬间可能抖出重复事件，重复行会占住桌宠端换帧节流位）。
3. **轮转**：事件文件超过约 1MB 时轮转（如 `dsh-{pid}.jsonl` → `dsh-{pid}.jsonl.1`，只留一代）；读方通过文件身份识别自动适配，无需特殊处理。
4. **绝不阻塞宿主**：写事件失败时静默放弃——联动是锦上添花，不能影响 Agent 本体（参考 `integrations/dsh-pet-bridge/index.js` 的做法）。
5. **隐私红线**：只写状态/事件元数据（状态、事件名、工具名），**不要**把代码内容、命令全文、文件内容、屏幕信息写进事件文件。

## 3. 内置 Agent 与接入模式（现状分析）

内置 Agent 的接入事实全部声明在 **`pet/agents/registry.py` 的 `AgentSpec`**：
配置开关默认值、监视器工厂、安装/卸载入口、写外部配置前的授权文案、本机安装
探测点都由注册表派生；配置清洗白名单、右键菜单、`--uninstall-cleanup` 不再各自
维护名单。

| Agent | 模式 | 事件来源 | 是否写外部配置 |
|---|---|---|---|
| DSH | 插件订阅 | 内置桥接插件 `integrations/dsh-pet-bridge/` 订阅 agent 生命周期事件，按实例写桥目录 `<base>/dsh-pet-bridge/dsh-{pid}.jsonl`（消费端 glob `dsh*.jsonl`，兼容旧版单文件 `dsh.jsonl`） | 安装/卸载 dsh 插件（弹窗同意） |
| Claude Code | hooks 注入 | 向 `~/.claude/settings.json` 注入官方 hooks，落地脚本把事件写 `agent-events/claude.jsonl` | 注入/卸载 hooks（弹窗同意） |
| Kimi（Kimi Code / 旧版 Kimi CLI） | hooks 注入 | 向 `~/.kimi-code/config.toml`（旧版 `~/.kimi/config.toml`）追加 `[[hooks]]` 条目，事件写 `agent-events/kimi.jsonl` | 注入/卸载 hooks（弹窗同意） |
| ZCode | hooks 注入 | 向 `~/.zcode/cli/config.json` 的 `hooks.events.*` 注册事件条目并打开 `hooks.enabled`，事件写 `agent-events/zcode.jsonl` | 注入/卸载 hooks（弹窗同意） |
| Cursor | transcript 直读 | 直接 tail `~/.cursor/projects/**/agent-transcripts/*.jsonl`（官方转写文件，按 role/content 解析） | 否 |
| OpenCode | 数据库直读 | 只读 `~/.local/share/opencode/opencode.db` 的 `event` 表（rowid 增量） | 否 |

**审批类事件一律不注册**：`PermissionRequest` / `PermissionResult` 是宿主侧的
阻塞式决策通道，桌宠目前只有 DSH 具备审批回写能力；其余 Agent 注册了只会把宿主
的审批流程拖进不确定状态（宁缺勿错）。等某个宿主接入回写通道后再单独登记。

给新 Agent 选择接入模式的判断依据：

- **Agent 有插件/hook 机制** → 优先「hook 注入 + 统一协议写文件」（模式同 Claude Code），这是侵入最小、协议最稳的方式。走这条路的监视器只需继承 `HookAgentMonitor`（几行），安装/卸载器放进 `pet/agents/<id>.py`（纯逻辑、不 import Qt）。
- **Agent 有现成的本地事件源**（transcript/日志/SQLite）→ 可以像 Cursor/OpenCode 那样写直读适配器（需要改桌宠代码，见 §5）。
- **Agent 什么都没有** → 只要它能执行任意命令（几乎都能），就可以让它周期性地往统一协议文件追加一行 JSON——配合 §4 的自定义通道，零代码接入。

## 4. 自定义 Agent 通道（不改桌宠代码）

桌宠支持在 `config.json` 的 `agent_link` 块里声明任意数量的自定义联动 Agent：桌宠对其事件文件做**只读监听**（统一协议），不写任何外部配置、无需授权弹窗。

```jsonc
// <config.dir>/config.json
{
  "agent_link": {
    "custom_agents": [
      {
        "key": "gemini",
        "name": "Gemini CLI",
        "path": "~/.gemini/pet-events.jsonl"
      }
    ]
  }
}
```

| 字段 | 约束 |
|---|---|
| `key` | `^[a-z0-9][a-z0-9_-]{0,31}$`（唯一出处 `pet/agents/registry.py::AGENT_KEY_PATTERN`），不得与内置键（`dsh`/`claude`/`cursor`/`opencode`/`kimi`/`zcode`，权威名单见 `pet/agents/registry.py`）重复，全局唯一 |
| `name` | 联动气泡/菜单显示名；留空时用 key |
| `path` | 事件文件路径，支持 `~` 展开；文件不必预先存在（不存在时静默等待） |

配置后重启桌宠（或重新加载配置）即生效：右键菜单「Agent 联动」会出现该 Agent 的开关，行为与内置 Agent 一致（六态映射、开始/过程/完成气泡、音效全部可用）。最多 8 个自定义条目。

**不必手写脚本**：`pet/agents/hook_writer.py` 提供通用接入助手，为任意能执行命令的宿主生成
统一协议 hook 命令（脚本自动落地、命令直接粘进宿主配置）：

```bash
python -m pet.agents.hook_writer --agent gemini --out ~/.gemini/pet-events.jsonl
# 宿主按参数传事件名时逐事件生成：--event <事件名>
# 需要程序化处理时：--json
```

这也是**没有内置适配器、或不便内置的宿主**（见
[`AGENT-INTEGRATION-REGISTRY-2026-09-22.md`](AGENT-INTEGRATION-REGISTRY-2026-09-22.md) §4）
的推荐接入路径。注意助手需要源码运行环境（`python -m pet.agents.hook_writer`）；
打包版用户按下面的 printf 示例手动追加，协议完全相同。

**接入示例**（不用助手时，也可让任意 Agent 在干活时直接追加）：

```bash
printf '{"ts": %s, "state": "working"}\n' "$(date +%s)" >> ~/.gemini/pet-events.jsonl   # 开工
printf '{"ts": %s, "event": "PreToolUse", "tool": "bash"}\n' "$(date +%s)" >> ~/.gemini/pet-events.jsonl  # 过程
printf '{"ts": %s, "state": "idle"}\n' "$(date +%s)" >> ~/.gemini/pet-events.jsonl      # 收工
```

## 5. 新增一个内置 Agent（改桌宠代码）

内置 Agent 走**声明式注册表**（2026-09 起）：`pet/agents/registry.py` 的
`AgentSpec` 是唯一事实来源，配置默认值与清洗白名单、监视器装配、显示名、右键
菜单、`--uninstall-cleanup` 全部从注册表派生。新增一个内置 Agent 只需：

| # | 位置 | 改什么 |
|---|---|---|
| 1 | `pet/agents/registry.py` | 加一条 `AgentSpec`：key / 显示名 / 接入方式 / 监视器引用 / 安装与卸载引用 / 授权文案 / 安装耗时形态 / 本机安装探测点 |
| 2 | `pet/agents/<id>.py` | 写安装/卸载器（纯逻辑、不 import Qt）：`install_hooks(events_file) -> (ok, msg)`、`uninstall_hooks() -> bool`。走 hook 通道的直接复用 `pet/agents/hook_writer.py` |
| 3 | `pet/agent_link.py` | hook 通道：继承 `HookAgentMonitor` 写一个几行的监视器类；直读类：覆写 `_poll()`，把解析结果经 `state_changed`/`activity` 发出 |
| 4 | `pet/agents/registry.py` `EXTRA_EVENT_STATES` | 宿主专有事件名 → 六态（通用事件已在 `agent_link.DEFAULT_EVENT_STATE_MAP`） |

`pet/config.py` / 右键菜单 / `pet/uninstall_cleanup.py` **不需要改**——它们都从注册表读。
监视器实现上，走统一协议的只需继承 `HookAgentMonitor`（参考 `KimiMonitor`）；
已有本地事件源的覆写 `_poll()`（参考 `CursorMonitor`/`OpenCodeMonitor`）。

新接入的宿主必须遵守 §2.5 写方注意事项，尤其是「绝不阻塞宿主」与「只写元数据」；
写外部配置的 Agent 必须在 `AgentSpec` 里声明 `needs_consent`，由桌宠统一弹窗授权。

注册表的字段语义、已接入七家宿主的注入事实（含各自的 schema 红线与实测基线）见
[`AGENT-INTEGRATION-REGISTRY-2026-09-22.md`](AGENT-INTEGRATION-REGISTRY-2026-09-22.md)。

## 6. 隐私与安全红线（不可违反）

1. **默认全关**：所有 Agent 联动开关默认关闭，用户显式勾选才启动。
2. **不访问外网**：联动链路只碰本地文件/本地数据库，以及本机回环（`127.0.0.1`）的 WebSocket/HTTP 交互回写；任何一方不得向外网发起网络请求。
3. **只存元数据**：事件文件只有状态/事件名/工具名，绝不写截图、代码内容、命令全文。
4. **写外部配置必须先弹窗**：任何向 Agent 配置（如 `~/.claude/settings.json`、dsh profiles）注入内容的操作，必须先经用户确认，且卸载时只清理带本桌宠标记的条目。
5. **自定义通道只读**：`custom_agents` 仅监听用户指定的文件，不创建目录、不写任何外部位置。
