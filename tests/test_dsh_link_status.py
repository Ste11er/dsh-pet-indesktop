# -*- coding: utf-8 -*-
"""``pet/dsh_link_status.py`` 回归：DSH 联动四态的判定与"防漂移"常量。

背景（2026-09-29 实机排查）：主人报告"项目文件里说会联动 DSH，但我没见到反应"。
根因之一是链路状态**完全不可见**——插件没装、装了没重启、DSH 没跑，三种情况
在界面上长得一模一样（都没有反应）。本模块把三者区分开，这些用例钉住判定口径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pet import dsh_link_status as st


def _profile(root: Path, name: str, *, plugin: bool = True, valid: bool = True) -> Path:
    """造一个 DSH profile（含 package.json），plugin=False 表示没装桥接。"""
    profile = root / "profiles" / name
    profile.mkdir(parents=True)
    body = "not-json" if not valid else json.dumps({
        "name": f"profile-{name}",
        "dependencies": {st.PLUGIN_NAME: "link:/tmp/dsh-pet-bridge"} if plugin else {},
    })
    (profile / "package.json").write_text(body, encoding="utf-8")
    return profile


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / ".config" / "dsh-pet-standalone"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _event_file(config_dir: Path, *, age: float, now: float) -> Path:
    """造一个事件文件，mtime = now - age（桥接每次事件都会写这个文件）。"""
    bridge = config_dir.parent / st.EVENT_DIR_NAME
    bridge.mkdir(parents=True, exist_ok=True)
    path = bridge / "dsh-4242.jsonl"
    path.write_text('{"ts":1,"event":"turn/start"}\n', encoding="utf-8")
    os.utime(path, (now - age, now - age))
    return path


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------- 防漂移常量


def test_constants_match_producers():
    """常量必须与生产端/消费端一致，否则状态判定会静默说谎。"""
    from pet.agent_link import DSH_PLUGIN_NAME
    from pet.dsh_state import BRIDGE_DIR_NAME

    assert st.PLUGIN_NAME == DSH_PLUGIN_NAME == "@dsh-pet/bridge"
    assert st.EVENT_DIR_NAME == BRIDGE_DIR_NAME == "dsh-pet-bridge"
    assert st.EVENT_GLOB == "dsh*.jsonl"


def test_event_dir_matches_consumer_formula(tmp_path):
    """事件目录公式 = config.dir.parent / dsh-pet-bridge（与 dsh_state 同口径）。"""
    from pet.dsh_state import DshStateTracker

    config_dir = _config_dir(tmp_path)
    assert st.event_dir(config_dir) == config_dir.parent / st.EVENT_DIR_NAME
    assert st.event_dir(config_dir) == DshStateTracker(config_dir)._bridge_dir


# ---------------------------------------------------------------- 四态判定


def test_not_installed_when_no_profile_has_bridge(tmp_path):
    _profile(_root(tmp_path), "web", plugin=False)
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=False,
                               root=_root(tmp_path), port_online=True)
    assert status.state is st.DshLinkState.NOT_INSTALLED
    assert "未安装" in status.label
    assert status.hint


def test_installed_but_disabled(tmp_path):
    root = _root(tmp_path)
    _profile(root, "web")
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=False,
                               root=root, port_online=True)
    assert status.state is st.DshLinkState.DISABLED
    assert "未启用" in status.label


def test_installed_enabled_but_dsh_offline(tmp_path):
    root = _root(tmp_path)
    _profile(root, "web")
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=True,
                               root=root, port_online=False)
    assert status.state is st.DshLinkState.OFFLINE
    assert "DSH" in status.hint


def test_installed_enabled_offline_beats_no_events(tmp_path):
    """DSH 没在跑时不要报"重启 DSH"——那是另一回事，会把人带偏。"""
    root = _root(tmp_path)
    _profile(root, "web")
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=True,
                               root=root, port_online=False)
    assert "重启" not in status.hint


def test_installed_enabled_no_events_says_restart(tmp_path):
    root = _root(tmp_path)
    _profile(root, "web")
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=True,
                               root=root, port_online=True, now=10_000.0)
    assert status.state is st.DshLinkState.NOT_LOADED
    assert "重启" in status.hint


def test_connected_reports_event_age(tmp_path):
    _profile(_root(tmp_path), "web")
    config_dir = _config_dir(tmp_path)
    now = 10_000.0
    _event_file(config_dir, age=3.0, now=now)
    status = st.probe_dsh_link(config_dir, link_enabled=True, root=_root(tmp_path),
                               port_online=True, now=now)
    assert status.state is st.DshLinkState.CONNECTED
    assert "3" in status.label


def test_stale_event_still_connected_when_dsh_running(tmp_path):
    """有历史事件 + DSH 在线 = 插件已加载（DSH 空闲而已），不许报"需重启"。"""
    _profile(_root(tmp_path), "web")
    config_dir = _config_dir(tmp_path)
    now = 10_000.0
    _event_file(config_dir, age=st.FRESH_SECONDS + 120.0, now=now)
    status = st.probe_dsh_link(config_dir, link_enabled=True, root=_root(tmp_path),
                               port_online=True, now=now)
    assert status.state is st.DshLinkState.CONNECTED
    assert "分钟前" in status.label
    assert "重启" not in status.hint


def test_fresh_events_beat_unreachable_port(tmp_path):
    """实机 bug（2026-09-29）：主人的 DSH 跑在 3080，状态行只探 38080 →
    明明事件正实时写入，设置页却写"没有探测到 DSH 服务"。

    事件流是比端口猜测更硬的证据：刚写进事件文件只有活着的插件做得出来。
    """
    _profile(_root(tmp_path), "web")
    config_dir = _config_dir(tmp_path)
    now = 10_000.0
    _event_file(config_dir, age=4.0, now=now)
    status = st.probe_dsh_link(config_dir, link_enabled=True, root=_root(tmp_path),
                               port_online=False, now=now)
    assert status.state is st.DshLinkState.CONNECTED
    assert "4 秒前" in status.label


def test_port_probe_covers_all_candidate_ports(tmp_path, monkeypatch):
    """端口探测必须覆盖候选集（3080 真实 web 默认在列），不能只看默认端口。"""
    _profile(_root(tmp_path), "web")
    probed: list[int] = []

    def _only_3080(port):
        probed.append(int(port))
        return int(port) == 3080

    monkeypatch.setattr(st.harness_launcher, "is_running", _only_3080)
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=True, root=_root(tmp_path),
                               now=10_000.0)          # port_online=None → 真探测
    assert 3080 in probed
    assert status.state is st.DshLinkState.NOT_LOADED      # 在线但还没收到事件


def test_candidate_ports_match_tracker_set(monkeypatch):
    """与状态机同口径（pet/dsh_state.py::_candidate_ports）：含 3080/38080/DSH_PORT。"""
    assert {3080, 38080} <= set(st.candidate_ports())
    monkeypatch.setenv("DSH_PORT", "41234")
    assert 41234 in st.candidate_ports()


def test_missing_event_file_yields_no_age(tmp_path):
    assert st.latest_event_age(_config_dir(tmp_path), now=1.0) is None


# ---------------------------------------------------------------- 扫描与容错


def test_profiles_scan_ignores_non_profiles_and_bad_json(tmp_path):
    root = _root(tmp_path)
    (root / "profiles" / "node_modules").mkdir(parents=True)   # 无 package.json
    _profile(root, "web")
    _profile(root, "broken", valid=False)
    assert [p.name for p in st.real_profiles(root=root)] == ["broken", "web"]
    assert [p.name for p in st.installed_profiles(root=root)] == ["web"]


def test_probe_survives_garbage_config_dir(tmp_path):
    """探测绝不许抛异常：设置页装配失败比状态不准严重得多。"""
    status = st.probe_dsh_link(tmp_path / "nope", link_enabled=True,
                               root=tmp_path / "missing", port_online=True)
    assert status.state is st.DshLinkState.NOT_INSTALLED


def test_env_override_profile_root(tmp_path):
    custom = tmp_path / "custom-dsh"
    _profile(custom, "web")
    status = st.probe_dsh_link(_config_dir(tmp_path), link_enabled=True,
                               env={st.PROFILES_ENV: str(custom)},
                               port_online=True, now=1.0)
    assert status.state is st.DshLinkState.NOT_LOADED


# ---------------------------------------------------------------- 设置页状态行


def _dialog(tmp_path, monkeypatch):
    """按既有设置页测试的口径装配真对话框（关掉真实自启动查询）。"""
    import pet.modern_settings_dialog as settings_mod
    from PySide6.QtWidgets import QApplication
    from pet.config import Config

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    base = tmp_path / "config"
    base.mkdir(exist_ok=True)
    return settings_mod.ModernSettingsDialog(Config(base), include_ai=True)


def test_settings_row_shows_state_and_refreshes_on_show(tmp_path, monkeypatch):
    """设置页必须有一行写清联动状态，且**每次显示都重算**。

    设置对话框是应用级缓存的（只建一次），所以"装好插件 → 重启 DSH → 再打开
    设置"必须看到新状态；这里直接触发 showEvent 这条兜底路径来钉住它。
    """
    import os
    import time

    from PySide6.QtGui import QShowEvent
    from PySide6.QtWidgets import QLabel

    from pet.settings_widgets import SettingRow

    monkeypatch.setenv(st.PROFILES_ENV, str(tmp_path / "dsh-home"))
    # 状态行不传 port_online（真机要真探测）；测试里把端口探测钉成"在跑"，
    # 只验证状态判定与刷新路径，不依赖本机有没有 DSH。
    monkeypatch.setattr(st.harness_launcher, "is_running", lambda *a, **k: True)
    dialog = _dialog(tmp_path, monkeypatch)
    cfg = dialog.config
    try:
        row = dialog.findChild(SettingRow, "settingRow_dsh_link_status")
        assert row is not None, "设置页缺少「DSH 联动状态」行"
        assert dialog.agent_link_box.isAncestorOf(row), "状态行必须落在「Agent 联动」折叠框内"
        label = row.findChild(QLabel, "dshLinkStatusLabel")
        assert label is not None
        assert "未安装" in label.text()

        # 造出"已安装 + 已启用 + 刚收到事件"的全绿现场，再走一次显示路径
        _profile(tmp_path / "dsh-home", "web")
        cfg.data["agent_link"] = {**cfg.data.get("agent_link", {}), "dsh": True}
        cfg.save()
        event = _event_file(cfg.dir, age=2.0, now=time.time())
        assert os.path.getsize(event) > 0
        label.showEvent(QShowEvent())
        assert label.property("dshLinkState") == st.DshLinkState.CONNECTED.value, label.text()
        assert "秒前收到过 DSH 事件" in label.text(), label.text()
    finally:
        dialog.close()

