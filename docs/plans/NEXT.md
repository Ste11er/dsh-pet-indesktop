# 计划台账（待办池）

本文件是**跨任务**的「以后要做什么」待办池。

## 它与断点的分工

- `.scratch/<feature-slug>/HANDOFF.md` = **断点**：我在哪停的，必须当场可恢复。
- `docs/plans/NEXT.md` = **计划项**：跨任务的未来工作。
- 两处互链，不要互相复制：断点写"当前任务的恢复位置"，计划项写"另一件还没做的事"。

## 入池方式

每轮工作收尾时，由 agent **显式加载 `grilling` skill**，按它的格式列出本轮
跳过 / 延后 / 未做的项，逐条询问是否入池（**恰好一轮**，每项附推荐答案）。
**只有用户确认的项才写入本文件。**

## 归档方式

计划项完成时，由**完成它的同一个 agent 在同一轮内**把它从本文件删除，并在
`docs/plans/DONE.md` 追加一行（完成日期 + 证据链接）。
**同一项不得同时存在于两个文件。**

## 条目格式

```
### P-<YYYY-MM-DD>-<两位序号> <标题>
- 为什么当时跳过：…
- 证据：…（file:line / 文档 / 命令输出）
- 下一步：…
```

id 必须唯一，且**绝不出现在 `DONE.md`**。

---

### P-2026-09-29-01 可点击的审批/提问气泡：桥接插件移植到 DSH 0.1.7 的 `/api/remote.mux` 新契约

- 为什么当时跳过：DSH 0.1.7-rc.2 已删除插件依赖的 `/api/events.mux`、`server-request`、`approval/requested`、`/api/respond`（插件侧 `integrations/dsh-pet-bridge/index.js:1129,1148,1151`；本机 0.1.7 只有 `.../dsh-api-gateway/lib/index.js:12` 的 `/api/remote.mux`），移植需要先逆向新帧契约，与"看得见"这件事解耦。
- 证据：`docs/DSH-BRIDGE-PET-EVENT-CONTRACT-2026-09-02.md`；探针实测（2026-09-29）证明状态链路本身正常，只有可点击交互链路失效。
- 下一步：先抓 0.1.7 的 mux 帧样本，再定新契约与回写方式。

### P-2026-09-29-02 `XDG_CONFIG_HOME` 路径不一致会导致静默零事件

- 为什么当时跳过：本机未设置该变量（`echo $XDG_CONFIG_HOME` 为空），当前两边路径一致，先记录风险。
- 证据：插件硬编码 `path.join(os.homedir(), ".config", "dsh-pet-bridge")`（`integrations/dsh-pet-bridge/index.js:96`），桌宠遵守 XDG（`pet/config.py:452-453`、`pet/dsh_state.py:163`）。
- 下一步：插件改为优先读 `XDG_CONFIG_HOME`，并补一条"两侧路径公式一致"的回归测试。

### P-2026-09-29-03 DSH 在线探测无去抖，offline 会清掉正在显示的提醒

- 为什么当时跳过：优先让"看得见"落地，抖动本身不阻塞验收。
- 证据：`pet/dsh_state.py:308-347` 无连续失败阈值（单次 socket 探测失败即 offline）；本机历史日志出现 6 秒内 `idle -> offline -> idle`；`pet/app.py:1900-1903` 每次 offline 都 `dismiss_all_interactions()`。
- 下一步：连续 N 次失败才判 offline，并让 offline 只在确认 DSH 退出时清提醒。

### P-2026-09-29-04 审批/提问以外的状态仍无独立可见反应

- 为什么当时跳过：验收口径只要求 thinking 气泡 + 审批/提问常驻提醒。
- 证据：`pet/app.py:1889-1918` 只对 `thinking`/`offline` 做事；`success`/`error` 由 agent_link 的另一条链路（`report_gates.done`/`exec_failed`）负责。
- 下一步：核实 legacy 链路对 `turn/end`、`execution/failed` 的实际覆盖后，再决定是否在状态机侧补反应。

### P-2026-09-29-05 订阅额度可选展示项与 token 自动刷新的再评估

- 为什么当时跳过：已明确不展示 credits / 约等消息数 / `model_usage`，也不代刷 token。
- 证据：`docs/CHATGPT-PLUS-QUOTA-2026-09-28-RESEARCH.md` 第 6、7 节（刷新 `refresh_token` 会改写本机 CLI 凭据）。
- 下一步：出现真实需求时再评估，并同步更新该研究档。

### P-2026-09-29-06 订阅额度研究档改名落地后，同步本台账第 5 条的证据路径

- 为什么当时跳过：改名提交（把研究档改成 `-2026-09-29-`）还在 `feat/subscription-quota` 上未推送；本分支基于 `origin/main`，仓库里实存的名字仍是 `-09-28-`，所以第 5 条按**当前实存路径**写。该提交一旦进 `main`，那条链接会指向不存在的文件。
- 证据：`git log --oneline -1 feat/subscription-quota` → `28231b1 docs(quota): 修正额度研究档日期为 2026-09-29 并同步实测全量数`；`origin/main` 无此提交（`git branch -r --contains 28231b1` 为空）。
- 下一步：该提交进 `main` 的同一轮，把第 5 条证据路径改成 `docs/CHATGPT-PLUS-QUOTA-2026-09-29-RESEARCH.md`。

### P-2026-09-29-07 右键菜单项也带上联动状态后缀

- 为什么当时跳过：本轮先做设置页状态行（主人发现"没反应"时第一眼看的是右键菜单，但菜单项当时是光秃秃的「DeepSeek Harness (DSH)」）；端到端验收还没跑，先不再加第二个呈现面，免得验收失败时多一处要排查。
- 证据：菜单项文案在 `pet/context_menus/shared.py:339-351`（无状态后缀）；状态探测已就绪：`pet/dsh_link_status.py::probe_dsh_link`（五态）+ `pet/settings_pet_controls.py::DshLinkStatusLabel`（自刷新先例）。
- 下一步：端到端验收通过后，在菜单构建时取 `probe_dsh_link` 结果，给菜单项加上「· 已连接 / · 未安装 / · 需重启 DSH」这类短后缀；注意菜单构建是热路径，只许用已就绪的廉价探测（实测 7~48 µs，见 `docs/PR-REPORT-DSH-LINK-VISIBILITY-2026-09-29.md` §四）。
