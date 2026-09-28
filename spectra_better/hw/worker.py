"""Выделенный поток для работы с драйверами.

Драйверы SpectraSP написаны на Delphi: DLL запоминает поток, в котором была
загружена, и создаёт в нём служебные окна. Поэтому загрузка DLL, все вызовы и
обработка оконных сообщений выполняются в одном потоке. Интерфейс программы при
этом не блокируется, даже если прибор долго отвечает или «завис».
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional

from .winutil import pump_messages

log = logging.getLogger(__name__)

_STOP = object()


class DeviceThread:
    def __init__(self, name: str, idle_interval: float = 0.02):
        self.name = name
        self._q: "queue.Queue" = queue.Queue()
        self._idle = idle_interval
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._busy_since: Optional[float] = None
        self._current: str = ""
        self._thread.start()

    # ----------------------------------------------------------------- API
    def submit(self, fn: Callable[..., Any], *args, label: str = "", **kwargs) -> Future:
        fut: Future = Future()
        self._q.put((fut, fn, args, kwargs, label or getattr(fn, "__name__", "call")))
        return fut

    def call(self, fn: Callable[..., Any], *args, timeout: Optional[float] = None, **kwargs) -> Any:
        """Синхронный вызов (из другого потока!) с ожиданием результата."""
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        return self.submit(fn, *args, **kwargs).result(timeout=timeout)

    @property
    def busy_for(self) -> float:
        """Сколько секунд выполняется текущая команда (0 — простаивает)."""
        t = self._busy_since
        return time.monotonic() - t if t is not None else 0.0

    @property
    def current_label(self) -> str:
        return self._current

    def is_current_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._q.put(_STOP)
        self._thread.join(timeout)

    # ------------------------------------------------------------- поток
    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=self._idle)
            except queue.Empty:
                pump_messages()
                continue
            if item is _STOP:
                break
            fut, fn, args, kwargs, label = item
            if not fut.set_running_or_notify_cancel():
                continue
            self._busy_since = time.monotonic()
            self._current = label
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001 — передаём вызывающему
                fut.set_exception(e)
            finally:
                self._busy_since = None
                self._current = ""
            pump_messages()
