# -*- coding: utf-8 -*-
"""ncm-cli（网易云音乐 CLI/TUI）播放来源的离线单元测试。

全部用例不依赖真实 ncm-cli、不联网：

- 子进程边界用 PATH 前置的**桩脚本**（吐预置 JSON）——mock 操作系统边界是
  项目测试纪律允许的切面；
- daemon socket / play-session.json 用临时目录伪造；
- 采样线程用真实 threading.Event 驱动，不做 sleep 猜时序。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import threading
import time

import pytest

from pet import ncm_player


@pytest.fixture(autouse=True)
def _reset_ncm_module_state():
    """每个用例前后复位模块级缓存/单例，杜绝用例间状态泄漏。

    不加这个的话：先跑的用例把 ``_NCM_CONFIG_DIR``（按真实 HOME 解析）与
    ``_sampler`` 单例留在模块上，后面所有改 HOME/XDG 的用例全部失真——
    全量套件里稳定复现、单跑永远绿的元凶。
    """
    ncm_player._reset_module_state_for_tests()
    yield
    ncm_player._reset_module_state_for_tests()


def _write_stub(tmp_path, name, body):
    """造一个可执行的桩脚本；在 POSIX 上赋 0755。"""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _shell_print_json(payload: str) -> str:
    r"""构造一段 `printf '%s' <json>` 的 shell 片段，JSON 加单引号安全转义。

    单引号内的单引号按 POSIX shell 规则拆成 '\''，换行等控制字符避免出现在
    表达式里（f-string 语法限制），由调用方传单行 JSON。
    """
    assert "\n" not in payload
    return "printf '%s' " + "'" + payload.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- state 解析


def test_parse_state_title_splits_name_artist():
    """「歌名 - 歌手」按第一个 " - " 拆开；拆不开时整串当歌名。"""
    title, artist = ncm_player._split_title("晴天 - 周杰伦")
    assert (title, artist) == ("晴天", "周杰伦")
    title, artist = ncm_player._split_title("Borrowed Blue - KKI - Remix")
    assert title == "Borrowed Blue"
    assert artist == "KKI - Remix"
    title, artist = ncm_player._split_title("纯音乐无分隔")
    assert (title, artist) == ("纯音乐无分隔", "")


def _state_json(status="playing", title="晴天 - 周杰伦", position=41.2,
                duration=269.0):
    return json.dumps({
        "success": True,
        "state": {
            "status": status, "title": title, "position": position,
            "duration": duration, "progress": "0:41 / 4:29",
            "volume": None, "currentIndex": 0, "queueLength": 1,
        },
    })


def test_read_state_playing():
    """播放中：title 拆出歌名/歌手，position/duration 取真值。"""
    data = ncm_player._parse_state(_state_json())
    assert data is not None
    assert data["status"] == "playing"
    assert data["title"] == "晴天 - 周杰伦"
    assert data["position"] == pytest.approx(41.2)
    assert data["duration"] == pytest.approx(269.0)


def test_parse_state_garbage_returns_none():
    assert ncm_player._parse_state("not json") is None
    assert ncm_player._parse_state(json.dumps({"success": False})) is None
    assert ncm_player._parse_state(json.dumps({"success": True})) is None
    assert ncm_player._parse_state("") is None


def test_playback_from_state():
    """state → Playback 换算：playing=True、position 为真值、app_id 标记来源。"""
    data = ncm_player._parse_state(_state_json())
    before = time.monotonic()
    playback = ncm_player._playback_from_state(data, title_song_id=None)
    assert playback is not None
    assert playback.track.title == "晴天"
    assert playback.track.artist == "周杰伦"
    assert playback.track.playing is True
    assert playback.position == pytest.approx(41.2)
    assert playback.updated_at >= before
    assert playback.app_id == "ncm-cli"
    # Track.key 可用作切歌判据
    assert playback.track.key() == ("晴天", "周杰伦")


def test_playback_from_stopped_state():
    """stopped：playing=False。暂停在 ncm-cli 里也表现为 stopped，由上层宽限期兜。"""
    data = {"status": "stopped", "title": "晴天 - 周杰伦",
            "position": 102.3, "duration": 269.0}
    playback = ncm_player._playback_from_state(data, title_song_id=None)
    assert playback is not None
    assert playback.track.playing is False


def test_playback_from_state_without_title():
    """stopped 后 title 仍保留最后一首——仅当 status=playing 才产 Playback。"""
    data = {"status": "stopped", "title": "", "position": 0.0, "duration": 0.0}
    assert ncm_player._playback_from_state(data, title_song_id=None) is None


# ---------------------------------------------------------------- socket 门控


def test_daemon_socket_gate(tmp_path, monkeypatch):
    """daemon socket 不存在时绝不起 ncm-cli 子进程（不把 daemon 拉活）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", home / ".config" / "ncm-cli")

    calls = []

    def _recorder(*args, **kwargs):
        calls.append(args)
        raise AssertionError("socket 不存在时不应调用子进程")

    monkeypatch.setattr(ncm_player, "_run_ncm", _recorder)
    monkeypatch.setattr(ncm_player, "STATE_POLL_SECONDS", 0)

    assert ncm_player._daemon_socket_path() == (
        home / ".config" / "ncm-cli" / "player-daemon.sock")
    assert ncm_player.sample_now() is None
    assert calls == []


def test_socket_gate_uses_xdg(tmp_path, monkeypatch):
    """socket 路径尊重 XDG_CONFIG_HOME（ncm-cli 用 Electron 风格配置目录）。"""
    config_root = tmp_path / "xdg"
    config_root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.delenv("HOME", raising=False)
    assert ncm_player._daemon_socket_path() == (
        config_root / "ncm-cli" / "player-daemon.sock")


# ---------------------------------------------------------------- play-session.json


def test_read_session_song_id(tmp_path, monkeypatch):
    """播放期间读 play-session.json 拿加密歌曲 ID（取词的精确入参）。"""
    config_dir = tmp_path / "ncm-cli"
    config_dir.mkdir()
    (config_dir / "play-session.json").write_text(
        json.dumps({"id": "D6BD718AA81FAE09DF9B96F1ED836A82", "type": "song"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", config_dir)
    assert ncm_player._read_session_song_id() == "D6BD718AA81FAE09DF9B96F1ED836A82"


def test_read_session_song_id_bad_file(tmp_path, monkeypatch):
    """文件缺失/损坏/非 hex id：返回 None（上层写日志，不兜底）。"""
    config_dir = tmp_path / "ncm-cli"
    config_dir.mkdir()
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", config_dir)
    assert ncm_player._read_session_song_id() is None
    (config_dir / "play-session.json").write_text("{broken", encoding="utf-8")
    assert ncm_player._read_session_song_id() is None
    (config_dir / "play-session.json").write_text(
        json.dumps({"id": "not-hex!!"}), encoding="utf-8")
    assert ncm_player._read_session_song_id() is None


# ---------------------------------------------------------------- 取词


def _lyric_payload(*, no_lyric=False, pure_music=False, lrc=""):
    return json.dumps({
        "code": 200,
        "data": {
            "songId": "X", "lyric": lrc, "noLyric": no_lyric,
            "transLyric": None, "txtLyric": "", "romalrc": None,
            "pureMusic": pure_music,
        },
    })


def test_fetch_ncm_lyrics_normal_song(tmp_path, monkeypatch):
    """有 LRC：解析成 Lyrics，供 LyricTracker 用。"""
    payload = _lyric_payload(lrc="[00:01.00]第一句\n[00:05.00]第二句")
    _write_stub(tmp_path, "ncm-cli", "#!/bin/sh\n" + _shell_print_json(payload))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    lyrics = ncm_player.fetch_lyrics_via_ncm("D6BD718AA81FAE09DF9B96F1ED836A82")
    assert lyrics is not None
    assert not lyrics.instrumental
    assert [line.text for line in lyrics.lines] == ["第一句", "第二句"]
    assert [round(line.at, 2) for line in lyrics.lines] == [1.0, 5.0]


def test_fetch_ncm_lyrics_instrumental(tmp_path, monkeypatch):
    """pureMusic=true：纯音乐（instrumental=True）。"""
    _write_stub(tmp_path, "ncm-cli", "#!/bin/sh\n" + _shell_print_json(
        _lyric_payload(pure_music=True)))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    lyrics = ncm_player.fetch_lyrics_via_ncm("A" * 32)
    assert lyrics is not None
    assert lyrics.instrumental is True
    assert not lyrics.lines


def test_fetch_ncm_lyrics_no_lyric(tmp_path, monkeypatch):
    """noLyric=true（pureMusic 缺席）：确定性无词 → 空 Lyrics（不出气泡，照唱）。"""
    _write_stub(tmp_path, "ncm-cli", "#!/bin/sh\n" + _shell_print_json(
        _lyric_payload(no_lyric=True)))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    lyrics = ncm_player.fetch_lyrics_via_ncm("A" * 32)
    assert lyrics is not None          # 有效结果：确定性「无词」
    assert not lyrics                  # 但无行、非纯音乐
    assert lyrics.instrumental is False


def test_fetch_ncm_lyrics_pure_music_wins_over_no_lyric(tmp_path, monkeypatch):
    """pureMusic + noLyric 同时为 true（实测纯音乐常见）：必须判纯音乐。

    判成 noLyric 的后果：桌宠对着钢琴曲做唱歌动画（无词不唱的红线被破坏）。
    """
    _write_stub(tmp_path, "ncm-cli", "#!/bin/sh\n" + _shell_print_json(
        _lyric_payload(no_lyric=True, pure_music=True)))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    lyrics = ncm_player.fetch_lyrics_via_ncm("A" * 32)
    assert lyrics is not None
    assert lyrics.instrumental is True


def test_fetch_ncm_lyrics_txt_only_treated_as_no_lyric(tmp_path, monkeypatch):
    """只有非滚动文本歌词（txtLyric 无时间戳）：视同无词（一期不支持）。"""
    payload = json.dumps({
        "code": 200,
        "data": {"lyric": "", "noLyric": False, "transLyric": None,
                 "txtLyric": "没有时间戳的词", "pureMusic": None},
    })
    _write_stub(tmp_path, "ncm-cli", "#!/bin/sh\n" + _shell_print_json(payload))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    lyrics = ncm_player.fetch_lyrics_via_ncm("A" * 32)
    assert lyrics is not None and not lyrics and not lyrics.instrumental


def test_fetch_ncm_lyrics_failure_returns_none(tmp_path, monkeypatch):
    """子进程失败/非 200：None（上层写 WARNING 日志，不兜底、不重试）。"""
    _write_stub(tmp_path, "ncm-cli", """
        #!/bin/sh
        echo '{"code":401,"message":"未登录"}' >&2
        exit 1
    """)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    assert ncm_player.fetch_lyrics_via_ncm("A" * 32) is None


def test_fetch_ncm_lyrics_timeout_returns_none(tmp_path, monkeypatch):
    """超时被钳住：死慢的桩按失败处理，不让取词线程挂死。"""
    _write_stub(tmp_path, "ncm-cli", """
        #!/bin/sh
        sleep 30
    """)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", tmp_path / "ncm-cli")
    monkeypatch.setattr(ncm_player, "LYRIC_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    assert ncm_player.fetch_lyrics_via_ncm("A" * 32) is None
    assert time.monotonic() - started < 5.0


def test_fetch_ncm_lyrics_bad_args(monkeypatch):
    """空/非法 song_id 直接 None，不起子进程。"""
    def _boom(*a, **k):
        raise AssertionError("不应起子进程")

    monkeypatch.setattr(ncm_player, "_run_ncm", _boom)
    assert ncm_player.fetch_lyrics_via_ncm("") is None
    assert ncm_player.fetch_lyrics_via_ncm(None) is None
    assert ncm_player.fetch_lyrics_via_ncm("not hex!") is None


# ---------------------------------------------------------------- 采样线程


def test_sampler_lifecycle_and_playing_flag(tmp_path, monkeypatch):
    """采样线程：stop 后线程退出；is_playing 反映最近一次 state。"""
    home = tmp_path / "home"
    (home / ".config" / "ncm-cli").mkdir(parents=True)
    (home / ".config" / "ncm-cli" / "player-daemon.sock").write_bytes(b"")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", home / ".config" / "ncm-cli")
    monkeypatch.setattr(ncm_player, "STATE_POLL_SECONDS", 0)

    outputs = [
        {"status": "playing", "title": "晴天 - 周杰伦",
         "position": 1.0, "duration": 269.0},
        {"status": "stopped", "title": "晴天 - 周杰伦",
         "position": 1.0, "duration": 269.0},
    ]
    queue = list(outputs)
    calls = []

    def fake_sample():
        calls.append(time.monotonic())
        data = queue.pop(0) if queue else None
        if data is None:
            return None
        return ncm_player._playback_from_state(data, title_song_id=None)

    monkeypatch.setattr(ncm_player, "_sample_once", fake_sample)

    sampler = ncm_player.NcmSampler()
    assert sampler.is_playing() is False
    sampler.start()
    deadline = time.monotonic() + 5.0
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 2, "采样线程应至少跑两拍"
    # 第二拍是 stopped：playing 标志应已翻转（宽限期由上层处理）。
    deadline = time.monotonic() + 5.0
    while sampler.is_playing() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sampler.is_playing() is False
    sampler.stop()
    sampler.join(timeout=5.0)
    assert not sampler.is_alive()
    # 再次 start/stop 幂等。
    sampler2 = ncm_player.NcmSampler()
    sampler2.start()
    sampler2.stop()
    sampler2.join(timeout=5.0)
    assert not sampler2.is_alive()


def test_sampler_survives_sample_exception(tmp_path, monkeypatch):
    """采样异常不能杀死线程（写日志、当 None 处理、下一拍继续）。"""
    home = tmp_path / "home"
    (home / ".config" / "ncm-cli").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", home / ".config" / "ncm-cli")
    monkeypatch.setattr(ncm_player, "STATE_POLL_SECONDS", 0)

    calls = {"n": 0}

    def fake_sample():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("daemon 短暂抽风")
        return None

    monkeypatch.setattr(ncm_player, "_sample_once", fake_sample)
    sampler = ncm_player.NcmSampler()
    sampler.start()
    deadline = time.monotonic() + 5.0
    while calls["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls["n"] >= 2, "异常后线程应继续"
    assert sampler.is_alive()
    sampler.stop()
    sampler.join(timeout=5.0)


def test_sampler_not_available_without_socket(tmp_path, monkeypatch):
    """没有 daemon socket：available() 恒 False（用于设置面/日志判断）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ncm_player, "_NCM_CONFIG_DIR", home / ".config" / "ncm-cli")
    assert ncm_player.available() is False


# ---------------------------------------------------------------- 平台分派


def test_is_music_playing_dispatches_on_linux(monkeypatch):
    """Linux：唱歌检测走 ncm 快照，不再恒 False。"""
    import pet.music_detect as music_detect

    monkeypatch.setattr(music_detect.sys, "platform", "linux")
    monkeypatch.setattr(ncm_player, "current_playback_playing", lambda: True)
    assert music_detect.is_music_playing() is True
    monkeypatch.setattr(ncm_player, "current_playback_playing", lambda: False)
    assert music_detect.is_music_playing() is False


def test_is_music_playing_uses_ncm_sampler_singleton(monkeypatch):
    """首次调用会拉起共享采样器（幂等）；False 时不能反复新建线程。"""
    import pet.music_detect as music_detect

    monkeypatch.setattr(music_detect.sys, "platform", "linux")
    created = []

    class FakeSampler:
        def __init__(self):
            created.append(self)
            self.started = 0

        def start(self):
            self.started += 1

        def is_playing(self):
            return False

    monkeypatch.setattr(ncm_player, "NcmSampler", FakeSampler)
    assert music_detect.is_music_playing() is False
    assert len(created) == 1
    assert music_detect.is_music_playing() is False
    assert len(created) == 1, "采样器必须是单例，不能每拍新建"


def test_now_playing_dispatches_to_ncm(monkeypatch):
    """Linux：get_now_playing 走 ncm state（SMTC 在非 Windows 恒 None）。"""
    import pet.now_playing as now_playing

    monkeypatch.setattr(now_playing.sys, "platform", "linux")
    monkeypatch.setattr(
        now_playing, "_ncm_get_now_playing",
        lambda tracked=None: "SENTINEL_PLAYBACK")
    assert now_playing.get_now_playing("whatever") == "SENTINEL_PLAYBACK"
