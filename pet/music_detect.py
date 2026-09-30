# -*- coding: utf-8 -*-
"""后台音乐/音频播放检测（Windows + Linux）。

- **Windows**：通过 pycaw 读取默认音频输出设备的瞬时峰值电平：只要系统正在
  输出声音（音乐、视频、游戏等），峰值就会高于静音阈值。桌宠据此自动播放
  唱歌动画；阈值设置得较低，避免只有极微弱提示音时频繁触发。
- **Linux**：没有 pycaw 这类系统级峰值接口（且 Wayland/PulseAudio 权限模型
  下读输出峰值不可行），改为读 ncm-cli 播放器的共享采样线程快照
  （``pet/ncm_player.py``，覆盖 CLI 与 TUI，2026-09-30）。快照由后台线程
  每 3 秒刷新一次，本函数零子进程成本，可安全地在 1s 唱歌定时器里调用。
"""

from __future__ import annotations

import sys

# 峰值电平阈值：0.0=静音，1.0=满幅。取 0.02 过滤极低电平/数字静音。
MUSIC_PEAK_THRESHOLD = 0.02


_meter = None


def _get_meter():
    """惰性创建并复用一个音频峰值检测 COM 对象。

    每次调用都重新 Activate 会持续产生 COM 接口句柄，长时间运行（如音乐自动
    唱歌每 4 秒检测一次）可能累积并导致崩溃；这里只初始化一次。
    """
    global _meter
    if _meter is not None:
        return _meter
    try:
        import comtypes
        from ctypes import POINTER, cast

        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        device = AudioUtilities.GetSpeakers()._dev
        interface = device.Activate(
            IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None
        )
        _meter = cast(interface, POINTER(IAudioMeterInformation))
    except Exception:
        _meter = None
    return _meter


def _is_music_playing_windows() -> bool:
    try:
        meter = _get_meter()
        if meter is None:
            return False
        return meter.GetPeakValue() > MUSIC_PEAK_THRESHOLD
    except Exception:
        # 无 pycaw / 音频设备不可用 / COM 初始化失败时按“未播放”处理，不打扰用户
        return False


def _is_music_playing_linux() -> bool:
    # Linux 分支读 ncm 采样快照。绝不在 GUI 线程跑子进程（单次 ~313ms 会卡
    # 窗口一拍）；current_playback_playing 只读内存标志，必要时拉起采样线程。
    try:
        from . import ncm_player
        return bool(ncm_player.current_playback_playing())
    except Exception:
        return False


def is_music_playing() -> bool:
    """返回当前是否有音乐在放（Windows：系统音频峰值；Linux：ncm-cli 播放中）。"""
    if sys.platform == 'win32':
        return _is_music_playing_windows()
    if sys.platform.startswith('linux'):
        return _is_music_playing_linux()
    return False
