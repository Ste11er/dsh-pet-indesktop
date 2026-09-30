# PR 报告：ncm-cli（网易云音乐 CLI/TUI）自动唱歌 + 歌词气泡（Linux）

> **基线**：`3cdfe98`（Merge PR #1: feat/subscription-quota）
> **分支**：`main` 工作区　**日期**：2026-09-30
> **范围**：5 个文件（实现 4、测试 2，其中新增 2）+ 本报告
> **关联**：`docs/PR-REPORT-music-lyric-2026-09-16.md`（歌词显示首版）、
> `docs/NETWORK-PROXY-AND-VPN-2026-09-22.md`（网络边界口径）

---

## 一、核心特性

Linux（Wayland/X11）上桌宠此前**完全没有**音乐联动：唱歌检测（pycaw 音频
峰值）与歌词链路（winrt SMTC）都是 Windows 专属，Linux 上恒为"没在放歌"。
本 PR 以用户本机的 **ncm-cli**（网易云音乐 CLI/TUI，v0.1.7，Node 实现）为
Linux 播放来源，补齐"放歌 → 自动唱歌 + 歌词气泡"整条链路，并按用户明确
要求收紧语义：**无歌词的歌不出任何气泡（连标题也没有），但照唱**。

实测前提（2026-09-30 本机探测，全部有输出为证）：`ncm-cli state` 是本地
daemon Unix socket IPC（断网输出不变），CLI 与 TUI 共享同一 daemon，外部
`state` 实时看到 TUI 播放；单次子进程 296~336ms（Node 冷启动主导）；
`title` 是"歌名 - 歌手"拼串、无歌曲 ID，加密 ID 只在 daemon 存活期间的
`~/.config/ncm-cli/play-session.json`；`pause` 后 status 也是 `stopped`；
`song lyric --songId` 网络取词 ~1.0-3.3s，带 `noLyric`/`pureMusic` 一等标志
（纯音乐两者常同时为 true）。

| # | 能力 | 说明 |
|---|---|---|
| 1 | Linux 唱歌检测 | `music_detect.is_music_playing()` Linux 分支读 ncm 共享采样线程快照（3s 一拍），覆盖 CLI 与 TUI |
| 2 | Linux 曲目/进度 | `now_playing.get_now_playing()` Linux 分支返回 ncm `Playback`（position 是 daemon 真值且实时上涨，比 Windows 网易云的恒 0 好） |
| 3 | ncm 精确取词 | 带 `song_id` 的取词走 `ncm-cli song lyric`（加密 ID 精确命中）；**失败只写日志，不做三源兜底、不重试**（用户决策） |
| 4 | 无词不出气泡（全局语义变更） | 无词歌：照唱、零气泡（旧版常驻"我在唱《》"标题的行为废除，Windows 连带变更）；纯音乐（`pureMusic` 优先于 `noLyric`）：不唱 +"正在听"+提示（现状） |
| 5 | 零成本空闲 | daemon socket 不存在时不起任何子进程（避免每 3s 复活退出的 daemon）；两开关（唱歌/歌词）都关时无采样线程 |

**红线 / 不变量**：

- Windows 链路（pycaw 峰值 + SMTC）一字不动，仅新增 Linux 分支分派；
- `MusicLyricController` 的让路/优先级/宽度锁/暂停冻结等既有语义不变；
- GUI 线程绝不同步跑 ncm 子进程（313ms 会卡窗口一拍）；
- 不碰 `mpv.sock`（ncm-cli 内部实现细节，版本一变就碎）。

## 二、修改文件说明

### 实现

| 文件 | 增删 | 改动意图 |
|---|---|---|
| `pet/ncm_player.py` | +460（新增） | ncm-cli 播放来源整模块：`_sample_once()`（socket 门控→`ncm-cli state`→解析→`Playback`，`title` 按 `" - "` 拆歌名/歌手）；`NcmSampler` 常驻 daemon 采样线程（3s 一拍、进程级单例、快照读 `is_playing`/`current_playback`、`prime()` 保证首读不空窗）；`fetch_lyrics_via_ncm(song_id)`（`song lyric` 子进程→`Lyrics`：LRC 解析/`pureMusic`→instrumental/**优先于**`noLyric`/仅 txtLyric 视同无词/失败 None 只写 WARNING）；`_reset_module_state_for_tests()` 测试隔离钩子 |
| `pet/music_detect.py` | +28/−8 | `is_music_playing()` 平台分派：win32→pycaw 峰值（原逻辑不动）；linux→`ncm_player.current_playback_playing()`（快照读，必要时拉起共享采样器）；其余平台 False 不变 |
| `pet/now_playing.py` | +23/−0 | `Playback` 增 `song_id: str = ""` 字段（ncm 加密 ID，取词精确入参；Windows SMTC 恒空串，向后兼容）；`get_now_playing()` Linux 分支 → `_ncm_get_now_playing()` 读共享快照；win32 原路径一字不动 |
| `pet/music_lyric_controller.py` | +38/−19 | ①无词语义：`_on_lyrics_ready` 查不到→只记 `_no_lyric_keys` 不显示（删掉"标题继续常驻"分支）；`_announce` 不再即时亮标题（删 `_show` 调用，只建立内部状态）；`_on_playback_ready` 取词在途不再显示标题。②ncm 取词分支：`_fetch_worker` 增 `song_id` 参数，非空走 `ncm_player.fetch_lyrics_via_ncm`（异常 WARNING 不兜底），空走原三源；`_start_track` 从 `playback.song_id` 透传 |

### 测试

| 文件 | 增删 | 覆盖 |
|---|---|---|
| `tests/test_ncm_player.py` | +447（新增） | 24 用例：state 解析/`" - "` 拆分/stopped 快照、socket 门控（含 XDG）、play-session.json ID 读取（缺失/损坏/非 hex）、取词全分支（LRC/pureMusic/noLyric/**pureMusic+noLyric 同时 true 判纯音乐**/仅 txtLyric/失败/超时钳制/非法 ID 不起子进程）、采样线程生命周期/异常存活/幂等、模块状态隔离 fixture、平台分派（music_detect Linux 快照、单例不重建、now_playing 派发） |
| `tests/test_music_lyric.py` | +109/−10 | 语义翻转 4 处旧断言（切歌空窗不出气泡、无词不出气泡、首次边界只建内部状态、取词中不出气泡）+ 新增 3 例：ncm Playback 端到端（song_id 透传→装词→标题+首句气泡）、ncm stopped 快照冻结进度（同曲不清状态、tracker 进入 paused） |

### 未改动（看起来相关但故意没动）

- `pet/window.py` / `pet/window_alerts.py`：唱歌定时器/宽限期/无缝续播链路
  原样复用（`check_music_sing` 只换了 `is_music_playing` 的数据来源，该函数
  本身零改动）；行数预算红线不新增字段。
- `pet/speech_bubble.py`：气泡的宽度锁/高度棘轮/翻页全复用，无改动。
- `pet/settings_*` / `pet/config.py`：零新增设置项（复用 `music_sing_enabled`
  / `music_lyric_enabled`），无迁移负担。
- `pet/context_menus/`：菜单播控（ncm 已具备 pause/next/prev）与「打开网易云」
  Linux 适配列为二期（用户确认）。

## 三、实现要点

**为什么是"每 3 秒一个子进程"而不是直连 daemon socket**：ncm-cli 的
daemon 协议未公开且代码混淆（javascript-obfuscator，静态逆向不可行）；
`ncm-cli state` 是唯一稳定公共接口。313ms 的子进程成本在后台 daemon 线程
消化，GUI 线程只读内存快照（实测 100 次读 0.3ms）。mpv.sock 虽可亚毫秒读
进度，但拿不到曲名，且属 ncm-cli 内部细节，明确不碰。

**为什么暂停用 stopped+冻结 兜住而不是精确识别**：ncm-cli 的 status 只有
playing/stopped 两值（pause 后也是 stopped、position 冻结）。区分"暂停"
与"播完"对唱歌不重要：6 秒宽限期天然吸收（暂停 <6s 恢复不退出唱歌，
≥6s 退出）；对歌词重要：带 title 的 stopped 快照仍产 `playing=False` 的
Playback，控制器走既有"同曲暂停冻结"分支，恢复播放接得上。

**取词失败的哲学（用户决策）**：ncm 取词失败/查 ID 失败只写 WARNING
日志，本会话这首歌当无词处理，不做三源网络兜底、不重试。确定性无词
（`noLyric`）与纯音乐（`pureMusic`）是服务端一等信号，与瞬时失败严格
区分：前者本会话不再查，后者也只是当次失败。

**pureMusic 优先于 noLyric**：实测纯音乐（风之谷钢琴、晴天钢琴版）两个
标志同时为 true。若先判 noLyric 会把钢琴曲判成"无词歌"——桌宠对着钢琴曲
做唱歌动画，破坏"纯音乐不唱"红线。实机发现后已修（有专项回归测试）。

**`_announce` 不再即时亮标题的原因**：有没有词要等取词落地（1~3.3s）才知道。
旧版切歌先亮"我在唱《》"，若结果是纯音乐再换"正在听《》"；新版严格口径下
无词歌连标题都不能出，而空窗期未知结果，故不显示。纯音乐模式（确定性
结论落地后）仍即时显示。

## 四、性能分析

**方法（可复现）**：见下各命令。环境：Ubuntu (linux 6.0.0-31-generic) /
Python 3.11 (.venv) / ncm-cli 0.1.7 (Node) / Wayland。

| 指标 | 实测 | 归属 |
|---|---|---|
| `ncm-cli state` 单次子进程 | 296/300/312/321/336ms（5 样本，均 313ms）；daemon 热：0.29/0.29/0.32s | 新增路径（采样线程内） |
| 采样器稳态 CPU（播放中，60s 窗口） | 0.02s CPU / 60s wall = **0.0% 单核**（隔离进程实测：仅采样器无 GUI） | 新增 |
| 采样器内存 | RSS 26.5MB（隔离进程，含 Python 解释器） | 新增 |
| GUI 线程读快照 | 100 次 `is_music_playing()` 共 **0.3ms**（3µs/次） | 新增（唱歌 1s 定时器内） |
| 空闲（daemon 退出后） | socket 门控拦截，`sample_now()` 0.000s×3，**零子进程**；daemon 停止后 ~30s 自动退、socket 删除，轮询不再拉活 | 新增 |
| 取词 `ncm-cli song lyric` | 1.04~2.15s（5 首：1.04/1.05/1.06/1.13/1.50/2.15s），一次性 daemon 线程，每首歌一次 | 新增 |
| 桌宠整进程（对照） | 60s 播放中 CPU 973 ticks≈16% 单核（动画渲染主导，与基线同量级）；RSS 229MB | 既有 |

**结论**：①稳态开销：播放期间采样线程 0.0% 单核（313ms/3s 的子进程等待
几乎全是 I/O 等待，不烧 CPU）；空闲期间零开销（socket 门控）。②新增路径
成本：state 313ms/3s（后台线程）、取词 ~1-3s/首歌（一次性线程，切歌才
触发）；GUI 线程无任何新增阻塞点（快照读 3µs）。③新系统调用：每 3s 一次
`fork+exec node`（播放期间）+ 一次 socket 连接；每首歌一次网络取词；
空闲时无。④内存：+26MB（采样器线程 + 模块）；无缓存增长（ncm 取词
不走磁盘缓存，`_no_lyric_keys` 每会话有限条目）。

## 五、实机运行记录

环境：本机 Ubuntu + Wayland，ncm-cli 0.1.7 已登录，`music_sing_enabled`
与 `music_lyric_enabled` 均开。TUI 以 `tmux new-session -d -s ncmprobe
'ncm-cli tui'` 启动（自动拉每日推荐开始播放）。

**根因/前提现场**：改动前 Linux 上 `music_detect.is_music_playing()` 恒
False（`tests/test_music_detect.py::test_non_windows_returns_false` 钉住
的旧行为），桌宠无任何音乐反应——本 PR 前 Linux 音乐联动为零。

**四场景端到端**（临时 TEMP-PROBE 日志注入 `check_music_sing` 观察内部
状态，验证后已移除；气泡以 X11 窗口枚举证实——气泡是独立 Tool 窗口，
出现/尺寸可观测）：

1. **有词歌**（GLASSY SKY - Donna Burke）：取词 71 行 1.06s；气泡窗口
   548x88 出现且 5 秒后仍在（每拍续期）；`active=True` 持续（唱歌中）。
   晴天 - Jay：16 行 2.15s，气泡 548x132，`active=True`。
2. **无词歌**（Memory_α、夏夜、烟花和你）：取词 0 行（1.13s/1.50s）；
   **气泡窗口不存在**（仅桌宠主窗 922x562 与灵动岛 32x128）；
   `active=True`（照唱）。——用户核心要求的严格验证。
3. **纯音乐**（风之谷 钢琴）：取词 0 行（纯音乐）1.10s；
   `instrumental` 守卫生效（TEMP-PROBE 停止打印 = 在 instrumental 分支
   提前 return，桌宠不唱）；「正在听」气泡 548x132 在显。切回有词歌后
   `active` 恢复 True（随机动画结束后进入唱歌，与 Windows 既有节拍一致）。
4. **暂停**（`ncm-cli pause`）：`playing=False` 后 `active=True` 保持
   10s（宽限期内），随后 `active=False`（退出唱歌）；恢复播放接上。

**pureMusic 优先级缺陷的实机发现与修复**：首版先判 `noLyric`，实机放
晴天钢琴版（`noLyric:true + pureMusic:true`）时桌宠仍在唱——判成了无词歌。
修正判定顺序（pureMusic 优先）后实机复测风之谷钢琴：不唱 + 正在听，
符合语义。专项回归测试
`test_fetch_ncm_lyrics_pure_music_wins_over_no_lyric` 钉住。

**边界与失败路径**：daemon 退出后（stop + 30s）socket/play-session.json
被删，`sample_now()` 三次全 0.000s 零子进程——门控真实生效；TUI 场景
`queueLength` 恒 0 的 ncm-cli 已知怪癖不影响本链路（只用 status/title/
position）。

**无法自动验证的能力与排查证据**：真实气泡的可见内容与歌词逐句滚动无法
在 CI/无头环境断言（Wayland 禁止跨进程截屏：`xwd` 对 ARGB Tool 窗口返回
0 字节、`gnome-screenshot` 在 Wayland 下 GdkPixbuf 断言失败、Qt
`grabWindow` 返回 null——三种探针均试过）。改用：气泡窗口的存在性/
几何（X11 枚举）+ 桌宠自身日志（取词行数、instrumental 标志）+
`check_music_sing` 内部状态探针三者交叉证实。唱歌动画无法从外部区分
素材（同分辨率 640x360），由 `_music_sing_active` 探针直接证实。

## 六、测试与验证

| 门 | 命令 | 结果 |
|---|---|---|
| 静态检查 | `python -m ruff check pet tests scripts` | All checks passed |
| 聚焦 | `pytest -q tests/test_ncm_player.py tests/test_music_lyric.py tests/test_music_detect.py tests/test_music_sing_grace.py tests/test_music_sing_timer.py tests/test_now_playing_session.py tests/test_menu_layout.py tests/test_window_pause.py` | 212 passed |
| 受影响时序族满载 3 遍（4 核 spin 负载） | `pytest -q tests/test_ncm_player.py tests/test_music_sing_timer.py tests/test_music_sing_grace.py tests/test_music_lyric.py` | 117/117/117 passed（1.91/1.99/1.86s） |
| 全量 | `python -m pytest -q` | **2979 passed, 21 skipped**（0 failed） |
| 断言有效性 | 实机 pureMusic 缺陷即"注入缺陷"实证：首版判序被实机场景击穿（对着钢琴曲唱），测试同步补 `pure_music_wins_over_no_lyric` 后红→绿 | 已证 |

测试期间发现并修复的两个真实缺陷（均先红后绿）：
①`_NCM_CONFIG_DIR` 缓存与 `_sampler` 单例跨用例泄漏（全量套件稳定红、
单跑永远绿）——加 `_reset_module_state_for_tests()` + autouse fixture；
②采样器首读空窗（刚 start 读到 None 快照会把刚建立的状态误清）——加
`prime()` 阻塞等首拍（实测首读 0.30s，之后 3µs）。

## 七、已知限制与后续

- **二期（用户确认入计划）**：①通用 Linux 检测（MPRIS/脉冲音频峰值，
  覆盖 Firefox 等任意播放源）；②右键菜单播控（ncm `pause/next/prev`，
  Linux 上现有音乐子菜单因 SMTC 不可用而整体隐藏）；③翻译歌词
  （`transLyric`）显示。
- 切歌识别延迟 ≤3s（轮询粒度）；开口唱歌额外受随机动画结束时机影响
  （既有行为，Windows 同款）。
- `state.title` 拆分按第一个 `" - "`：歌手名含 `" - "` 的极少数曲会拆偏
  （歌名正确，仅 artist 字段不准，无功能影响）。
- ncm 取词不走磁盘缓存（三源缓存机制属 Windows 路径）；同一首歌重播
  会再次取词（~1-3s，daemon 线程，无感知）。`_no_lyric_keys` 防的是
  查不到的重复请求。
- TUI 运行时外部 `ncm-cli play` 被 ncm-cli 自身拒绝（不影响本功能，
  本功能只读不控）。

## 八、风险与回滚

- **影响面**：Linux 平台新增能力 + 全平台"无词歌不出气泡"语义变更
  （Windows 连带：查无歌词的歌不再常驻"我在唱《》"标题——用户明确批准）。
- **开关**：`music_sing_enabled` / `music_lyric_enabled` 两开关即总闸；
  ncm-cli 未安装/未跑时 socket 门控静默无检测，开关无害。
- **配置迁移**：零新增持久化键。
- **回滚**：`git revert` 即可，无落盘残留（ncm 取词不写缓存；
  `lyrics_cache/` 仅 Windows 三源路径使用）。
