# 声明式 Agent 接入注册表与两家新 Agent（Kimi / ZCode）

**基线**：`main`，2026-09-22，本机实测（Linux，Python 3.14 + PySide6 6.11，`QT_QPA_PLATFORM=offscreen`）。

本文是本次「桌宠不只联动 DSH、而是联动系统上所有 Agent 工具」改造的**工程档案**：
为什么引入注册表、注册表的契约是什么、新宿主的注入事实与红线、**移除/新增一个内置
Agent 的完整清单**（§6，含品牌政策相关的一次性清理），以及验证基线。
面向集成方的对外协议口径仍以 [`AGENT_LINK_PROTOCOL.md`](AGENT_LINK_PROTOCOL.md) 为准。

参考实现来源：同机 `clawd-on-desk`（Electron 桌宠，支持 23 家 Agent）的调研结论，原始调研记录见
`.scratch/clawd-integration-inventory/CLAWD-AGENT-INTEGRATIONS.md` 与
`.scratch/clawd-integration-inventory/CLAWD-WIRE-CONTRACT.md`（只读参考，不随产品分发）。

---

## 1. 问题：名单散落在五处

改造前，一个内置 Agent 的接入事实被硬编码在五个互不知情的位置：`pet/config.py` 的默认值与
清洗白名单、`pet/agent_link.py` 的 `monitors` 字典与 `AGENT_NAMES`、`AgentLinkManager.set_enabled`
里的 `if agent_key == "claude" / elif agent_key == "dsh"` 分支、`pet/context_menus/shared.py`
的菜单条目、`pet/uninstall_cleanup.py` 的卸载步骤。

接入第一家（Claude）时代价可接受，接入第二、三家时开始出现「漏改一处 → 配置被清洗掉 /
菜单没开关 / 卸载残留」的隐性缺陷。本次改造先把名单收敛，再接入新 Agent——
否则每加一家都要在五处同步，缺陷面随家数线性增长。

## 2. 方案：`pet/agents/registry.py` 是唯一事实来源

```
pet/agents/
├── __init__.py      # 空包（刻意不做 re-export，避免 config → registry → monitors 循环导入）
├── registry.py      # AgentSpec 声明表 + 惰性点分引用解析（纯数据，不 import Qt）
├── adapters.py      # 内置 Agent 监视器工厂 + DSH 桥接插件安装/卸载适配
├── hook_writer.py   # 通用 hook 落地脚本生成器 + 通用接入助手 CLI（自定义通道）
├── kimi.py          # Kimi Code / 旧版 Kimi CLI：config.toml [[hooks]]
└── zcode.py         # ZCode：cli/config.json hooks.events.*
```

`AgentSpec` 的字段即接入契约：

| 字段 | 作用 | 消费方 |
|---|---|---|
| `key` / `name` / `menu_label` | 配置键 / 气泡与台词显示名 / 右键菜单显示名 | config、agent_link、菜单 |
| `integration` | 接入方式（plugin / hook / tail / db），声明与文档用途 | 文档、UI |
| `monitor` | 监视器工厂点分引用，调用约定 `factory(config_dir, parent)` | `AgentLinkManager.__init__` |
| `order` | 菜单与配置默认值的稳定顺序 | 菜单、config |
| `install` / `uninstall` | 注入器入口点分引用 | `set_enabled`、`--uninstall-cleanup` |
| `uninstall_result_key` | 卸载清理结果字典的键 | `uninstall_cleanup.py` |
| `needs_consent` / `consent_title` / `consent_text` | 写外部配置前的授权弹窗文案 | `set_enabled` |
| `install_mode` | `sync`（毫秒级）或 `background`（可能数十秒，走后台线程 + 信号回主线程） | `set_enabled` |
| `detect_paths` | 本机安装探测点（相对家目录） | `_warn_if_agent_absent` |
| `hook_events` | 已注入的事件名（文档/UI 展示） | 文档、测试 |

两条关键设计约束：

1. **注册表不 import Qt、不 import 监视器实现**。监视器与注入器都用 `"模块:属性路径"` 点分引用
   声明、调用时才解析。这样 `pet/config.py` 可以在导入链最早期引用它而不产生循环依赖；
   同时 `monkeypatch.setattr(ClaudeCodeMonitor, "uninstall_hooks", ...)` 这类既有测试 seam 继续有效
   （每次调用重新 `getattr`，不做导入期绑定）。
2. **监视器留在 `pet/agent_link.py`，注入器留在 `pet/agents/*.py`**。前者需要 Qt 信号与监视器基类，
   后者要求纯逻辑可单测（不建 QApplication）。

安装器返回契约统一为 `bool | tuple[bool, str]`（字符串是失败原因，直接进用户可见的失败气泡），
由 `agent_link._install_outcome()` 归一。

Agent key 词法只有一处定义（`registry.AGENT_KEY_PATTERN`）：内置与自定义通道共用，
`pet/config.py::_clean_custom_agents` 与通用接入助手都按它校验。

## 3. 两家新宿主的注入事实（本机实测）

两家都是 hook 类宿主，事件名都由宿主通过 stdin JSON 的 `hook_event_name`（ZCode 走 argv）送达，
因此**共用一个落地脚本生成器** `hook_writer.ensure_event_hook()`：它生成一个 0.3s 内读完 stdin、
只写元数据（`ts` / `agent` / `event` / `tool`）、失败静默退出的脚本，宿主配置里只需要填
「解释器 + 脚本路径（+ 事件名）」。

| 宿主 | 配置文件 | 形状 | 额外前置条件 |
|---|---|---|---|
| Kimi Code / Kimi CLI | `~/.kimi-code/config.toml`（旧版 `~/.kimi/config.toml`） | 追加 `[[hooks]]` 块，键仅 `event`/`matcher`/`command`/`timeout` | 只写**已存在**的代际目录；写盘前按 Kimi Code strict schema 自校验 |
| ZCode | `~/.zcode/cli/config.json` | `hooks.enabled = true` + `hooks.events.<Event> = [{"hooks":[{"type":"process","command":…,"args":[…, event],"timeoutMs":8000}]}]` | 容器对象只允许 `hooks` 键（多键会让 ZCode 拒绝加载整份配置） |

注入事件集合：

- Kimi（14）：`SessionStart` / `SessionEnd` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` /
  `PostToolUseFailure` / `Stop` / `StopFailure` / `SubagentStart` / `SubagentStop` /
  `PreCompact` / `PostCompact` / `Notification`，Kimi Code 另有 `Interrupt`
- ZCode（6）：`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` /
  `PostToolUseFailure` / `Stop`

### 红线（共同）

1. **不注册审批类事件**（`PermissionRequest` / `PermissionResult`）。它们是宿主侧的阻塞式决策
   通道，桌宠目前只有 DSH 具备审批回写能力；注册了只会把宿主审批流程拖进不确定状态。
2. **绝不覆盖用户显式关闭**：ZCode 的 `hooks.enabled = false` 会被识别为「用户选择」，
   安装直接返回失败并说明原因，不写入。
3. **绝不吞掉不可解析的用户配置**：JSON/TOML 解析失败时返回失败、原文件一字不动（有测试断言
   文件字节不变）。
4. **卸载只删自己的条目**：靠命令/参数里的标记串（`kimi_event_hook` / `zcode_event_hook`）识别，
   第三方 hooks 与用户自有事件原样保留。
5. **多开共享**：hooks 是全局状态，仍有其他实例开着该联动时，关闭开关不卸载
   （`other_instances_use_agent`）。

## 4. 被品牌政策排除的宿主（重要）

仓库有一条**上游品牌政策**：`tests/test_desktop_pet_features.py::test_product_copy_has_no_external_brand_reference`
禁止 `pet/`、`tests/`、`docs/`、`README.md` 出现**某外部 AI 编程 CLI 的名字**。历史记录见
[`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md) 第八轮：

> 应上游品牌政策移除了某 CLI 工具的联动（上游有测试禁止仓库出现该名字）

该宿主本机已安装，但**不做内置联动**——它的配置路径字面量必然含品牌词，内置即撞政策
（中性改名也绕不开路径；用字符串拼接规避测试属于故意规避，明确不做）。它的接入走
**自定义通道 + 通用接入助手**（§5），仓库内零品牌词，用户本机照样联动。

政策若将来放宽，重新内置时的完整改动面见 §6.2；届时需要同步放宽的正是那条测试的豁免名单
（`agent_link.py` / `test_agent_link.py` / `*-RESEARCH.md` / `README-CHANGE-*` 已是既有豁免）。

## 5. 通用接入助手（给任意宿主的零代码通道）

`pet/agents/hook_writer.py` 提供 CLI，为**任何**能执行命令的宿主生成 hook 命令：

```bash
# 生成（脚本落在 --out 同目录，命令直接可粘）
python -m pet.agents.hook_writer --agent mycli --out ~/.mycli/pet-events.jsonl
# 宿主用 argv 传事件名时，每个事件各生成一条
python -m pet.agents.hook_writer --agent mycli --out ~/.mycli/pet-events.jsonl --event Stop
# 需要程序化处理时输出 JSON（command / executable / args / script）
python -m pet.agents.hook_writer --agent mycli --out ~/.mycli/pet-events.jsonl --json
```

再把打印出的 `{"key", "name", "path"}` 填进 `config.json` 的 `agent_link.custom_agents`
（协议见 [`AGENT_LINK_PROTOCOL.md`](AGENT_LINK_PROTOCOL.md) §4）。CLI 会拒绝非法 key 与内置 key
（内置有专属安装器，不该被引导到自定义通道）。

**适用范围**：助手是源码运行环境的能力（`python -m pet.agents.hook_writer`）。打包版用户没有
`python -m pet` 入口，按协议文档 §4 的 `printf` 示例手动追加即可，事件格式完全一致。

## 6. 增删一个内置 Agent 的完整清单

### 6.1 移除（准出清单）

删掉 `AgentSpec` 条目后，配置默认值/清洗白名单、右键菜单、监视器装配会自动跟上
（因为它们都从注册表派生）。但下面这些**不会自动跟上**，必须逐项确认：

| # | 位置 | 动作 |
|---|---|---|
| 1 | `pet/agents/registry.py` | 删除该 `AgentSpec`（同时把后续 `order` 顺延，保持菜单稳定顺序） |
| 2 | `pet/agents/<id>.py` | 删除该宿主的注入器模块（若有） |
| 3 | `pet/agent_link.py` | 删除对应监视器类（`HookAgentMonitor`/`BaseAgentMonitor` 子类） |
| 4 | `tests/test_agent_link.py::test_default_all_disabled` | 从期望字典里删掉该 key（**该用例钉住 agent_link 默认值全集**，不删就红） |
| 5 | `tests/test_agents_registry.py` | 删除该宿主专属测试类；注册表一致性用例里的 key 断言同步 |
| 6 | `docs/AGENT_LINK_PROTOCOL.md` | §3 表格行、§4 内置键清单 |
| 7 | `README.md` | 功能列表中的 Agent 名单（三处：亮点、功能小节、目录树） |
| 8 | `docs/INDEX.md` / 本文 | 索引行与本文的宿主清单 |
| 9 | **已发布版本的残留（最容易漏）** | 若该 Agent 曾随**已发布版本**装过 hooks/插件：删掉注册条目后 `--uninstall-cleanup` 不再清理它，升级用户的宿主配置会残留。必须在 `pet/uninstall_cleanup.py` 保留**一次性清理分支**（直接调该宿主的 uninstaller，或内联删除逻辑），并在发布说明里标注「下一版可移除」；等到确认没有用户可能从含该 Agent 的版本升级时再删 |
| 10 | 品牌政策 | 若该 Agent 的名字在品牌黑名单上，删除后必须跑 `pytest tests/test_desktop_pet_features.py::test_product_copy_has_no_external_brand_reference`，确认 `pet/`、`tests/`、`docs/`、`README.md` 无残留（含注释与 docstring；`__pycache__` 里的旧 `.pyc` 不算） |

> 本仓库确实踩过第 9 条的对称问题：卸载清理只按**当前**注册表走，所以「删除」与
> 「清理已装用户的残留」是两件事，必须分开处理。

### 6.2 新增

见 [`AGENT_LINK_PROTOCOL.md`](AGENT_LINK_PROTOCOL.md) §5：加 `AgentSpec` + 写注入器 + 监视器类 +
事件映射；`pet/config.py`、右键菜单、`--uninstall-cleanup` 都不用改。若该宿主名字在品牌黑名单上，
还需先解决 §4 的政策问题（放宽测试豁免 + 在本文记录理由），否则 CI 会红。

## 7. 菜单与配置的变化

- 右键「Agent 联动」子菜单由 `agent_specs()` 生成，顺序即 `order`：
  DSH(10) → Claude Code(20) → Cursor(30) → OpenCode(40) → Kimi Code(50) → ZCode(60)。
- `agent_link` 配置新增两个默认 `false` 的布尔开关，由 `builtin_agent_keys()` 派生默认值与清洗白名单；
  自定义通道 `custom_agents` 的 key 仍然不得与任一内置键重复（否则菜单/监视器注册撞车）。
- 这些开关是**菜单级开关**（Menu Action Model），不是设置页设置项，因此不进入
  [`SETTINGS-CHANGE-GATES.md`](SETTINGS-CHANGE-GATES.md) 的设置页契约（无设置页布局/搜索/披露层级变化）。
- `--uninstall-cleanup` 改为遍历 `uninstallable_specs()`，结果键在原有 `claude_hooks` / `dsh_bridge`
  之外新增 `kimi_hooks` / `zcode_hooks`（旧键保持不变，安装包脚本不受影响）。
- 开启成功的气泡文案由 `agent_key.upper()` 改为注册表显示名（`pet/window.py::_toggle_agent_link`）。

## 8. 验证

新增 focused 测试 `tests/test_agents_registry.py`（34 项）：注册表 ↔ 配置默认值/清洗白名单一致性、
六家监视器工厂可装配、通用 hook 脚本**真子进程**写入统一协议（含 argv 优先、事件名缺失时不写记录）、
通用接入助手 CLI（命令可用、拒绝非法/内置 key、生成命令端到端写事件）、
两家注入器的幂等/用户条目保留/显式关闭拒绝/不可解析配置不覆盖/卸载只删自己、Kimi strict schema 自校验。

```
.venv/bin/ruff check pet/ tests/                                  # All checks passed
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_agents_registry.py -q   # 34 passed
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q           # 2752 passed / 21 skipped / 7 failed（见下）
```

全量那 7 个失败**与本改动无关**：在未改动的 `HEAD`（56f8ad2）上逐条复跑同样失败，属本机环境
（Python 3.14 + 无 Windows 侧依赖）问题，已建临时 worktree 对照确认：

| 失败用例 | 归属 |
|---|---|
| `test_agent_link_dep_specs.py::test_safe_probes_swallow_permission_errors` | Python 3.14 的 `Path.is_dir()` 不再走 `Path.stat`，打桩失效 |
| `test_click_sound.py`（4 项） | 本机文件权限/内容哈希探测（`OSError`） |
| `test_harness_launcher.py::test_find_launch_command_fallback_without_dsh` | 本机 PATH 里有 `dsh`，与用例前提不符 |
| `test_windows_node_env.py::TestHarnessGlobalRoots::…nvm_windows_root` | Windows 专属布局用例，Linux 上跑 |

品牌政策测试 `test_product_copy_has_no_external_brand_reference` 复绿（残留的注释/docstring
引用已清干净；`docs/SETTINGS-REDESIGN-Q5-…-RESEARCH.md` 属既有豁免）。

已同步更新的既有测试：`tests/test_agent_link.py::test_default_all_disabled`（新增两个默认键）、
`TestInstallFinishedGuard`（后台安装线程名 `dsh-bridge-install` → `agent-install-dsh`）。
后台安装线程名属白盒 seam，改名理由：它已不再专属于 DSH。

## 9. 相关文档

- [`AGENT_LINK_PROTOCOL.md`](AGENT_LINK_PROTOCOL.md) —— 统一事件协议、六态词汇、自定义通道、新增内置 Agent 的步骤（§5）。
- [`DSH-BRIDGE-PET-EVENT-CONTRACT-2026-09-02.md`](DSH-BRIDGE-PET-EVENT-CONTRACT-2026-09-02.md) —— DSH 侧三层事件契约。
- [`PROACTIVE_SCREEN_IMPLEMENTATION_MANUAL.md`](PROACTIVE_SCREEN_IMPLEMENTATION_MANUAL.md) —— Cursor/OpenCode 直读适配器细节。
- [`PROJECT_HANDOFF.md`](PROJECT_HANDOFF.md) —— 品牌政策的历史出处（第八轮）。
