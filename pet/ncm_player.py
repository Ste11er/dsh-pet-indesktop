# -*- coding: utf-8 -*-
"""ncm-cli（网易云音乐 CLI / TUI）播放来源（Linux）。

ncm-cli 的 CLI 命令与 TUI 共享同一个本地 daemon（Unix socket），因此本模块
只对接 ``ncm-cli state`` 一个入口就能同时覆盖两种使用方式（2026-09-30 实测）：

- ``state`` 是**本地** daemon IPC（断网实测输出不变），但每次调用是一个完整
  Node 子进程，实测 296~336ms（冷启动主导）——所以采样放后台线程、
  **3 秒一拍**，绝不进 GUI 线程；
- ``state.title`` 是 ``"歌名 - 歌手"`` 拼串，**不含歌曲 ID**。加密 ID 只在
  daemon 存活期间的磁盘文件 ``~/.config/ncm-cli/play-session.json`` 里
  （每次起播重写，daemon 退出即删除），恰好覆盖播放期间；
- ``pause`` 之后 ``status`` 也是 ``"stopped"``（没有 paused 值），position
  冻结——与停止的区别交给上层（唱歌宽限期 / 歌词暂停）处理；
- 歌词走 ``ncm-cli song lyric --songId <加密ID>``：精确命中、带服务端一等
  ``noLyric`` / ``pureMusic`` 标志。**失败/查不到只写日志，不做任何兜底源**。

线程模型：

- ``NcmSampler`` 是进程级单例的常驻 daemon 线程（``music_detect`` 与
  ``MusicLyricController`` 共用），持有最近一次 state 快照；
- GUI 线程只读快照（``is_music_playing`` / ``current_playback``），零子进程
  成本；每拍之间歌词进度由 ``LyricTracker`` 本地时钟外推；
- daemon socket 不存在时**根本不调 state**——避免每 3 秒把退出的 daemon
  重新拉活（state 客户端会自动 spawn daemon）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .now_playing import Playback, Track
from .music_lyric import Lyrics, parse_lrc

log = logging.getLogger(__name__)

# state 轮询间隔（秒）。单次子进程实测 ~313ms（Node 冷启动主导），1Hz 等于
# 每秒一次完整 Node 启动，不可取；3s 一拍约 10% 单核（后台线程），切歌/开口
# 唱歌的识别延迟 ≤3s，可接受。测试通过把它改成 0 加速。
STATE_POLL_SECONDS = 3.0

# 取词子进程超时（秒）。实测 song lyric 约 3.3s，留足余量但不挂死取词线程。
LYRIC_TIMEOUT_SECONDS = 15.0

# state 子进程超时（秒）。本地 IPC 正常 <0.5s；超时说明 daemon 卡死。
STATE_TIMEOUT_SECONDS = 5.0

# ncm-cli 配置目录与关键文件。测试用 monkeypatch 覆盖 _NCM_CONFIG_DIR。
_NCM_CONFIG_DIR: Path | None = None

_SONG_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


def _reset_module_state_for_tests() -> None:
    """清空模块级缓存/单例（测试隔离用；生产代码不调用）。

    ``_NCM_CONFIG_DIR`` 首次解析后缓存（HOME/XDG 进程内视为不变），
    ``_sampler`` 是进程级单例——两者都会把状态泄漏到后续测试，测试必须
    显式复位。生产路径没有调用点，ruff 不会报 unused（下划线开头 + 被测试
    引用）。
    """
    global _NCM_CONFIG_DIR, _sampler
    _NCM_CONFIG_DIR = None
    sampler = _sampler
    _sampler = None
    # 测试会把 NcmSampler 换成替身，不能假设有完整接口。
    alive = getattr(sampler, "is_alive", None)
    if callable(alive) and alive():
        stop = getattr(sampler, "stop", None)
        if callable(stop):
            stop()


def _config_dir() -> Path:
    """ncm-cli 的配置目录（~/.config/ncm-cli 或 $XDG_CONFIG_HOME/ncm-cli）。

    缓存到模块级 ``_NCM_CONFIG_DIR``：HOME/XDG 环境在进程生命周期内视为不变
    （与 ncm-cli 自己的 Electron 风格路径解析一致），测试用 monkeypatch 直改。
    """
    global _NCM_CONFIG_DIR
    if _NCM_CONFIG_DIR is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            base = Path(xdg)
        else:
            base = Path.home() / ".config"
        _NCM_CONFIG_DIR = base / "ncm-cli"
    return _NCM_CONFIG_DIR


def _daemon_socket_path() -> Path:
    """daemon 的 Unix socket 路径——存在即"ncm-cli 正在放歌（或刚放完）"。"""
    return _config_dir() / "player-daemon.sock"


def available() -> bool:
    """本机是否具备 ncm-cli 播放来源（socket 存在 = daemon 在跑）。"""
    try:
        return _daemon_socket_path().exists()
    except Exception:
        return False


# ---------------------------------------------------------------- 子进程


def _run_ncm(args: list[str], *, timeout: float) -> subprocess.CompletedProcess | None:
    """跑一次 ``ncm-cli`` 子进程；任何失败（找不到/超时/非零退出）返回 None。"""
    try:
        return subprocess.run(
            ["ncm-cli", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        log.debug("ncm-cli 未安装，跳过 ncm 播放来源")
        return None
    except subprocess.TimeoutExpired:
        log.warning("ncm-cli %s 超时（%.1fs）", args[0], timeout)
        return None
    except Exception:
        log.debug("ncm-cli %s 调用失败", args[0], exc_info=True)
        return None


# ---------------------------------------------------------------- state 采样


def _parse_state(text: str) -> dict | None:
    """解析 ``ncm-cli state --output json`` 的 stdout。

    预期结构 ``{"success": true, "state": {status,title,position,...}}``；
    任何缺失/损坏返回 None（下一拍再试）。
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    return state


def _split_title(title: str) -> tuple[str, str]:
    """把 ``"歌名 - 歌手"`` 拆成 ``(歌名, 歌手)``。

    按第一个 ``" - "`` 拆（实测 TUI/CLI 的拼接格式）；拆不开时整串当歌名、
    歌手为空——歌名用于气泡标题与切歌判据，错了比缺了更糟。
    """
    raw = str(title or "").strip()
    if not raw:
        return "", ""
    if " - " in raw:
        name, _, artist = raw.partition(" - ")
        return name.strip(), artist.strip()
    return raw, ""


def _playback_from_state(state: dict, *, title_song_id: str | None) -> Playback | None:
    """state dict → :class:`Playback`；无曲目（无标题）返回 None。

    ``status`` 只有 ``playing`` / ``stopped`` 两个值（**暂停也是 stopped**，
    position 冻结，实测 2026-09-30）。对带标题的 stopped 仍返回
    ``playing=False`` 的快照——上层（歌词暂停冻结 / 唱歌宽限期）据此区分
    "暂停一下"与"真的没了"：前者保留进度等恢复，后者等 daemon 退出后由
    socket 门控自然归 None。
    """
    if not isinstance(state, dict):
        return None
    title_raw = str(state.get("title") or "").strip()
    status = str(state.get("status") or "").strip().lower()
    if status not in ("playing", "stopped") or not title_raw:
        return None
    name, artist = _split_title(title_raw)
    if not name:
        return None
    try:
        position = float(state.get("position"))
    except (TypeError, ValueError):
        position = None
    try:
        duration = float(state.get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    # ncm state 的 position 是 daemon 报的真值且实时上涨（实测 3s 间涨 3.3s）
    # ——比 Windows 网易云（SMTC 恒 0）好，直接当真值用。
    track = Track(title=name, artist=artist, duration=duration,
                  playing=(status == "playing"))
    return Playback(
        track=track,
        position=position,
        updated_at=time.monotonic(),
        app_id="ncm-cli",
        song_id=title_song_id or "",
    )


def _read_session_song_id() -> str | None:
    """从 ``play-session.json`` 读当前曲目的加密歌曲 ID（取词入参）。

    daemon 存活期间该文件必然存在、每次起播重写；读不到（缺文件/损坏/非 32
    位 hex）返回 None——上层写 WARNING、本会话这首歌不出歌词，**不做网络兜底**
    （2026-09-30 设计决策：失败只写日志）。
    """
    try:
        path = _config_dir() / "play-session.json"
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    song_id = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
    if not _SONG_ID_RE.match(song_id):
        return None
    return song_id


def _sample_once() -> Playback | None:
    """采一拍：socket 门控 → ``ncm-cli state`` → Playback。

    socket 不存在时直接返回 None 且**不起子进程**——state 客户端会自动把
    daemon 拉活，轮询等于每 3 秒复活一个退出的 daemon。
    """
    if not available():
        return None
    proc = _run_ncm(["state", "--output", "json"], timeout=STATE_TIMEOUT_SECONDS)
    if proc is None or proc.returncode != 0:
        return None
    state = _parse_state(proc.stdout)
    if state is None:
        return None
    song_id = _read_session_song_id()
    return _playback_from_state(state, title_song_id=song_id)


def sample_now() -> Playback | None:
    """同步采一拍（供需要即时结果的调用方；轮询请走 NcmSampler）。"""
    try:
        return _sample_once()
    except Exception:
        log.debug("ncm 采样失败", exc_info=True)
        return None


# ---------------------------------------------------------------- 采样线程


class NcmSampler:
    """进程级共享的常驻采样线程，维护「最近一次 state」快照。

    兼具 ``threading.Thread`` 风格的 ``is_alive`` / ``join``（生命周期断言
    与收口用），本体是组合而非继承：线程对象只在 ``start`` 后存在。

    - ``music_detect.is_music_playing``（GUI 线程，1s 唱歌定时器）只读快照；
    - ``current_playback``（歌词控制器采样线程）同样只读快照——**每拍直接调
      ``sample_now`` 会与 3s 节奏打架**，统一以本线程的节奏为准。
    快照比真实状态最多滞后一个轮询周期（3s）；暂停/停止的抖动由上层
    （唱歌宽限期 6s / 歌词暂停判定）吸收。
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        # 首个快照落地标志：prime() 等它，避免"刚 start 就读到 None"的空窗。
        self._primed = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._playing = False
        self._playback: Playback | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动采样线程（幂等；已停的实例不复活，调用方换新实例）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ncm-state-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求退出并等待线程结束（timeout 5s；卡在子进程上不强杀）。"""
        self._stop.set()
        self.join(timeout=5.0)

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if not thread.is_alive():
                self._thread = None

    # ------------------------------------------------------------ 读数

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def current_playback(self) -> Playback | None:
        with self._lock:
            return self._playback

    def prime(self, timeout: float = 2.0) -> None:
        """阻塞等到首个快照落地（或超时），供"刚 start 就要读数"的调用方。

        实机实测 ``ncm-cli state`` 单次 ~300ms；不 prime 的话歌词控制器在
        采样线程刚拉起的头几百毫秒里只能读到 None 快照——那次采样的
        ``_on_playback_ready(None)`` 会把刚建立的状态误清掉（表现为开启
        功能后第一拍空转）。等待上限远大于单次 state 耗时即可，超时就让
        调用方拿 None 走"没在放歌"分支。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._primed.is_set():
                return
            if not self.is_alive():
                return
            time.sleep(0.02)

    # ------------------------------------------------------------ 循环

    def _loop(self) -> None:
        # 先采一拍再等间隔：start() 后最多 STATE_POLL_SECONDS 出首个快照，
        # 与「开启功能立即检测一次」的既有体验一致。
        while not self._stop.is_set():
            try:
                playback = _sample_once()
            except Exception:
                log.debug("ncm 采样异常（按未播放处理）", exc_info=True)
                playback = None
            with self._lock:
                self._playback = playback
                self._playing = playback is not None and playback.track.playing
            self._primed.set()
            if self._stop.wait(STATE_POLL_SECONDS):
                break


# 进程级单例：music_detect 与歌词控制器共用一条采样线程。
# 测试用 monkeypatch 替换 NcmSampler 类本身来拦截创建。
_sampler: NcmSampler | None = None
_sampler_lock = threading.Lock()


def shared_sampler() -> NcmSampler:
    """取（或建）进程级共享采样器。绝不重复建线程。"""
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = NcmSampler()
        return _sampler


def is_shared_sampler_running() -> bool:
    sampler = _sampler
    return sampler is not None and sampler._thread is not None and sampler._thread.is_alive()


def current_playback_playing() -> bool:
    """GUI 线程的唱歌检测入口：读共享采样器快照，零子进程成本。

    采样器没起就拉起（首次调用）并 prime 到首个快照——否则开启功能后
    第一拍恒 False，桌宠要等一个轮询周期才开口。此后快照由线程持续
    刷新，直到进程退出。
    """
    sampler = shared_sampler()
    if not is_shared_sampler_running():
        sampler.start()
        sampler.prime()
    return sampler.is_playing()


def current_playback() -> Playback | None:
    """歌词控制器的采样入口：读共享采样器快照（不重复起子进程）。"""
    sampler = shared_sampler()
    if not is_shared_sampler_running():
        sampler.start()
        sampler.prime()
    return sampler.current_playback()


# ---------------------------------------------------------------- 取词


def fetch_lyrics_via_ncm(song_id: str | None) -> Lyrics | None:
    """按加密歌曲 ID 用 ``ncm-cli song lyric`` 取词。

    返回语义与 :func:`pet.music_lyric.fetch_lyrics` 对齐：

    - 有 LRC → ``Lyrics(lines=...)``；
    - ``pureMusic`` / 纯音乐占位文案 → ``Lyrics(instrumental=True)``；
    - ``noLyric`` / 只有非滚动 ``txtLyric``（无时间戳，一期不支持）→
      ``Lyrics()``（确定性「无词」：不出气泡，但照唱）；
    - 子进程失败 / 非 200 / 超时 → ``None``（瞬时失败，本会话当无词，
      写 WARNING 日志，**不做三源兜底、不重试**——2026-09-30 设计决策）。
    """
    song_id = str(song_id or "").strip()
    if not _SONG_ID_RE.match(song_id):
        log.warning("ncm 取词跳过：无效歌曲 ID %r", song_id)
        return None
    proc = _run_ncm(
        ["song", "lyric", "--songId", song_id, "--output", "json"],
        timeout=LYRIC_TIMEOUT_SECONDS,
    )
    if proc is None:
        return None
    if proc.returncode != 0:
        log.warning(
            "ncm 取词失败（exit %d）: %s",
            proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError):
        log.warning("ncm 取词返回非 JSON（前 200 字符）: %r", proc.stdout[:200])
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or int(payload.get("code") or 0) != 200:
        log.warning("ncm 取词接口未成功: code=%r", payload.get("code") if isinstance(payload, dict) else None)
        return None
    if data.get("pureMusic"):
        # pureMusic 优先于 noLyric：实测纯音乐两者常同时为 true（风之谷钢琴、
        # 晴天钢琴版），但语义上 pureMusic 更强——「这是首纯音乐」而不是
        # 「这首歌没词」。判成 noLyric 会让桌宠对着钢琴曲做唱歌动画。
        return Lyrics(instrumental=True)
    if data.get("noLyric"):
        return Lyrics()  # 服务端明确无词
    lrc = str(data.get("lyric") or "")
    lines = parse_lrc(lrc)
    if not lines:
        # 没有逐行 LRC、只有 txtLyric（非滚动文本）：视同无词。
        return Lyrics()
    from .music_lyric import _looks_instrumental
    if _looks_instrumental(lines):
        return Lyrics(instrumental=True)
    return Lyrics(lines=tuple(lines))


# ---------------------------------------------------------------- 平台


def ncm_supported() -> bool:
    """ncm 来源是否在当前平台启用（Linux 专属——Windows 走既有 pycaw/SMTC）。"""
    return sys.platform.startswith("linux")
