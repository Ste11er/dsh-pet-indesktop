# -*- coding: utf-8 -*-
"""声明式 Agent 注册表 + 新内置 Agent（Kimi / ZCode）接入测试。

覆盖：
- 注册表是配置默认值 / 清洗白名单 / 监视器装配 / 菜单 / 卸载清理的单一事实来源；
- 通用 hook 写入器落地的脚本真能把宿主事件写进统一协议文件（真子进程验证）；
- 通用接入助手 CLI（自定义通道）生成的命令真能写事件，且拒绝非法/内置 key；
- Kimi [[hooks]] 与 ZCode hooks.events 的注入幂等、用户配置保留、卸载只删自己的条目；
- 宿主 schema 红线：Kimi Code strict schema 的键/事件/timeout 自校验。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from pet.agents import kimi as kimi_mod
from pet.agents import zcode as zcode_mod
from pet.agents.hook_writer import ensure_event_hook, events_file_for
from pet.agents.registry import (
    agent_display_names,
    agent_spec,
    agent_specs,
    builtin_agent_keys,
    extra_event_states,
    monitor_factory,
    resolve_ref,
    uninstallable_specs,
)
from pet.config import _clean_agent_link_data, _clean_custom_agents, _default_agent_link_data


# ============================================================================
# 1. 注册表作为单一事实来源
# ============================================================================
class TestRegistryIsSingleSource:
    def test_builtin_keys_match_specs_and_config_defaults(self):
        keys = builtin_agent_keys()
        assert keys == tuple(spec.key for spec in agent_specs())
        defaults = _default_agent_link_data()
        for key in keys:
            assert defaults[key] is False, f"{key} 缺少默认开关"
        # 本次接入的两家必须在默认值里
        assert {"kimi", "zcode"} <= set(keys)

    def test_cleaner_keeps_builtin_switches(self):
        cleaned = _clean_agent_link_data({"kimi": True, "zcode": True})
        assert cleaned["kimi"] is True
        assert cleaned["zcode"] is True

    def test_custom_agent_key_may_not_shadow_builtin(self):
        """自定义通道不得占用内置键（否则菜单/监视器注册撞车）。"""
        raw = [
            {"key": "kimi", "name": "Fake Kimi", "path": "~/x.jsonl"},
            {"key": "myagent", "name": "Mine", "path": "~/y.jsonl"},
        ]
        cleaned = _clean_custom_agents(raw)
        assert [item["key"] for item in cleaned] == ["myagent"]

    def test_display_names_cover_every_spec(self):
        names = agent_display_names()
        assert names["kimi"] == "Kimi Code"
        assert names["zcode"] == "ZCode"
        assert names["dsh"] == "DSH"
        assert set(names) == set(builtin_agent_keys())

    def test_every_monitor_factory_builds_a_monitor(self, tmp_path):
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        built = {}
        for spec in agent_specs():
            monitor = monitor_factory(spec)(tmp_path, None)
            built[spec.key] = monitor
            assert monitor.agent_key == spec.key
            assert monitor.events_file.name == f"{spec.key}.jsonl"
        assert sorted(built) == sorted(builtin_agent_keys())
        for monitor in built.values():
            monitor.stop()

    def test_uninstallable_specs_have_unique_result_keys(self):
        specs = uninstallable_specs()
        keys = [spec.uninstall_result_key for spec in specs]
        assert len(keys) == len(set(keys))
        # 既有卸载清理结果键必须保持（安装包脚本与测试按它读结果）
        assert {"claude_hooks", "dsh_bridge"} <= set(keys)
        assert {"kimi_hooks", "zcode_hooks"} <= set(keys)

    def test_resolve_ref_reports_bad_reference(self):
        with pytest.raises(ValueError):
            resolve_ref("no-colon-here")

    def test_extra_event_states_fold_into_normalizer(self):
        from pet.agent_link import DEFAULT_EVENT_STATE_MAP, normalize_event_state

        assert DEFAULT_EVENT_STATE_MAP["Notification"] == "attention"
        assert normalize_event_state("SubagentStart") == "working"
        assert normalize_event_state("Interrupt") == "idle"
        assert normalize_event_state("PreCompact") == "working"
        for event, state in extra_event_states().items():
            assert DEFAULT_EVENT_STATE_MAP[event] == state


# ============================================================================
# 2. 通用 hook 写入器（真子进程）
# ============================================================================
class TestHookWriter:
    def test_script_and_command_shape(self, tmp_path):
        script = ensure_event_hook(tmp_path, "mycli")
        assert script.path.parent == tmp_path
        assert script.path.name.startswith("mycli_event_hook")
        assert str(script.path) in script.command
        assert script.base_args[-1] == str(script.path)
        assert script.args_for("Stop")[-1] == "Stop"

    def test_explicit_output_name(self, tmp_path):
        """自定义联动的 path 可以是任意文件名，脚本按它命名事件文件。"""
        script = ensure_event_hook(tmp_path, "mycli", out_name="custom-name.jsonl")
        assert script.path.name.startswith("mycli_event_hook")
        assert "custom-name.jsonl" in script.path.read_text(encoding="utf-8")

    def test_refresh_is_idempotent(self, tmp_path):
        first = ensure_event_hook(tmp_path, "kimi")
        before = first.path.read_text(encoding="utf-8")
        second = ensure_event_hook(tmp_path, "kimi")
        assert second.path == first.path
        assert second.path.read_text(encoding="utf-8") == before

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 写入脚本用 Python 解释器")
    def test_script_appends_event_and_tool_from_stdin(self, tmp_path):
        script = ensure_event_hook(tmp_path, "mycli")
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "shell"})
        proc = subprocess.run(
            [sys.executable, str(script.path)],
            input=payload, capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 0
        lines = events_file_for(tmp_path, "mycli").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["agent"] == "mycli"
        assert record["event"] == "PreToolUse"
        assert record["tool"] == "shell"
        assert isinstance(record["ts"], float)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 写入脚本用 Python 解释器")
    def test_argv_event_wins_over_stdin(self, tmp_path):
        script = ensure_event_hook(tmp_path, "zcode")
        proc = subprocess.run(
            [sys.executable, str(script.path), "Stop"],
            input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
            capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 0
        record = json.loads(events_file_for(tmp_path, "zcode").read_text(encoding="utf-8").splitlines()[0])
        assert record["event"] == "Stop"
        assert "tool" not in record

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 写入脚本用 Python 解释器")
    def test_unknown_event_is_ignored_not_guessed(self, tmp_path):
        """事件名取不到时绝不写记录（协议红线：未知事件不得默认成 working）。"""
        script = ensure_event_hook(tmp_path, "kimi")
        proc = subprocess.run(
            [sys.executable, str(script.path)],
            input="not json at all", capture_output=True, text=True, timeout=20,
        )
        assert proc.returncode == 0
        assert not events_file_for(tmp_path, "kimi").exists()


# ============================================================================
# 3. 通用接入助手（自定义通道）：给任意宿主生成 hook 命令
# ============================================================================
class TestGenericHookCli:
    def _run(self, *argv):
        from pet.agents.hook_writer import main

        return main(list(argv))

    def test_prints_ready_to_paste_command(self, tmp_path, capsys):
        out = tmp_path / "host" / "pet-events.jsonl"
        assert self._run("--agent", "mycli", "--out", str(out)) == 0
        printed = capsys.readouterr().out
        assert '"key": "mycli"' in printed
        assert str(out) in printed
        # 命令里引用的脚本确实落地了
        script = tmp_path / "host" / "mycli_event_hook.py"
        assert script.exists()
        assert str(script) in printed

    def test_event_name_is_appended_to_command(self, tmp_path, capsys):
        out = tmp_path / "pet-events.jsonl"
        assert self._run("--agent", "mycli", "--out", str(out), "--event", "Stop") == 0
        assert capsys.readouterr().out.rstrip().endswith("Stop")

    def test_json_output_shape(self, tmp_path, capsys):
        out = tmp_path / "pet-events.jsonl"
        assert self._run("--agent", "mycli", "--out", str(out), "--event", "Stop", "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["agent"] == "mycli"
        assert payload["path"] == str(out)
        assert payload["args"][-1] == "Stop"
        assert payload["command"].endswith("Stop")

    def test_rejects_invalid_key(self, tmp_path, capsys):
        assert self._run("--agent", "Bad Key", "--out", str(tmp_path / "x.jsonl")) == 2
        assert "非法" in capsys.readouterr().err

    def test_rejects_builtin_key(self, tmp_path, capsys):
        """内置 Agent 有专属安装器，不该被引导到自定义通道。"""
        assert self._run("--agent", "kimi", "--out", str(tmp_path / "x.jsonl")) == 2
        assert "内置 Agent" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 写入脚本用 Python 解释器")
    def test_generated_command_really_writes_events(self, tmp_path, capsys):
        """端到端：CLI 打印的命令真的能把宿主事件写进指定文件。"""
        import shlex

        out = tmp_path / "pet-events.jsonl"
        assert self._run("--agent", "mycli", "--out", str(out), "--json") == 0
        command = json.loads(capsys.readouterr().out)["command"]
        subprocess.run(
            shlex.split(command),
            input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "bash"}),
            capture_output=True, text=True, timeout=20,
        )
        record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        assert record["agent"] == "mycli"
        assert record["event"] == "PreToolUse"
        assert record["tool"] == "bash"
        assert isinstance(record["ts"], float)


# ============================================================================
# 4. Kimi：[[hooks]]（strict schema 自校验）
# ============================================================================
class TestKimiInstall:
    @pytest.fixture(autouse=True)
    def _kimi_homes(self, tmp_path, monkeypatch):
        code_home = tmp_path / "kimi-code"
        legacy = tmp_path / "kimi"
        code_home.mkdir()
        monkeypatch.setattr(kimi_mod, "kimi_code_home", lambda: code_home)
        monkeypatch.setattr(kimi_mod, "legacy_home", lambda: legacy)
        self.code_home = code_home
        self.legacy = legacy
        return code_home

    def test_install_writes_valid_toml_blocks(self, tmp_path):
        ok, detail = kimi_mod.install_hooks(tmp_path / "agent-events" / "kimi.jsonl")
        assert ok is True, detail
        text = (self.code_home / "config.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
        blocks = parsed["hooks"]
        assert len(blocks) == len(kimi_mod.CODE_EVENTS)
        assert {b["event"] for b in blocks} == set(kimi_mod.CODE_EVENTS)
        for block in blocks:
            assert set(block) == {"event", "matcher", "command", "timeout"}
            assert block["timeout"] == kimi_mod.HOOK_TIMEOUT_S
            assert kimi_mod.HOOK_MARKER in block["command"]
        # 阻塞式审批事件不注册（没有回写能力）
        assert "PermissionRequest" not in {b["event"] for b in blocks}

    def test_install_skips_generation_that_is_not_installed(self, tmp_path):
        """只装存在的代际：legacy 目录不存在时不去创建它。"""
        assert kimi_mod.install_hooks(tmp_path / "kimi.jsonl")[0] is True
        assert not (self.legacy / "config.toml").exists()

    def test_install_fails_when_no_kimi_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kimi_mod, "kimi_code_home", lambda: tmp_path / "nope")
        ok, detail = kimi_mod.install_hooks(tmp_path / "kimi.jsonl")
        assert ok is False
        assert "未检测到 Kimi 配置目录" in detail

    def test_install_is_idempotent_and_preserves_user_blocks(self, tmp_path):
        config = self.code_home / "config.toml"
        config.write_text(
            'default_model = "kimi-code/k3"\n\n'
            '[[hooks]]\nevent = "SessionStart"\ncommand = "user-own-hook"\n'
            'matcher = ""\ntimeout = 5\n',
            encoding="utf-8",
        )
        events_file = tmp_path / "kimi.jsonl"
        assert kimi_mod.install_hooks(events_file)[0] is True
        assert kimi_mod.install_hooks(events_file)[0] is True
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        assert parsed["default_model"] == "kimi-code/k3"
        user_blocks = [b for b in parsed["hooks"] if b["command"] == "user-own-hook"]
        assert len(user_blocks) == 1
        ours = [b for b in parsed["hooks"] if kimi_mod.HOOK_MARKER in b["command"]]
        assert len(ours) == len(kimi_mod.CODE_EVENTS)

    def test_install_drops_stale_empty_hooks_assignment(self, tmp_path):
        config = self.code_home / "config.toml"
        config.write_text('hooks = []\n', encoding="utf-8")
        assert kimi_mod.install_hooks(tmp_path / "kimi.jsonl")[0] is True
        assert "hooks = []" not in config.read_text(encoding="utf-8")

    def test_uninstall_removes_only_our_blocks(self, tmp_path):
        config = self.code_home / "config.toml"
        config.write_text(
            '[[hooks]]\nevent = "Stop"\ncommand = "user-own-hook"\nmatcher = ""\ntimeout = 5\n',
            encoding="utf-8",
        )
        assert kimi_mod.install_hooks(tmp_path / "kimi.jsonl")[0] is True
        assert kimi_mod.uninstall_hooks() is True
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        assert [b["command"] for b in parsed["hooks"]] == ["user-own-hook"]

    def test_validate_blocks_rejects_unknown_event(self):
        blocks = kimi_mod.build_blocks(("NotARealEvent",), '"cmd"')
        with pytest.raises(ValueError):
            kimi_mod.validate_blocks(blocks, kimi_mod.CODE_EVENTS)

    def test_validate_blocks_rejects_out_of_range_timeout(self):
        blocks = kimi_mod.build_blocks(("Stop",), '"cmd"').replace("timeout = 30", "timeout = 9999")
        with pytest.raises(ValueError):
            kimi_mod.validate_blocks(blocks, kimi_mod.CODE_EVENTS)

    def test_validate_blocks_rejects_illegal_key(self):
        blocks = kimi_mod.build_blocks(("Stop",), '"cmd"') + '\nname = "oops"'
        with pytest.raises(ValueError):
            kimi_mod.validate_blocks(blocks, kimi_mod.CODE_EVENTS)


# ============================================================================
# 5. ZCode：hooks.events.*
# ============================================================================
class TestZCodeInstall:
    @pytest.fixture(autouse=True)
    def _zcode_home(self, tmp_path, monkeypatch):
        home = tmp_path / "zcode"
        (home / "cli").mkdir(parents=True)
        monkeypatch.setattr(zcode_mod, "zcode_home", lambda: home)
        self.home = home
        return home

    def test_install_writes_strict_hook_entries(self, tmp_path):
        ok, detail = zcode_mod.install_hooks(tmp_path / "agent-events" / "zcode.jsonl")
        assert ok is True, detail
        settings = json.loads((self.home / "cli" / "config.json").read_text(encoding="utf-8"))
        hooks = settings["hooks"]
        assert hooks["enabled"] is True
        for event in zcode_mod.HOOK_EVENTS:
            entries = hooks["events"][event]
            assert len(entries) == 1
            # 容器只允许 hooks（matcher 缺省 = 匹配全部）；多键会让 ZCode 拒绝加载
            assert set(entries[0]) == {"hooks"}
            hook = entries[0]["hooks"][0]
            assert set(hook) == {"type", "command", "args", "timeoutMs"}
            assert hook["type"] == "process"
            assert zcode_mod.HOOK_MARKER in hook["args"][0]
            assert hook["args"][-1] == event
        assert "PermissionRequest" not in hooks["events"]

    def test_install_preserves_user_entries_and_is_idempotent(self, tmp_path):
        config = self.home / "cli" / "config.json"
        config.write_text(json.dumps({
            "hooks": {
                "enabled": True,
                "events": {"Stop": [{"hooks": [{"type": "process", "command": "user-own", "args": ["x"], "timeoutMs": 1000}]}]},
            },
            "other": {"keep": True},
        }), encoding="utf-8")
        events_file = tmp_path / "zcode.jsonl"
        assert zcode_mod.install_hooks(events_file)[0] is True
        assert zcode_mod.install_hooks(events_file)[0] is True
        settings = json.loads(config.read_text(encoding="utf-8"))
        assert settings["other"] == {"keep": True}
        stop_entries = settings["hooks"]["events"]["Stop"]
        assert len(stop_entries) == 2
        assert any("user-own" in json.dumps(g) for g in stop_entries)

    def test_install_respects_explicit_disabled(self, tmp_path):
        config = self.home / "cli" / "config.json"
        config.write_text(json.dumps({"hooks": {"enabled": False}}), encoding="utf-8")
        ok, detail = zcode_mod.install_hooks(tmp_path / "zcode.jsonl")
        assert ok is False
        assert "hooks.enabled = false" in detail

    def test_uninstall_removes_only_our_entries(self, tmp_path):
        config = self.home / "cli" / "config.json"
        config.write_text(json.dumps({
            "hooks": {"enabled": True, "events": {"Stop": [{"hooks": [{"type": "process", "command": "user-own", "args": ["x"], "timeoutMs": 1000}]}]}},
        }), encoding="utf-8")
        assert zcode_mod.install_hooks(tmp_path / "zcode.jsonl")[0] is True
        assert zcode_mod.uninstall_hooks() is True
        settings = json.loads(config.read_text(encoding="utf-8"))
        assert settings["hooks"]["enabled"] is True  # 用户开关不动
        assert len(settings["hooks"]["events"]["Stop"]) == 1
        assert "user-own" in json.dumps(settings["hooks"]["events"]["Stop"])

    def test_install_failure_on_corrupt_config_does_not_overwrite(self, tmp_path):
        config = self.home / "cli" / "config.json"
        config.write_text("{ this is not json", encoding="utf-8")
        ok, detail = zcode_mod.install_hooks(tmp_path / "zcode.jsonl")
        assert ok is False
        assert config.read_text(encoding="utf-8") == "{ this is not json"
        assert "解析失败" in detail
