# Linux 开发环境（uv 管理 .venv）与平台适配记录

**基线**：`main`，2026-09-22，本机实测（Ubuntu 系 Linux，`uv 0.12.17`，CPython 3.11.16 由 uv 管理，
PySide6 6.11.2，`QT_QPA_PLATFORM=offscreen`）。

本文记录两件事：
1. **Linux 上怎么一键建开发环境**（uv + 锁文件 + 系统库），以及为什么这么设计；
2. 为让套件在 Linux（含只读 HOME / 容器 / 文件沙箱）下**真绿**所做的适配，逐条附根因。

适用范围：Linux 开发机与 CI `ubuntu-latest`。Windows/macOS 的既有路径（`pip install -r requirements.txt`、
`run.bat`）不受影响——`requirements.txt` 仍是唯一权威依赖清单。

---

## 1. 一键建环境

```bash
scripts/setup_dev_env.sh            # 建/更新 .venv（幂等）
scripts/setup_dev_env.sh --relock   # 改过 requirements.txt 后重新解析锁文件
```

脚本做四件事：

| 步骤 | 命令 | 说明 |
|---|---|---|
| 解释器 | `uv python install --no-bin 3.11` | 版本取自 `.python-version`，**与 CI `pr-test.yml` 的 matrix 一致** |
| 锁文件 | `uv pip compile requirements.txt -o requirements.lock --universal` | 仅 `--relock` 或锁文件缺失时执行 |
| 环境 | `uv venv --allow-existing .venv` + `uv pip sync requirements.lock` | 严格按锁同步（会移除多余包） |
| 自检 | `QT_QPA_PLATFORM=offscreen python -c "import PySide6"` | 失败时打印下面那条 apt 安装命令 |

日常命令：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q   # 全量测试
.venv/bin/python -m pet                                   # 启动桌宠（需要图形会话）
.venv/bin/ruff check pet/ tests/                          # 静态检查
```

### 1.1 为什么 Python 版本钉 3.11

CI 三平台都跑 3.11。本仓库有一条成文纪律：**报红之前先确认本机环境与 CI 等价**
（见 [`BUILD-CI-FAILURE-NOTES-2026-08.md`](BUILD-CI-FAILURE-NOTES-2026-08.md) 第 2.4 节）。
把 `.python-version` 钉到 3.11 就是这条纪律的机器化——本机与 CI 同版本，避免「本机红 CI 绿」。

同时，套件**也**在新解释器上跑得通：本次已在 CPython 3.14.6 上全量复跑并通过
（唯一一个版本敏感的用例已按 §3.1 修成版本无关，两个解释器均 2761 passed / 0 failed）。
所以想用系统 Python 3.13/3.14 也可以：
`uv venv --python 3.14 .venv && uv pip sync --python .venv/bin/python requirements.lock`。

### 1.2 为什么 uv 的缓存/解释器目录落在仓库内

uv 默认写 `~/.cache/uv` 与 `~/.local/share/uv`。在只读 HOME、容器、或**文件沙箱**（本次开发环境
就是这种：仅工作区可写）里，那两处会直接 `EROFS (os error 30)`，连 `uv venv` 都起不来。
脚本因此默认：

```
UV_CACHE_DIR=$ROOT/.uv-cache          # .uv-cache 自带 .gitignore（内容 *）
UV_PYTHON_INSTALL_DIR=$ROOT/.uv-python
```

两者都已进仓库 `.gitignore`。要共用全局缓存时显式覆盖环境变量即可：

```bash
UV_CACHE_DIR=$HOME/.cache/uv UV_PYTHON_INSTALL_DIR=$HOME/.local/share/uv scripts/setup_dev_env.sh
```

`uv python install --no-bin` 的 `--no-bin` 是必要的：默认还会往 `~/.local/bin` 建 `python3.11`
软链，在只读 HOME 下会失败（不致命，但会打一条 warning 噪音）。

### 1.3 为什么锁文件不取代 requirements.txt

`requirements.txt` 是**唯一权威清单**：`.github/workflows/pr-test.yml`、三条 `build-*.yml`、
`scripts/build_*.sh` 与 README 都按它装依赖，`docs/BUILD-CI-FAILURE-NOTES-2026-08.md` 明确写了
「合并新增依赖的 PR 后先同步依赖再判定红」。

`requirements.lock` 只是它的 `--universal` 解析产物（保留 `sys_platform == 'win32'` 等平台标记，
故三平台共用同一份），用于本地可复现安装。**依赖变更的正确姿势**：改 `requirements.txt` →
`scripts/setup_dev_env.sh --relock` → 两个文件一起提交。

## 2. Linux 系统库

PySide6 在 Linux 上 import 需要一组系统库（与 CI 一致）：

```bash
sudo apt-get install -y libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 \
                        libfontconfig1 libdbus-1-3 fonts-noto-cjk
```

- 前五个是 Qt 的平台插件/字体依赖；`libdbus-1-3` 供托盘与系统集成；
  `fonts-noto-cjk` 保证中文文案不豆腐块。
- 无显示器环境（服务器、容器、CI）用 `QT_QPA_PLATFORM=offscreen`；
  测试还需 `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` 避免探测真实钥匙串。

## 3. 本次为 Linux 做的适配

Linux 上套件原先有 7 条红。逐条复现后分三类：**测试未隔离机器全局状态**（4 条）、
**解释器版本敏感**（1 条）、**测试假设了可写 HOME**（4 条里含此因）。全部修在测试/实现侧，
不改任何产品语义。

### 3.1 `_safe_is_dir` 改为显式 `Path.stat()`（Python 3.13+ 兼容）

`tests/test_agent_link_dep_specs.py::test_safe_probes_swallow_permission_errors` 打桩 `Path.stat`
抛 `PermissionError`，断言 `agent_link._safe_is_dir()` 返回 False。

Python 3.13+ 的 `Path.is_dir()` 内部改走 `os.stat`，**不再经过 `Path.stat`**，打桩失效 → 用例红
（3.11/3.12 上绿）。产品实现改为显式 stat，语义不变（都跟随符号链接、都抛 `OSError`）：

```python
try:
    return stat.S_ISDIR(path.stat().st_mode)
except OSError:
    return False
```

收益：这条「目录判定不得让权限/竞态错误逃逸」的防线在任何 Python 上**可测且行为一致**。

### 3.2 测试不再依赖真实 HOME 可写

`tests/test_click_sound.py` 的 4 条用例经 `_cache_path` → `_sound_cache_dir()` →
`QStandardPaths.AppDataLocation`，在 Linux 上落到 `$XDG_DATA_HOME` / `~/.local/share`。
只读 HOME / 容器 / 沙箱里那一步直接 `EROFS`，而这 4 条断言的是缓存**键**语义与播放路径，
与缓存落在哪个真实目录无关。修法：文件级 autouse fixture 把 `click_sound._sound_cache_dir`
固定到 `tmp_path/sounds_cache`。

### 3.3 测试不再依赖「本机没装全局 dsh」

两条用例断言「PATH 上没有 dsh 时的回退行为」，但本机装了全局 dsh，两条独立路径都会把它捞出来：

- `test_harness_launcher.py::test_find_launch_command_fallback_without_dsh`：
  `harness_launcher._which` 用的是**增强 PATH**（含 `~/.local/bin`），只 `monkeypatch.setenv("PATH", ...)`
  挡不住；且 `_npm_global_roots()` 还会扫真实家目录的静态候选根（`~/.local/lib/node_modules`）。
  修法：直接打桩 `hl._which` 与两个候选根函数。
- `test_windows_node_env.py::TestHarnessGlobalRoots::test_manual_launch_finds_dsh_under_nvm_windows_root`：
  该用例原本只清 `node_runtime._WINDOWS_NODE_MODULES` 常量——在 Linux 上无效（走 `_POSIX_*` 分支），
  真实 dsh 照样漏进来。修法：补上 `hl.static_node_modules_roots` 打桩（平台无关）。

两条都只在「开发机装了全局 dsh」时暴露，CI 机器没有全局 dsh，所以此前一直是绿的。

### 3.4 `XDG_CONFIG_HOME`：配置根遵循 XDG Base Directory

`pet/config.py::_default_base()` 原先在 Linux 上硬编码 `~/.config`，而
`pet/autostart.py` 的 Linux 自启目录**已经**读 `XDG_CONFIG_HOME`——两者不一致。现在：

```python
xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
return Path(xdg) if xdg else Path.home() / ".config"
```

未设置时行为完全不变（老用户配置不搬家），设置时遵循规范。护栏见
`tests/test_config_schema.py::test_default_base_honors_xdg_config_home_on_linux` 与
`::test_default_base_keeps_windows_and_macos_layouts`（Windows 看 `%APPDATA%`、macOS 看
`Library/Application Support`，均不受 XDG 影响）。

## 4. Linux 实机验收

**启动冒烟**（本机实测，`XDG_*` 指向临时目录、offscreen）：

```bash
XDG_CONFIG_HOME=$PWD/.smoke/config QT_QPA_PLATFORM=offscreen timeout 25 .venv/bin/python -m pet
```

结果：进程持续运行到超时被杀（无崩溃、无 traceback），日志显示完整启动链路——

```
启动 (slot: 0, instance: )
当前形象: shenshen
素材加载完成：shenshen 106 段动画
恢复位置 screen= avail=(0,0,799,799) dpr=1.0 -> (467,541)
winmm.dll 不可用（非 Windows 或加载失败）→ wav 播放回退 Qt 路径
[VIS] 桌宠显示 anim=待机呼吸休闲
灵动岛碰撞体已启动（同步硬墙，无 30Hz 检测）
进入事件循环
```

同时确认：QtMultimedia 用 FFmpeg 7.1.5 后端（音频可用）、配置落在
`$XDG_CONFIG_HOME/dsh-pet-standalone/`（§3.4 生效）。offscreen 平台打印的
`does not support setting window masks / raise()` 属平台插件噪音，非产品问题。

**套件**（两个解释器各跑一遍全量，均零失败）：

| 解释器 | 结果 | 命令 |
|---|---|---|
| CPython 3.11.16（uv 管理，与 CI 一致） | **2761 passed / 21 skipped / 0 failed**（187.9s） | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` |
| CPython 3.14.6（系统解释器） | **2761 passed / 21 skipped / 0 failed**（183.5s） | `uv venv --python 3.14 /tmp/v314 && uv pip sync --python /tmp/v314/bin/python requirements.lock && QT_QPA_PLATFORM=offscreen /tmp/v314/bin/python -m pytest -q` |

修改前基线（同一台机器、同一套件）：3.14 上 **7 failed**、3.11 上因环境缺失无法跑；
7 条红的根因就是 §3.1–§3.3 那三类，现已全部消除。

## 5. 仍然存在的平台差异（有意保留）

| 能力 | Linux 行为 | 原因 |
|---|---|---|
| `winmm` 点击音效 | 回退 Qt 播放路径 | Windows 专属后端；日志有明确提示，非降级缺陷 |
| 音乐歌词（SMTC） | 不可用，设置页开关置灰并说明 | 依赖 Windows 系统媒体控制（`winrt-*`） |
| 托盘图标 | 依赖桌面环境系统托盘（GNOME 需 AppIndicator 扩展） | README 已注明 |
| 自启 | XDG autostart `.desktop` | `pet/autostart.py` 既有实现 |

## 6. 相关文档

- [`../README.md`](../README.md) —— 用户/贡献者主说明（含 Linux 从源码运行段落）。
- [`BUILD-CI-FAILURE-NOTES-2026-08.md`](BUILD-CI-FAILURE-NOTES-2026-08.md) —— 「本机红 CI 绿」的环境等价纪律。
- [`DEV-HANDOVER.md`](DEV-HANDOVER.md) —— 开发交接总览。
- [`ONEDIR_PACKAGING.md`](ONEDIR_PACKAGING.md) —— Linux 打包流水线（`scripts/build_linux.sh`）。
