# -*- coding: utf-8 -*-
"""独立设置进程的「爹还在吗」看门狗。

为什么需要：设置进程是 ``QProcess.startDetached`` 拉起的**故意独立**的子进程，
桌宠退出（崩溃 / 会话结束 / 用户关掉）不会带走它。本机实测（2026-09-29）：

- 17:16 桌宠拉起设置进程；17:28 桌宠退出（会话结束路径），设置进程被 init 收养；
- 它继续活着 30 分钟，一直占着 ``settings.lock``；
- 于是此后每次点设置都只得到「独立设置进程已在运行，不重复拉起」，
  而且桌宠据此把气泡全抑制成「设置开着」——**看起来就是桌宠彻底不和 DSH 联动了**。

父进程一消失就自退（顺带把 ``settings.lock`` 交还），是这条链上最省事的断点。
平台差异：POSIX 上父死会被 init/子收割者收养，``os.getppid()`` 会变；Windows 上
``os.getppid()`` 语义不可靠，改用 ``OpenProcess(SYNCHRONIZE)`` +
``WaitForSingleObject`` 查退出。
"""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

WATCH_INTERVAL_MS = 2000


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """``pid`` 是否还存在。权限不足按"活着"处理（宁可多活一会儿，不乱退）。"""
    if int(pid) <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def parent_process_gone(recorded_ppid: int, *, getppid=None, pid_alive=None) -> bool:
    """记录的父进程是否已经消失。

    两条判据任一成立即算消失：``getppid()`` 变成了别人（被收养），或该 pid 已
    查不到。``getppid`` / ``pid_alive`` 可注入，便于无副作用地测这两条路。
    """
    getter = os.getppid if getppid is None else getppid
    alive = _pid_alive if pid_alive is None else pid_alive
    try:
        current = int(getter())
    except Exception:
        current = 0
    if int(recorded_ppid) > 0 and current > 0 and current != int(recorded_ppid):
        return True
    return not bool(alive(int(recorded_ppid)))


class ParentWatch(QObject):
    """定时轮询父进程；一旦消失就回调一次（幂等，之后表停）。"""

    def __init__(self, on_parent_gone, *, recorded_ppid=None, interval_ms: int = WATCH_INTERVAL_MS, parent=None):
        super().__init__(parent)
        self._on_gone = on_parent_gone
        self._ppid = int(os.getppid() if recorded_ppid is None else recorded_ppid)
        self._fired = False
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self.check)

    @property
    def recorded_ppid(self) -> int:
        return self._ppid

    @property
    def fired(self) -> bool:
        return self._fired

    def start(self) -> None:
        self.check()
        self._timer.start()

    def stop(self) -> None:
        """幂等收尾。

        对话框设了 ``WA_DeleteOnClose``：关窗时它自毁，会把挂在它下面的本 QTimer
        一起带走。收尾必须能在那之后照常调用，否则 ``_exec_settings`` 的 finally
        会在 ``lock.unlock()`` 之前抛出去，留下残余锁把后续设置入口堵住。
        """
        try:
            self._timer.stop()
        except RuntimeError:
            logger.debug("父进程看门狗计时器已被对话框自毁带走，无需停止", exc_info=True)

    def check(self) -> bool:
        """父进程若已消失：停表 + 回调一次，返回 True。"""
        if self._fired:
            return True
        if not parent_process_gone(self._ppid):
            return False
        self._fired = True
        self.stop()
        logger.info("父进程 %s 已退出：独立设置进程自退并释放 settings.lock", self._ppid)
        try:
            self._on_gone()
        except Exception:
            logger.exception("父进程消失回调失败")
        return True
