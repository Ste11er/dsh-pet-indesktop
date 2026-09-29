# -*- coding: utf-8 -*-
"""独立设置进程的「把已有窗口叫到前台」通道。

为什么需要：设置是单实例的（``settings.lock``）。已有设置进程在跑时，主进程按
设计不再拉起第二份，只把 ``_settings_child_active`` 置真；如果那扇窗口被别的
应用压在后面，用户看到的就是**「点设置没反应」**——本机实测（2026-09-29，KWin）：
17:16 打开的设置窗被 WPS / ChatGPT 盖住，之后每次点设置都只得到一行
「独立设置进程已在运行，不重复拉起」，窗口始终不见天日。

命名沿用 ``pet/collision_ipc.py::collision_server_name`` 的规范：本机命名服务 +
摘要后缀。POSIX 上它会落成 ``/tmp/<name>`` 的 Unix socket 路径，所以名字必须短；
粒度与它守护的单实例一致——``settings.lock`` 也按 ``config.dir`` 而不是按
``--instance``（各槽位共用一扇设置窗，这里同样只按目录）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .config import APP_DIR_NAME

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "dsh-pet-settings"
RAISE_COMMAND = "raise"
DEFAULT_TIMEOUT_MS = 250  # 本机 socket 正常是微秒级；只有"进程在但没在听"才会走满


def _identity() -> str:
    if sys.platform == "win32":
        return os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    return str(getattr(os, "getuid", lambda: 0)())


def channel_name(config_dir) -> str:
    """按配置目录派生本机通道名（同目录 = 同一扇设置窗 = 同一个名字）。"""
    namespace = f"{APP_DIR_NAME}\0{_identity()}\0{Path(config_dir).resolve()}"
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:20]
    return f"{CHANNEL_PREFIX}-{digest}"


def raise_existing_settings(config_dir, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    """请已有设置进程把窗口叫到前台；失败一律返回 False（不外抛，也不久等）。

    超时是硬上限：宁可这一次没唤起，也绝不允许把主进程 UI 卡住。
    """
    socket = QLocalSocket()
    try:
        socket.connectToServer(channel_name(config_dir))
        if not socket.waitForConnected(int(timeout_ms)):
            logger.info("设置唤起通道连不上：%s", socket.errorString())
            return False
        socket.write((RAISE_COMMAND + "\n").encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(int(timeout_ms))
        return True
    except Exception:
        logger.exception("请已有设置进程唤起窗口失败")
        return False
    finally:
        try:
            socket.disconnectFromServer()
        except Exception:
            logger.debug("关闭设置唤起客户端失败", exc_info=True)


class SettingsRaiseServer(QObject):
    """设置进程侧：监听唤起请求，收到 ``raise`` 就发 ``raise_requested``。

    best-effort——起不来只记日志，绝不影响设置页本身。``start()`` 里的
    ``removeServer`` 是安全的：调用点已经先拿到了 ``settings.lock``，也就是
    此刻必然没有另一个活着的设置进程，残留端点只可能是陈旧文件。
    """

    raise_requested = Signal()

    def __init__(self, config_dir, parent=None) -> None:
        super().__init__(parent)
        self.name = channel_name(config_dir)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)

    def start(self) -> bool:
        if self._server.isListening():
            return True
        QLocalServer.removeServer(self.name)
        if not self._server.listen(self.name):
            logger.warning("设置唤起通道监听失败：%s", self._server.errorString())
            return False
        return True

    def stop(self) -> None:
        """幂等收尾：对话框 ``WA_DeleteOnClose`` 自毁把 QLocalServer 带走后也要能调。"""
        try:
            if self._server.isListening():
                self._server.close()
        except RuntimeError:
            logger.debug("设置唤起通道对象已被对话框自毁带走，跳过关闭", exc_info=True)
        except Exception:
            logger.debug("关闭设置唤起通道失败", exc_info=True)
        try:
            # 静态调用：上面的 C++ 对象即使已经没了，端点文件也要清掉
            QLocalServer.removeServer(self.name)
        except Exception:
            logger.debug("清理设置唤起通道端点失败", exc_info=True)

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                continue
            conn.readyRead.connect(lambda c=conn: self._on_payload(c))
            if conn.bytesAvailable():
                self._on_payload(conn)

    def _on_payload(self, conn) -> None:
        try:
            payload = bytes(conn.readAll()).decode("utf-8", "replace").strip()
        except Exception:
            payload = ""
        if payload == RAISE_COMMAND:
            self.raise_requested.emit()
        try:
            conn.disconnectFromServer()
        except Exception:
            logger.debug("断开设置唤起连接失败", exc_info=True)
