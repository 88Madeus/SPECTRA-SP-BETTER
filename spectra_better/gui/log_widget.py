"""Журнал событий в окне программы."""
from __future__ import annotations

import logging

from PyQt5 import QtCore, QtGui, QtWidgets


class _Bridge(QtCore.QObject):
    record = QtCore.pyqtSignal(str, int)


class QtLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.bridge = _Bridge()
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.record.emit(self.format(record), record.levelno)
        except RuntimeError:     # окно уже закрыто
            pass


class LogView(QtWidgets.QPlainTextEdit):
    MAX_BLOCKS = 3000

    def __init__(self, handler: QtLogHandler, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        self.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        handler.bridge.record.connect(self.append_record, QtCore.Qt.QueuedConnection)

    def append_record(self, text: str, level: int) -> None:
        color = "#b00020" if level >= logging.ERROR else ("#a15c00" if level >= logging.WARNING else "#222222")
        esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")).replace("\n", "<br>")
        self.appendHtml(f'<span style="color:{color}">{esc}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
