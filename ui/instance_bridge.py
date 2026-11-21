"""Lets a second launch of TimeLens ask the running one to show its window.

Only one TimeLens may run (single-instance lock). If the user starts it again - for
example from the Start menu while it sits in the tray - the new process sends "show" over
a local named pipe to the first one and exits.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "TimeLens.ShowWindow"


def notify_running_instance(name: str = SERVER_NAME, timeout_ms: int = 1500) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


class InstanceServer(QObject):
    show_requested = Signal()

    def __init__(self, name: str = SERVER_NAME):
        super().__init__()
        QLocalServer.removeServer(name)            # clear a stale pipe from a crashed run
        self.server = QLocalServer(self)
        self.listening = self.server.listen(name)
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self) -> None:
        # Any connection means "show the window". Do not wait for data: the second launch may
        # already have written and disconnected before we accepted, and readyRead would never fire.
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            if conn is not None:
                conn.disconnectFromServer()
                self.show_requested.emit()
