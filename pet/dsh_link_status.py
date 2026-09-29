# -*- coding: utf-8 -*-
"""DSH 联动状态探测（只读、无 Qt）：把"没装 / 没开 / 装了没重启 / DSH 没跑 / 已连接"
算成一句用户能看懂的话。

**为什么需要它**：主人报告"项目文件说会联动 DSH，但我没见到反应"。真机排查
（2026-09-29）发现根因之一是链路状态**完全不可见**——插件没装、装了没重启 DSH、
DSH 压根没跑，这三种情况在界面上长得一模一样（都没有反应），于是只能靠人肉
翻 ``~/.dsh/profiles/*/package.json`` 与 ``~/.config/dsh-pet-bridge/`` 才能分辨。
本模块把三者区分开，供设置页「DSH 联动状态」行显示。

**为什么单独一个纯模块**：``modern_settings_dialog.py`` 有行数预算、
``agent_link.py`` 已 5000+ 行；这里只做文件系统与端口探测，不需要 Qt，
测试可以不起事件循环直接跑。

**防漂移**：常量与生产端/消费端必须一致，``tests/test_dsh_link_status.py``
第一条用例就是钉它们——
- 插件名 ``@dsh-pet/bridge`` ↔ ``pet/agent_link.py::DSH_PLUGIN_NAME``
- 事件目录 ``<config.dir 的父目录>/dsh-pet-bridge`` ↔ ``pet/dsh_state.py::BRIDGE_DIR_NAME``
  （同时对应 ``integrations/dsh-pet-bridge/index.js`` 的 ``bridgeDir()``）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import harness_launcher

PLUGIN_NAME = "@dsh-pet/bridge"
EVENT_DIR_NAME = "dsh-pet-bridge"
EVENT_GLOB = "dsh*.jsonl"
PROFILES_ENV = "DSH_HOME"

#: 事件新鲜度阈值（秒）：超过它就不算"已连接"。桥接按 80ms 合批写入，
#: 正常刷新远快于此；放宽到 60s 是为了容忍"DSH 开着但主人没在干活"。
FRESH_SECONDS = 60.0


class DshLinkState(str, Enum):
    """联动链路的五种状态（判定顺序即 probe_dsh_link 的判定顺序）。"""

    NOT_INSTALLED = "not_installed"
    DISABLED = "disabled"
    OFFLINE = "offline"
    NOT_LOADED = "not_loaded"
    CONNECTED = "connected"


@dataclass(frozen=True)
class DshLinkStatus:
    state: DshLinkState
    label: str
    hint: str = ""

    @property
    def is_connected(self) -> bool:
        return self.state is DshLinkState.CONNECTED


def profiles_root(*, env=None, root=None) -> Path:
    """DSH_HOME（默认 ``~/.dsh``）；``root`` 供测试直接指定。"""
    if root is not None:
        return Path(root)
    source = os.environ if env is None else env
    raw = str(source.get(PROFILES_ENV) or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".dsh"


def real_profiles(*, env=None, root=None) -> list[Path]:
    """真实存在的 profile：``profiles/`` 下含 ``package.json`` 的子目录。

    与 ``pet/agent_link.py::_real_profiles`` 同口径（排除 node_modules 这类
    没有 package.json 的目录，旧版曾把它们误当 profile 去装插件）。
    """
    profiles_dir = profiles_root(env=env, root=root) / "profiles"
    try:
        children = sorted(p for p in profiles_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p for p in children if (p / "package.json").is_file()]


def profile_has_bridge(profile_dir, *, plugin: str = PLUGIN_NAME) -> bool:
    """profile 的 ``package.json`` 里声明了桥接依赖（安装的唯一判据）。"""
    try:
        manifest = json.loads((Path(profile_dir) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    deps = manifest.get("dependencies") if isinstance(manifest, dict) else None
    return isinstance(deps, dict) and plugin in deps


def installed_profiles(*, env=None, root=None, plugin: str = PLUGIN_NAME) -> list[Path]:
    return [p for p in real_profiles(env=env, root=root) if profile_has_bridge(p, plugin=plugin)]


def event_dir(config_dir) -> Path:
    """事件目录：与消费端同公式（``config.dir`` 的父目录 / dsh-pet-bridge）。"""
    return Path(config_dir).parent / EVENT_DIR_NAME


def latest_event_age(directory, *, now=None) -> float | None:
    """最新事件文件的年龄（秒）；目录不存在或没有任何事件文件返回 None。"""
    newest: float | None = None
    try:
        candidates = list(Path(directory).glob(EVENT_GLOB))
    except OSError:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    stamp = time.time() if now is None else now
    return max(0.0, float(stamp) - newest)


def candidate_ports() -> list[int]:
    """待探测的 DSH 端口，与状态机同口径（``pet/dsh_state.py::_candidate_ports``）。

    DSH 可能跑在 **3080**（web 真实默认；`pet/harness_launcher.py` 的注释说明
    3080 只是 Windows 上不宜*绑定*、作为客户端连接没问题）或 38080（桌宠
    启动时的避让端口），也可能由 `DSH_PORT` 指定——全部探测，任一在线即在。

    为什么必须复用同一套：**上一版只探了默认端口 38080**，于是主人正通过
    3080 上的 DSH 下指令，设置页却写着"没有探测到 DSH 服务"（2026-09-29 实机）。
    3080 排最前，因为它是真实 web 默认、命中率最高。
    """
    ports: list[int] = [3080]
    for raw in (*harness_launcher._candidate_ports(), os.environ.get("DSH_PORT"), 38080):
        try:
            port = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if port not in ports:
            ports.append(port)
    return ports


def any_port_online(ports=None) -> bool:
    """候选端口任一在线即视为 DSH 在线；异常绝不外抛。"""
    for port in (candidate_ports() if ports is None else ports):
        try:
            if harness_launcher.is_running(int(port)):
                return True
        except Exception:
            continue
    return False


def link_enabled_from_config(config) -> bool:
    """桌宠侧联动开关（``agent_link.dsh``）：失败一律当"没开"。"""
    try:
        section = config.get("agent_link", {}) or {}
    except Exception:
        return False
    return bool(section.get("dsh")) if isinstance(section, dict) else False


def probe_dsh_link(config_dir, *, link_enabled: bool, env=None, root=None, now=None,
                   port_online: bool | None = None) -> DshLinkStatus:
    """按"最可能的原因优先"判定链路状态；任何 IO 异常都不外抛。

    ``port_online`` 显式给出时不探测端口（测试用）；否则探测候选端口集。
    """
    try:
        installed = installed_profiles(env=env, root=root)
    except Exception:
        installed = []
    if not installed:
        return DshLinkStatus(
            DshLinkState.NOT_INSTALLED,
            "未安装：DSH 的 profile 里还没有桥接插件",
            "在设置页或右键菜单打开「DSH 联动」会自动安装；装完需要重启一次 DSH。",
        )
    if not link_enabled:
        return DshLinkStatus(
            DshLinkState.DISABLED,
            "已安装，但桌宠侧的 DSH 联动未启用",
            "打开「Agent 联动 → DeepSeek Harness (DSH)」后，桌宠才会对 DSH 起反应。",
        )
    age = latest_event_age(event_dir(config_dir), now=now)
    if age is not None and age <= FRESH_SECONDS:
        # 刚写进事件文件只有活着的插件做得出来——这比端口猜测更硬，
        # 因此放在端口探测之前：DSH 跑在非常规端口时也不许假报离线。
        return DshLinkStatus(DshLinkState.CONNECTED, f"已连接：{age:.0f} 秒前收到过 DSH 事件")
    online = any_port_online() if port_online is None else bool(port_online)
    if not online:
        return DshLinkStatus(
            DshLinkState.OFFLINE,
            "已安装并已启用，但没有探测到 DSH 服务",
            "DSH 没在运行（或在候选端口 3080/38080 之外）。启动 DSH 后本行会自动变回「已连接」。",
        )
    if age is None:
        return DshLinkStatus(
            DshLinkState.NOT_LOADED,
            "已安装并已启用，但还没收到任何 DSH 事件",
            "插件要重启一次 DSH 才会被加载：重启后本行会变成「已连接」。",
        )
    # 有历史事件 + DSH 在线 = 插件确实加载过；只是这一阵没有活动（空闲正常）。
    return DshLinkStatus(
        DshLinkState.CONNECTED,
        f"已连接：最近一次 DSH 事件在 {age / 60:.0f} 分钟前（DSH 空闲时正常）",
    )
