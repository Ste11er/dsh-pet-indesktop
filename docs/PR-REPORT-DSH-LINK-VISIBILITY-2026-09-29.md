# PR 报告：DSH 联动「看得见」（状态行 + 审批/提问常驻提醒 + 计划台账）

- 分支：`fix/dsh-link-visibility`（基于 `origin/main` 的 `3cdfe98`）
- 日期：2026-09-29
- 触发：主人报告「项目文件中提到，项目会联动 dsh，使得 pet 做出相应的反应，但我现在
  并没有见到相应的反应，解决这个问题」
- 关联台账：[`docs/plans/NEXT.md`](plans/NEXT.md)（本轮 5 条经确认的延后项）、
  [`docs/plans/DONE.md`](plans/DONE.md)

## 一、核心特性

主人看到的「没有任何反应」不是一条链断了，而是**五条**同时在断，且前三条在界面上
长得一模一样（都是"没反应"），只能靠人肉翻 `~/.dsh/profiles/*/package.json` 与
`~/.config/dsh-pet-bridge/` 才能分辨：

| # | 根因 | 证据 | 本次处置 |
|---|---|---|---|
| 1 | 桥接插件根本没装进 web profile | `~/.dsh/profiles/web/package.json` 的 `dependencies` 无 `@dsh-pet/bridge`，`node_modules/@dsh-pet/` 不存在 | 状态行显式写出「未安装」；开启入口复用既有的同意 + 后台安装路径 |
| 2 | 联动开关只在右键菜单里，默认关 | `~/.config/dsh-pet-standalone/config.json` 的 `agent_link.dsh=false`；唯一入口 `pet/context_menus/shared.py:339-351` | 状态行常驻显示「已安装，但…未启用」并提示去哪开 |
| 3 | `app.py` 只对 `thinking` / `offline` 有反应 | `pet/app.py:1889-1918`（原文只有这两个分支） | `waiting_approval` / `waiting_question` → 常驻提醒；其余状态收口提醒 |
| 4 | 汇报概率门把 60% 的思考气泡随机吞掉 | 本机 `report_gates.state=0.4`（代码默认 1.0，见 `pet/report_gates.py:19-28`） | **不改默认值**：这是主人的显式偏好，交接项里写清"自己拖回 100%" |
| 5 | 本机 DSH 0.1.7-rc.2 删除了桥接依赖的 mux 契约 | 插件侧 `integrations/dsh-pet-bridge/index.js:1129,1148,1151` 依赖 `/api/events.mux`+`/api/respond`；0.1.7 只剩 `/api/remote.mux`（`dsh-api-gateway/lib/index.js:12`） | 可点击回写移植**延后**（台账 ①）；本次先用只读常驻提醒覆盖"看得见" |

同时补上**机制**：主人要求把"本轮跳过/延后的事"变成可追踪的台账，于是新增
`docs/plans/NEXT.md`（待办池）+ `docs/plans/DONE.md`（完成档案）+ `AGENTS.md`
硬规则（收尾必须显式加载 `grilling` skill 做**恰好一轮**"跳过项是否入池"追问；
无跳过项必须写明一行，不许沉默略过），并由 `tests/test_plan_ledger.py` 机器化校验
两份台账的结构不变量。

明确**不做**（都进了台账，不是遗忘）：可点击审批/问答回写（①）、`XDG_CONFIG_HOME`
路径不一致（②）、`offline` 抖动无去抖（③）、审批/提问之外的独立可见反应（④）、
额度可选展示项与 token 自动刷新（⑤）。

### 追加（同日第二轮）：设置入口的「孤儿进程」事故

主人紧接着的两条报告——「我打不开桌宠设置的界面」与「桌宠现在还并没有和 DSH 进行
联动」——是**同一个根因**。本机现场（2026-09-29 17:16–17:46）：

- PID 103991 是 17:16:38 由**另一只桌宠**拉起的设置进程；那只桌宠 17:28 退出后它
  被 init 收养，继续占着 `~/.config/dsh-pet-standalone/settings.lock`（mtime
  17:16:39、95 字节、内容记着 pid 103991）；
- 于是当前桌宠（PID 126524）每次被点设置都只写一行「独立设置进程已在运行，不重复
  拉起」（17:31:54），并把 `_settings_child_active` 置真 → **所有气泡按「设置开着」
  被抑制**；而 17:31–17:38 的日志里 `[DSH STATE] idle→working→thinking`、
  `working→waiting_approval` 一直在刷——**联动本身是通的，只是全被抑制成不可见**；
- 那扇窗口自己没坏（`xprop`：`WM_STATE=Normal`、800×560 逻辑尺寸、完整落在屏幕内），
  只是被 WPS / ChatGPT 压在后面 → 用户看到的就是「点设置没反应」。

处置是**两条产品修复**（不是现场清理）：孤儿自退 + 再点设置把已有窗口叫到前台。
kill 掉孤儿后立即复验：17:46:15 日志出现
`alert enqueue alertType=approval priority=1 alertId=dsh-waiting`——审批常驻提醒真的
挂出来了（§五 第 7 条）。

## 二、修改文件说明

`git diff --numstat`（已跟踪文件）与新增文件行数：

| 文件 | 增/删 | 改了什么 + 为什么 |
|---|---|---|
| `AGENTS.md` | +19/−0 | 新增 `### Plan ledger and session close-out`（收尾追问/同轮归档/计划项≠断点/台账保持小）+ `Context pointers` 一句指针。**为什么**：主人要求把"跳过项"机制化，否则下一轮又会静默丢掉。 |
| `docs/INDEX.md` | +3/−0 | 登记 `docs/plans/NEXT.md` / `DONE.md`（新文档入场规则）+ 本报告（PR 报告存档）。 |
| `pet/agent_link.py` | +61/−0 | 等待提醒整条链：文案表 `_DSH_WAITING_ALERTS` + `_show_dsh_waiting`（挂 sticky 提醒）+ `dismiss_dsh_waiting`（幂等收口）+ `notify_dsh_state` 分流（等待态不进 legacy 簿记）+ `__init__` 一个布尔标志。**为什么**：本机已无 mux 可点击通道，状态机信号是"审批在等你"唯一可靠来源。 |
| `pet/app.py` | +33/−1 | `_on_dsh_state_changed`：`waiting_*` 转发给联动管理器；其余状态只调 `dismiss_dsh_waiting`（老桩没有该方法时靠 `hasattr` 跳过）。**为什么**：把"挂上"与"收掉"两个时机都接上，同时**不动**"working 不转发"的既有契约（既有测试钉着它）。第二轮追加：`_raise_existing_settings_process()`（锁被持有时请已有窗口到前台，失败只记日志、绝不回退开第二扇窗）+ 一行顶层导入（改的是"点设置没反应"里的"没反应"那一半）。 |
| `pet/__main__.py` | +37/−0 | `--settings` 主体：构造对话框后装**父进程看门狗**（`ParentWatch(dialog.reject)`，走与 Esc/关窗同一条落盘路径）+ **唤起通道服务端**（`SettingsRaiseServer`，收到 `raise` 就 `_raise_dialog`）。收尾用 `for teardown in (watch.stop, channel.stop)` 逐个兜异常，**保证 `lock.unlock()` 一定执行**。**为什么**：设置进程是 `startDetached` 拉起的孤儿候选，父死必须自退（否则占着锁把后续入口和气泡一起闷死）；单实例下"再点设置"必须能把已有窗口叫出来。 |
| `pet/settings_channel.py` | 新 +132 | 唤起通道：`channel_name(config_dir)`（沿用 `collision_server_name` 的 uid+摘要命名，37 字符，POSIX 上是 `/tmp/<name>` socket）+ `raise_existing_settings()`（有界 250 ms、失败返回 False 不外抛）+ `SettingsRaiseServer`（`start()` 先 `removeServer` 回收陈旧端点——调用点已持锁故安全；`stop()` 幂等）。 |
| `pet/settings_parent_guard.py` | 新 +131 | 父进程看门狗：`parent_process_gone()`（POSIX 用 `getppid()` 变化 + `kill(pid,0)` 兜底；Windows 用 `OpenProcess(SYNCHRONIZE)`+`WaitForSingleObject`）+ `ParentWatch`（2 秒轮询、回调幂等一次、`stop()` 幂等）。 |
| `tests/test_settings_channel.py` | 新 +132 | 7 用例：通道名稳定/短/按目录区分、活服务端往返、无服务端快速返回 False、垃圾载荷忽略、stop 后可重新 listen、陈旧端点回收（POSIX）、**对话框自毁后 stop() 不抛且端点不残留**。 |
| `tests/test_settings_parent_guard.py` | 新 +91 | 9 用例：被收养/pid 消失/父仍活/`getppid` 抛错退化为 pid 探测/pid 0、看门狗只回调一次、父活时不回调、回调抛错不外泄、**对话框自毁后 stop() 不抛**。 |
| `tests/test_settings_process_isolation.py` | +140/−1 | 4 条新用例：锁被持有时**会请已有窗口到前台**、唤起抛异常仍保持单实例语义（返回 True 且不回退开第二扇窗）、`--settings` 装了通道+看门狗并在退出时都收干净（且回调必须是 `dialog.reject`）、**收尾抛异常也不许吞掉 `lock.unlock()`**。 |
| `pet/settings_pet_controls.py` | +59/−0 | `DshLinkStatusLabel`（`showEvent` 自刷新）+ `add_dsh_status_group(box, dialog)`（挂组并返回行供 `claimed` 登记）+ `QLabel` 导入。**为什么**：设置对话框是应用级缓存的，状态不能只在装配时算一次。 |
| `pet/modern_settings_dialog.py` | +1/−0 | 一行 `claimed.update(settings_pet_controls.add_dsh_status_group(agent_box, self))`。**为什么**：本文件有行数预算（2346 → 2347，恰好卡在预算上限 2347），所以"加组 + 登记"压成一行，行定义全部留在上面的构建器里——这是仓库文档推荐的"优先拆分"而非"压行达标"。 |
| `pet/dsh_link_status.py` | 新 +212 | 纯探测模块（无 Qt）：五态判定 + 事件新鲜度 + 候选端口探测 + 防漂移常量。**为什么**：`agent_link.py` 已 5000+ 行、`modern_settings_dialog.py` 有预算；这里只做文件系统/端口探测，测试可以不起事件循环直接跑。 |
| `tests/test_dsh_link_status.py` | 新 +272 | 17 用例：五态判定顺序、防漂移常量、事件年龄、垃圾输入容错、`DSH_HOME` 覆盖、候选端口覆盖、设置页装配（行存在 + 落在「Agent 联动」框内 + `showEvent` 后状态从「未安装」变「已连接」）。 |
| `tests/test_dsh_link_visibility.py` | 新 +227 | 10 用例：提醒挂载/共用 id/幂等收口/未联动时 no-op/不污染 legacy 簿记（`_last_raw`/`_last_applied` 保持空），以及 `AppShell._on_dsh_state_changed` 的四条派发路径。 |
| `tests/test_plan_ledger.py` | 新 +121 | 9 用例：台账文件存在 + INDEX 登记 + `AGENTS.md` 含规则 + id 唯一 + 不得同时出现在两份台账 + 每条含三个字段。 |
| `docs/plans/NEXT.md` | 新 +76 | 待办池：约定（入池/归档/格式/与断点的分工）+ 7 条经确认的延后项。 |
| `docs/plans/DONE.md` | 新 +15 | 完成档案：append-only 表格 + "从 NEXT 删除与在此追加是同一动作的两半"的说明。 |

未改动（有意）：`pet/dsh_state.py`（状态机本身经探针验证是好的）、
`integrations/dsh-pet-bridge/*`（mux 移植延后）、`pet/report_gates.py`
（默认值保持 1.0，本机 0.4 是主人偏好）。

## 三、实现要点

1. **五态判定按"最可能的原因优先"排序**：未安装 → 未启用 → DSH 未运行 →
   已装但没收到事件（提示重启 DSH）→ 已连接（附"N 秒前收到过事件"）。
   顺序本身就是排错路径：先看装没装，再看开没开，最后才怀疑"是不是没重启"。
2. **提醒走 alert 队列，不进 `_on_agent_state`**。DSH 的 `waiting_*` 不属于 legacy
   状态词表（`thinking/working/attention/error/idle`）；硬塞进去会把
   `_last_raw["dsh"]` 写成 `waiting_approval`，于是随后的 `working` 会被 busy 边沿
   判成"新一轮开始"（多发一次开始音效 + 消费记录）。`test_waiting_does_not_pollute_legacy_bookkeeping`
   钉住这一点。
3. **`alert_id` 固定为 `dsh-waiting`**：两类等待互斥替换，不叠气泡；收口用
   `resolve_alert(alert_id)`（仓库既有的精确收口 API，避免误关别的 agent 的提醒），
   并且只在"确实挂过"时才收（幂等、不误伤）。
4. **`priority=1` + `alert_type="approval"`**：压过普通 watchdog(3)、让可点击交互(0)
   优先；设置页打开的抑制期结束后会自动恢复（`alert_survives_suppression` 白名单）。
5. **自刷新放在 `showEvent`**：`pet/app.py` 只构造一次设置对话框并反复 show，装配时
   算一次必然过期；`showEvent` 覆盖"打开设置 / 切到该页 / 展开该组"全部显示路径，
   dialog 侧不必再加钩子（也就不用再占预算行）。
6. **兼容旧桩**：`show_alert` 缺席时退化为 `show_bubble`，老桩只认 `text` 时再退一步
   （与 `_show_interaction_bubble` 同口径）；`app.py` 侧用 `hasattr` 守
   `dismiss_dsh_waiting`，第三方/测试替身管理器不会崩。
7. **端口探测必须用候选集，且不能让"端口没探到"压过"刚收到事件"**——这一条是
   **交付当天被主人抓出来的**：主人已经装好插件、开关也已打开、DSH 就跑在
   `127.0.0.1:3080`（他正通过它跟我说话），设置页却写着「没有探测到 DSH 服务」。
   根因是我第一版只探 `harness_launcher` 的默认端口 `38080`，而状态机侧
   （`pet/dsh_state.py:308-331`）一直用的是候选集 `{3080, $DSH_PORT, 38080}`——
   同一个"DSH 在线吗"出现了两套口径。修法：(a) 复用同一候选集（3080 排最前，
   它是真实 web 默认、命中率最高）；(b) 把**事件新鲜度提到端口探测之前**——
   "刚刚写进事件文件"只有活着的插件做得出来，比端口猜测更硬，因此 DSH 跑在
   非常规端口时也不许假报离线。三条测试钉住：
   `test_port_probe_covers_all_candidate_ports`、`test_fresh_events_beat_unreachable_port`、
   `test_candidate_ports_match_tracker_set`（防漂移）。
   顺带修正另一处同源失真：有历史事件但这一阵没活动 ≠ "插件没加载"，
   现在报「已连接：最近一次 DSH 事件在 N 分钟前（DSH 空闲时正常）」而不是
   催人"重启 DSH"（`test_stale_event_still_connected_when_dsh_running`）。
8. **孤儿设置进程必须自退**：设置进程是 `QProcess.startDetached` 拉起的，本来就
   **故意**比桌宠活得久（进程内设置页首开留下的字体/样式高水位没有卸载 API，只能靠
   进程退出回收）。但"父死"没有任何人负责 → 实测被 init 收养 30 分钟、占着
   `settings.lock` 把后续入口和气泡一起闷死。判据用两条：`getppid()` 变了（被收养）
   或该 pid 查不到；Windows 上 `getppid()` 语义不可靠，改查进程退出。回调走
   `dialog.reject`（与按 Esc/关窗同一条落盘路径，不丢未保存改动）。
9. **再点设置＝唤起已有窗口**：单实例语义是刻意的（设置项即时落盘，两扇窗会互相
   覆盖），所以不能"再开一扇"；补的是"窗口被压在后面时用户以为没反应"。通道名按
   `config.dir` 派生（与该锁的粒度一致，各槽位共用一扇窗），收到 `raise` 就
   `setWindowState` 去最小化 + `show()/raise_()/activateWindow()`。
10. **收尾必须幂等（本轮自己抓到的缺陷）**：设置对话框设了 `WA_DeleteOnClose`，关窗
    时它自毁，会把挂在它下面的 QTimer / QLocalServer 一起带走；第一版 `finally` 里
    直接 `watch.stop()` 于是在真机抛出
    `libshiboken: Internal C++ object (PySide6.QtCore.QTimer) already deleted`——更糟的
    是它会**排在 `lock.unlock()` 前面**，把锁留下。修法两层：两个 `stop()` 各自兜住
    `RuntimeError`，`_exec_settings` 的 finally 再逐个 teardown 兜异常。红→绿证据：
    旧写法直接抛（真机 traceback + 单跑脚本复现），新写法正常返回；三条新用例钉住
    （`test_stop_is_safe_after_dialog_destroyed` ×2 + `test_exec_settings_unlocks_even_if_teardown_raises`）。

## 四、性能分析

环境：本机 Linux，`.venv`（CPython 3.11.16），`QT_QPA_PLATFORM=offscreen`。
命令：`.venv/bin/python .scratch/dsh_link_probe_bench.py`（n=300）与同目录的 Qt 标签
基准（n=200）。实测：

| 路径 | 均值 | 最坏 | 样本 |
|---|---|---|---|
| `probe_dsh_link`：未安装（无 profile 目录，大多数未装用户） | 6.5 µs | 12.2 µs | 300 |
| `probe_dsh_link`：已连接（事件新鲜 → 提前返回，**不探端口**） | 37.6 µs | 68.6 µs | 300 |
| `probe_dsh_link`：无新鲜事件 + 真候选端口探测（本机 DSH 未运行 → 两个候选都立刻 ECONNREFUSED） | 38.4 µs | 56.9 µs | 300 |
| `DshLinkStatusLabel.refresh()`（真候选端口探测 + `setText`） | 0.03 ms | 0.13 ms | 200 |

- **稳态开销**：不新增定时器、线程、常驻进程；新增常驻状态只有一个 `bool`
  （`_dsh_waiting_shown`）与一个 `QLabel`（设置页内，随对话框生命周期）。
- **新增路径成本与触发频率**：状态行只在**显示时**算一次（打开设置 / 切页 / 展开，
  典型每分钟 0～几次），最坏 0.13 ms；提醒只在 DSH 状态**边沿**触发（一轮对话几条），
  成本是内存内入队 + 一次气泡绘制。
- **系统调用/网络/磁盘**：状态行每次显示 = `iterdir(profiles)` + 每个 profile 一次
  `stat`/读取 `package.json`（通常 0～2 个）+ `glob(事件目录)` + **最多
  `len(candidate_ports())` 次本地 `connect()`**（本机 2 个：3080 / 38080；
  事件新鲜时 0 次——提前返回）。未安装路径（6.5 µs）连端口都不连。
  提醒路径**没有任何**磁盘/网络/系统调用。
- **内存**：无累积增长（无缓存、无队列增长；alert 队列本身由仓库既有机制管理）。

第二轮追加路径的实测（`.venv/bin/python .scratch/dsh_channel_bench.py`，本机**空闲**时
跑，`QT_QPA_PLATFORM=offscreen`；旧写法在同负载下才准，故不与其他任务并跑）：

| 路径 | 均值 | p95 | 最坏 | 样本 |
|---|---|---|---|---|
| `channel_name()`（每次唤起算一次） | 18.8 µs | 20.3 µs | 36.0 µs | 2000 |
| `raise_existing_settings()` → 活着的设置进程（客户端+服务端一次完整往返） | 101.3 µs | 110.0 µs | 1765.0 µs | 200 |
| `raise_existing_settings()` → 没有服务端（50 ms 上限） | 78.2 µs | 93.0 µs | 114.7 µs | 20 |
| `SettingsRaiseServer.start()+stop()`（每次打开设置一次） | 90.3 µs | 100.3 µs | 113.8 µs | 50 |
| `parent_process_gone()` 被收养 / 父仍活（每个设置进程每 2 秒一次） | 0.5 / 1.1 µs | 0.6 / 1.2 µs | 6.9 / 7.0 µs | 2000 |

- **稳态开销（第二轮）**：桌宠侧**零**新增常驻——只有"再点设置"时同步做一次 101 µs
  的本机往返；设置进程侧多一个 2 秒周期定时器，每次 0.5–1.1 µs（≈0.3 µs/s），且只在
  设置窗开着的时段存在。
- **新增系统调用/网络/磁盘（第二轮）**：唤起 = 1 次 `connect()` + 1 次 `write()`；
  看门狗 = 每 2 秒 1 次 `getppid()` + 1 次 `kill(pid, 0)`（Windows 为
  `OpenProcess`/`WaitForSingleObject`/`CloseHandle`）。**没有**新增网络、线程或磁盘
  写入；socket 端点只是 `/tmp` 里的一个小文件，进程退出即删。
- **无服务端时不会卡满 250 ms**：实测 78 µs——`connect()` 打到不存在的端点会立刻
  失败；上限只在"端点活着却不 accept"这种病态情形下才走满。
- **内存（第二轮）**：无累积增长（socket 与定时器都随设置进程退出消失）。

## 五、实机运行记录

1. **链路本身是好的（探针，2026-09-29）**：向 `~/.config/dsh-pet-bridge/dsh-99999.jsonl`
   合成事件后，桌宠侧状态机 6/6 全部正确迁移——`idle→thinking`（16:35:44，
   `turn/start`）、`thinking→working`（16:35:47，`tool/call`）、
   `working→waiting_approval`（16:35:50，`approval/asked`）、
   `waiting_approval→working`（16:35:53，`approval/decided`）、
   `working→success`（16:35:56，`turn/end`）、`success→idle`（16:35:59，`AgentStatus idle`）。
   结论：事件→状态这一段没坏，坏的只是"装/开/转发/概率"这四段（见 §一）。探针文件事后已删。
2. **修复前现场**：`agent_link.dsh=false`、`~/.dsh/profiles/web/package.json` 无
   `@dsh-pet/bridge`、插件事件目录为空、`report_gates.state=0.4` —— 与 §一 的
   前四条一一对应，也就是"看不见反应"的完整解释。
3. **验收时现场（主人已自己点好菜单并重启 DSH）**：`~/.dsh/profiles/{web,headless}/package.json`
   两个 profile 都有 `"@dsh-pet/bridge": "link:/home/stella/Projects/dsh-pet-indesktop/integrations/dsh-pet-bridge"`
   且 `node_modules/@dsh-pet/bridge` 符号链接可解析；`agent_link.dsh=true`；
   `ss -ltn` 显示 DSH 监听 `127.0.0.1:3080`；`~/.config/dsh-pet-bridge/dsh-103657.jsonl`
   正被实时追加（本次排查期间它记下了我自己的 `tool/call` 与 `assistant/message`）。
   即 §一 的第 1、2 条已被主人自己闭环。
4. **交付当天抓到的假离线 → 已修 → 本机复验**：第一版状态行只探默认端口 38080，
   于是上述现场被报成「没有探测到 DSH 服务」。修好后用真 `Config(Path.home()/".config")`
   （与 `app.py` 同口径）跑同一段代码路径，实测输出：

   ```
   候选端口: [3080, 38080]   在线: True
   开关    : True
   已装    : 两个 profile 均有 @dsh-pet/bridge（符号链接可解析）
   事件年龄: 0.2 秒
   → 设置页那行会是： 已连接：0 秒前收到过 DSH 事件
   → 状态属性     ： connected
   ```

5. **本机新代码的装配级证据**：`test_settings_row_shows_state_and_refreshes_on_show`
   在同口径装配下验证了整条显示路径——空 `DSH_HOME` → 「未安装」；造出已安装 + 已启用
   + 新事件 → 同一次 `showEvent` 后变成「已连接…秒前收到过事件」。
6. **仍未闭环的一步（诚实边界）**：桌宠进程需要**重启一次**才会加载本次端口修复
   （现在跑的还是旧模块），重启后设置页那行才会由「没有探测到 DSH 服务」变成
   「已连接」。之后要看的用户可见行为是：DSH 里发一条消息 → 思考气泡出现
   （本机 `report_gates.state=0.4`，若不拖回 100% 会随机少 60%）；DSH 弹审批/提问
   → 桌宠挂出常驻提醒、处理完自动收掉。这两条的实机结果补记在
   `.scratch/dsh-link/HANDOFF.md`。
7. **孤儿清理后的即时复验（17:46）**：kill 103991 后 `settings.lock` 不复存在、
   `_NET_CLIENT_LIST` 里的「桌宠设置」消失；同一分钟内桌宠日志出现
   `17:46:15 alert enqueue alertType=approval priority=1 alertId=dsh-waiting`，随后
   `17:46:35 [DSH STATE] waiting_approval -> working`——审批提醒挂出、审批回来后自动
   收掉，而且**没有重启桌宠进程**（存活轮询自己把抑制放开了）。
8. **父死自退的真机记录（本机、非 CI、非 mock）**：wrapper 活 4 秒后退出 → 设置进程
   （`settings.lock` 已于 17:49:59 建立）约 2 秒后自行退出、锁文件消失：
   `t≈2s: 已自退 ✓` / `锁文件现状: 没有那个文件或目录`。同一次运行还抓到第一版收尾
   抛的 `Internal C++ object (QTimer) already deleted` traceback（→ §三 第 10 条）。
9. **唤起通道的跨进程记录（offscreen，不占用主人屏幕）**：`.scratch/diag/verify_raise_offscreen.py`
   - A. 第二个 `python -m pet --settings` 退出码 `0`（按单实例立即退出，没开第二扇窗）；
   - B. 另一进程 `raise_existing_settings(config.dir)` → `True`，且设置进程 stderr 出现
     `This plugin does not support raise()`——offscreen 平台插件**只在 `raise()` 真被
     调用时**才打印这行，即请求确实跨进程到达 GUI 侧并执行了唤起；
   - 附注：第一次尝试落空（设置窗刚建锁、通道还没 `listen`），第 2 次成功——正是
     §七 里"点得太快会落空"那条限制。
10. **主人验收（2026-09-29）**：主人自行点击后确认「已经没问题了，完成了设置启动的
    修复」——设置界面能正常打开，本轮两条修复的用户可见目标达成。

## 六、测试与验证

- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_dsh_link_status.py tests/test_dsh_link_visibility.py -q`
  → `27 passed`（新增的两个文件）
- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_plan_ledger.py -q` → `9 passed`
- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_architecture.py -q`
  → 通过（`modern_settings_dialog.py` 实测 2347 = 预算 2347，未校准常量）
- `.venv/bin/python -m ruff check .` → `All checks passed!`
- 全量（第一轮结束时）：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
  → **`2990 passed, 21 skipped, 11 warnings in 174.41s`**
  （账目：基线 `2952 passed, 21 skipped` + 本次新增 36 条〔状态 17 + 可见性 10 + 台账 9〕
  + 2 条 = 2990。那 2 条来自 `tests/test_pr_report_discipline.py` 按"受管报告文件数"
  参数化——本报告入场后自动多出 2 条校验；无既有用例被改动、删除或转为跳过）
- 设置进程相关聚焦（第二轮）：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest
  tests/test_settings_channel.py tests/test_settings_parent_guard.py tests/test_settings_process_isolation.py -q`
  → `47 passed`
- 全量（第二轮结束后，最终）：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
  → **`3010 passed, 21 skipped, 12 warnings in 226.26s`**
  （账目：第一轮 `2990` + 第二轮新增 20 条〔唤起通道 7 + 父进程看门狗 9 + 单实例/收尾 4〕
  = 3010；无既有用例被改动、删除或转为跳过）
- 相关面回归：`tests/test_pr_report_discipline.py`、`tests/test_plan_ledger.py` 合跑
  → `26 passed`；其余（`test_agent_link.py`、`test_desktop_pet_features.py`、
  `test_settings_interaction_tabs.py`、`test_architecture.py`）已含在全量内。

## 七、已知限制与后续

- 可点击审批/问答回写不可用（DSH 0.1.7 换契约）——本次只给只读提醒，移植进台账 ①。
- `XDG_CONFIG_HOME` 一旦设置，插件（硬编码 `~/.config`）与桌宠（遵守 XDG）会指向不同
  目录而**静默零事件**；本机未设置，故先记录（台账 ②）。
- `offline` 判定无去抖（单次 socket 失败即判离线），会把正在显示的提醒清掉（台账 ③）。
- `success`/`error` 之外的独立可见反应未做（台账 ④）。
- 状态行的刷新时机是"显示时"，若对话框一直开着且 DSH 状态变化，行内容不会自己跳变
  （重新切页/重开即刷新）；刻意不做定时器以免引入常驻轮询。
- 端口候选集之外的非常规端口：若 DSH 跑在那里且**尚无任何事件**，状态行仍会报
  「没有探测到 DSH 服务」（此时还没有比端口更硬的证据）；一旦有事件就以事件为准。
- 菜单项本身仍无状态后缀（设置页才有状态行），已进台账 ⑦。
- **再点设置会落空的一瞬**：唤起通道上限 250 ms，而设置进程是"先建锁、后 `listen`"，
  所以窗口刚启动的那不到 1 秒内再点会落空（实测第 2 次即成功）。刻意不加"等窗口就绪"
  的阻塞重试——窗口本来就在出现，用户不会看不到。
- **看门狗判的是"父进程不存在"，不是"桌宠卡死"**：桌宠活着但挂起时设置进程仍会留着。
  两条更激进的方案（只在窗口**真可见**时才抑制气泡 / 不可见窗口自愈超时）本轮由主人
  明确**不做**，故不进台账。
- 屏幕堆叠顺序那一步的最终验收由主人手动完成；自动化只覆盖到"请求跨进程送达 +
  `raise()` 被调用"（offscreen 平台看不到真实堆叠顺序）。

## 八、风险与回滚

- **可逆性**：改动集中在新增模块与新增方法，不改配置结构、不做数据迁移；`git revert`
  单个提交即可完全回滚。
- **对本机外部文件零改动**：本分支没有触碰 `~/.dsh/`（profile 的安装动作要主人点击后
  才会发生），也没有改 `~/.dsh/pet.json`、`sessions/`、凭据。
- **风险 1**：提醒依赖状态边沿；若某轮 DSH 没有发出 `approval/asked`（旧版桥接），
  提醒不会出现（此时与修复前的表现一致，不会更差）。
- **风险 2**：状态行会在设置页显示本机路径事实（profile 数量、是否有事件），不含
  账号/token/邮箱等隐私字段。
- **默认行为不变**：`agent_link.dsh` 默认仍为关；`report_gates` 默认值未动。
- **风险 3（第二轮）**：设置进程多一个本机 Unix socket 与一个 2 秒定时器，都随进程退出
  消失；异常退出留下的陈旧端点由下次 `start()` 的 `removeServer` 回收——`start()` 只在
  已拿到 `settings.lock` 之后调用，此刻必然没有活着的设置进程，故不会误删在用的端点。
  两个 `stop()` 幂等 + finally 逐个兜异常，保证 `settings.lock` 一定被释放。
