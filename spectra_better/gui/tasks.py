"""Запуск блокирующих операций в фоне с доставкой результата в поток интерфейса."""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable, Optional

from PyQt5 import QtCore

from ..hw.base import DeviceError

log = logging.getLogger(__name__)


class _Relay(QtCore.QObject):
    done = QtCore.pyqtSignal(object, object)       # callback, result
    failed = QtCore.pyqtSignal(object, object)     # callback, exception


class TaskRunner(QtCore.QObject):
    """Выполняет функции в отдельных потоках Python.

    Обратные вызовы on_done/on_error вызываются в потоке интерфейса.
    """
    busy_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._relay = _Relay()
        self._relay.done.connect(self._on_done, QtCore.Qt.QueuedConnection)
        self._relay.failed.connect(self._on_failed, QtCore.Qt.QueuedConnection)
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        return self._active

    def run(self, fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None,
            on_error: Optional[Callable[[BaseException], None]] = None, name: str = "task") -> None:
        with self._lock:
            self._active += 1
            first = self._active == 1
        if first:
            self.busy_changed.emit(True)

        def work():
            try:
                result = fn()
            except BaseException as e:  # noqa: BLE001
                if not isinstance(e, DeviceError):
                    log.error("Непредвиденная ошибка в задаче %s:\n%s", name, traceback.format_exc())
                self._relay.failed.emit(on_error, e)
            else:
                self._relay.done.emit(on_done, result)

        threading.Thread(target=work, name=name, daemon=True).start()

    def _finish(self) -> None:
        with self._lock:
            self._active -= 1
            idle = self._active == 0
        if idle:
            self.busy_changed.emit(False)

    def _on_done(self, cb, result) -> None:
        try:
            if cb:
                cb(result)
        finally:
            self._finish()

    def _on_failed(self, cb, exc) -> None:
        try:
            if cb:
                cb(exc)
            else:
                log.error("%s", exc)
        finally:
            self._finish()


class Emitter(QtCore.QObject):
    """Передача объектов из рабочих потоков в интерфейс."""
    measurement = QtCore.pyqtSignal(object)
    progress = QtCore.pyqtSignal(int, int, str)
    message = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal(str, object)     # вид съёмки, поток
