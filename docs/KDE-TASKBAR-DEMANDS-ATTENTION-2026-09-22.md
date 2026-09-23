# KDE/X11 任务栏被灵动岛永久高亮（`_NET_WM_STATE_DEMANDS_ATTENTION`）修复记录

**基线**：2026-09-22，Linux / KDE Plasma（`XDG_SESSION_TYPE=wayland`，应用按
`pet/app.py:_default_xcb_platform_on_wayland()` 强制走 XWayland `platform=xcb`），
PySide6 6.x + Qt xcb 插件，屏 1600×1000 @ dpr2。
**状态**：已修复并实机验证；`.venv`（CPython 3.11.16）全量套件通过。

相关文档：[`LINUX-DEV-ENVIRONMENT-2026-09-22.md`](LINUX-DEV-ENVIRONMENT-2026-09-22.md)（Linux 环境与实机验收口径）、
[`BUGFIX-AND-FEATURES-2026-08-24.md`](BUGFIX-AND-FEATURES-2026-08-24.md)（同族的"气泡不抢输入焦点"历史修复）。

---

## 1. 现象

用户报告（原文）："现在其出现了启动后，窗口一直高亮导致状态栏一直被唤醒的问题"。

两次澄清把范围钉死：

1. "但是关掉横栏，即非桌宠的部分，便会停止高亮" —— 出问题的是**灵动岛**（`DynamicIsland`，顶部"横栏"），
   关掉它症状即停。
2. "高亮并不指其在变，而是指其一直唤醒着任务栏" —— 不是闪烁/频率问题，而是**一个持续存在的状态**：
   KDE 任务栏里那条窗口条目长期处于"要求关注（needs attention）"的高亮/脉冲态。

## 2. 定位证据

### 2.1 哪个窗口带标记

用 `xprop` 读窗口管理器状态（应用布局：桌宠 `winId` = `0x1a00007`，灵动岛 = `0x1a0000e`，由探针打印的
`winId` 换算得到）：

```bash
xprop -id 0x1a0000e _NET_WM_STATE   # 灵动岛
# _NET_WM_STATE(ATOM) = _NET_WM_STATE_DEMANDS_ATTENTION, _NET_WM_STATE_ABOVE, _NET_WM_STATE_STAYS_ON_TOP
xprop -id 0x1a00007 _NET_WM_STATE   # 桌宠
# _NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE, _NET_WM_STATE_STAYS_ON_TOP
```

**只有灵动岛带 `_NET_WM_STATE_DEMANDS_ATTENTION`**，且 `xprop -spy` 盯 15 秒无任何变化——
它是一个静止的、永不自清的标记（与用户"一直"的口径一致）。

### 2.2 为什么永不自清

```bash
xprop -id 0x1a0000e WM_HINTS
# WM_HINTS(WM_HINTS):
#         Client accepts input or input focus: False
```

灵动岛窗口带 `Qt::WindowDoesNotAcceptFocus`，在 X11 上表现为 `WM_HINTS` 的 input hint = False：
**窗口管理器永远不会真正激活它**。而 EWMH 的 `_NET_WM_STATE_DEMANDS_ATTENTION` 恰恰是"窗口要求激活但没被激活"
的通告态，正常路径下由"窗口真的被激活"来清除——对这个窗口这条清除路径永远走不到，于是任务栏条目
永久高亮，并按 KDE 的 needs-attention 逻辑反复唤醒任务栏。

### 2.3 触发条件：`WA_ShowWithoutActivating`

在真实应用里跑时间线探针（记录 `t`、窗口级调用、调用点），岛的 `show()` 在 `t=0.64`（`pet/app.py:2020 _sync_dynamic_island`），
标记在 `t≈0.71~0.81` 之间出现——即**映射那一刻**被打上。

随后用运行时二分逐个屏蔽启动路径上的动作（每次只改一处，其余全真），结果：

| 变体（屏蔽对象） | 灵动岛 `_NET_WM_STATE` |
|---|---|
| baseline（什么都不屏蔽） | **FLAGGED** |
| `no_pet_show`（桌宠不 show） | FLAGGED |
| `no_dock`（不做停靠定位） | FLAGGED |
| `no_fixed_size`（不设 fixed size） | FLAGGED |
| `no_noaccept`（flags 去掉 `WindowDoesNotAcceptFocus`） | FLAGGED |
| `no_translucent`（去掉 `WA_TranslucentBackground`） | FLAGGED |
| **`no_showwithout`（不设 `WA_ShowWithoutActivating`）** | **CLEAN** |
| `no_sync_show`（岛不由 `_sync_dynamic_island` 拉起） | FLAGGED |

即：在这套窗口 flag 组合下，**`WA_ShowWithoutActivating` 是必要触发条件**。

### 2.4 去掉它是否安全

| 检查项 | 结果 |
|---|---|
| 去掉后窗口是否抢焦点 | 否。`_NET_ACTIVE_WINDOW` 全程仍是 `0x200000`（非本应用窗口） |
| 去掉后 hide→show 循环 | 仍 CLEAN，标记不回流 |
| 是否本来就不需要它 | 是。窗口已带 `WindowDoesNotAcceptFocus`（`WM_HINTS` input=False），WM 本来就不会给键盘焦点 |

**未完全钉死的部分（如实记录）**：Qt/KWin 内部究竟是哪一步把 `WA_ShowWithoutActivating` 转成了 demands-attention
没有追到源码级；单个最小窗口（同 flag 组合、单窗口、无其他窗口）复现不出该标记，说明还依赖应用级上下文
（同应用已有其它顶层窗口 / 映射时序等）。因此本文记录的是**实测规则**而非机制推导。若未来 Qt/KWin 升级后
行为变化，按第 5 节重新实测即可。

## 3. 修复

规则收口到一个模块，`pet/window_activation.py`：

> Linux 上，已经带 `Qt::WindowDoesNotAcceptFocus` 的窗口**不要再设** `WA_ShowWithoutActivating`；
> 其余情况（非 Linux，或窗口没有 `WindowDoesNotAcceptFocus`）保持原行为——该属性是 Windows/macOS 上
> "显示但不成为前台/key window"的手段，不能省。

`apply_show_without_activating(widget)` 在 `setWindowFlags()` **之后**调用（要读 flags 判断），返回是否真的设置了该属性。

调用点（三处，都是"置顶 + 不接受焦点 + 显示不抢焦点"的窗口）：

| 文件 | 窗口 | 说明 |
|---|---|---|
| `pet/dynamic_island.py` | `DynamicIsland` | 本次报告的直接病灶（常驻、启动即映射） |
| `pet/desktop_notify.py` | `DesktopNotification` | 同一 flag 组合，风险相同 |
| `pet/speech_bubble.py` | `PetSpeechBubble` | 同一 flag 组合；气泡高频 show/hide，同族风险 |

**未改**：`pet/island_chat.py`（`IslandChatBubble`）——它的 flags 来自 `QuickChatBubble`，**没有**
`WindowDoesNotAcceptFocus`，去掉属性会真的抢焦点，按规则保留原行为。

## 4. 验证

- 单元（`tests/test_window_activation.py`，9 例，offscreen 可跑）：规则本身 4 例 + 三个真实窗口在
  Linux/非 Linux 两个分支下的属性断言 5 例。**回退三个调用点后其中 3 例红，恢复后 9 例全绿**。
- 实机（XWayland + KWin，真实配置）：修复前 baseline 探针 FLAGGED；修复后连跑两次均 CLEAN，
  且 `_NET_ACTIVE_WINDOW` 未被抢。
- 全量：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`（见提交记录）。

## 5. 未来重新实测怎么做

启动应用（真实会话，非 offscreen）后，取灵动岛窗口 id 并读状态：

```bash
# 1) 列出本应用窗口
xprop -root _NET_CLIENT_LIST | tr ',' '\n' | while read -r id; do
  xprop -id "${id// /}" WM_CLASS 2>/dev/null | grep -q dsh-pet-standalone && echo "${id// /}"
done
# 2) 逐个看状态：目标窗口不应出现 DEMANDS_ATTENTION
xprop -id <id> _NET_WM_STATE
# 3) 盯变化（正常应无输出变化）
xprop -spy -id <id> _NET_WM_STATE
```

若重新出现 `DEMANDS_ATTENTION`：先看该窗口是否又被设了 `WA_ShowWithoutActivating`
（`grep -rn "WA_ShowWithoutActivating" pet/`），再按第 2.3 节的二分法定位新增触发点。

## 6. 干净回退/删除指引

本修复的全部产物（回退时按此清单逐项处理，无残留）：

1. `pet/window_activation.py` —— 整文件删除。
2. 三个调用点把 `apply_show_without_activating(self)` 还原为
   `self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)`：
   `pet/dynamic_island.py`（`DynamicIsland.__init__`）、`pet/desktop_notify.py`（`DesktopNotification.__init__`）、
   `pet/speech_bubble.py`（`PetSpeechBubble.__init__`），并删除各自的
   `from .window_activation import apply_show_without_activating`。
3. `tests/test_window_activation.py` —— 整文件删除。
4. 本文档 + `docs/INDEX.md`「渲染、解码与窗口结构」分组中的对应行 —— 一并删除（旧行为是 bug，
   删除即回到"任务栏永久高亮"的已知缺陷，不要保留半套实现）。

> 排查期使用的一次性探针（`.scratch/window-wakeup-probe/`）为 gitignored 临时件，已随手删除，不入库。
