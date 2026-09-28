"""Вспомогательные функции Windows: загрузка 32-битных DLL, прокачка сообщений.

Все DLL SpectraSP — 32-битные (Delphi, соглашение stdcall), поэтому программа
должна запускаться 32-битным Python. На других платформах модуль импортируется,
но загрузка DLL выдаёт понятную ошибку (это нужно для тестов и режима имитации).
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import sys
from typing import Iterable, List, Optional

from .base import DeviceError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
PYTHON_BITS = struct.calcsize("P") * 8


def check_platform() -> None:
    if not IS_WINDOWS:
        raise DeviceError("Работа с оборудованием возможна только в Windows "
                          "(доступен режим имитации: --simulate).")
    if PYTHON_BITS != 32:
        raise DeviceError(
            "Драйверы SpectraSP 32-битные, а запущен 64-битный Python.\n"
            "Установите Python 3.12 (32-bit) с python.org и запустите программу им "
            "(см. README, раздел «Установка»).")


def _arg_size_x86(t) -> int:
    """Размер аргумента в 32-битном процессе (указатели — 4 байта на любой платформе)."""
    if t in (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_wchar_p) or \
            (isinstance(t, type) and issubclass(t, ctypes._Pointer)):
        return 4
    return ctypes.sizeof(t)


def stack_bytes(argtypes: Iterable) -> int:
    """Сколько байт стека занимают аргументы stdcall в 32-битном процессе (каждый выровнен до 4)."""
    return sum((_arg_size_x86(t) + 3) // 4 * 4 for t in argtypes)


def stdcall_function(dll, name: str, restype, argtypes: Iterable, arg_bytes: int):
    """Получить функцию stdcall и проверить размер аргументов.

    arg_bytes — число байт, снимаемых функцией со стека (операнд инструкции
    `ret N`, установлен дизассемблированием DLL). Несовпадение означало бы
    порчу стека, поэтому проверяем заранее.
    """
    argtypes = list(argtypes)
    total = stack_bytes(argtypes)
    if total != arg_bytes:
        raise AssertionError(f"{name}: аргументы занимают {total} байт, а функция снимает {arg_bytes}")
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


class DllDirectory:
    """Контекст: добавляет каталог SpectraSP в пути поиска DLL и делает его текущим.

    Драйверы подгружают зависимые библиотеки по короткому имени
    (psi_ccd5.dll, DevCtrl.dll, SOLCon.DLL), поэтому каталог должен быть виден.
    Текущий каталог процесса остаётся каталогом SpectraSP (как у самой
    SpectraSP): программа везде использует абсолютные пути, а восстановление
    каталога из двух параллельных потоков подключения было бы гонкой.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._cookie = None

    def __enter__(self):
        if not os.path.isdir(self.path):
            raise DeviceError(f"Не найден каталог SpectraSP: {self.path}")
        os.chdir(self.path)
        if IS_WINDOWS:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetDllDirectoryW(self.path)
            if hasattr(os, "add_dll_directory"):
                try:
                    self._cookie = os.add_dll_directory(self.path)
                except OSError:
                    self._cookie = None
        return self

    def __exit__(self, *exc):
        # Каталог поиска DLL оставляем установленным: драйверы могут
        # догружать библиотеки и позже.
        return False


def load_dll(path: str):
    """Загрузить DLL по полному пути (stdcall)."""
    if not os.path.isfile(path):
        raise DeviceError(f"Не найден файл драйвера: {path}")
    check_platform()
    try:
        return ctypes.WinDLL(path)
    except OSError as e:
        raise DeviceError(f"Не удалось загрузить {os.path.basename(path)}: {e}") from e


def free_dll(dll) -> None:
    if dll is None or not IS_WINDOWS:
        return
    try:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(dll._handle))
    except Exception:  # noqa: BLE001 — выгрузка не критична
        log.debug("FreeLibrary failed", exc_info=True)


if IS_WINDOWS:
    from ctypes import wintypes

    class _MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT)]

    _user32 = ctypes.WinDLL("user32")
    _PeekMessageW = _user32.PeekMessageW
    _PeekMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _PeekMessageW.restype = wintypes.BOOL
    _TranslateMessage = _user32.TranslateMessage
    _TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    _DispatchMessageW = _user32.DispatchMessageW
    _DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    _PM_REMOVE = 1

    def pump_messages(limit: int = 200) -> None:
        """Обработать оконные сообщения текущего потока.

        Драйверы на Delphi создают в потоке, загрузившем DLL, невидимые окна и
        таймеры; как и в SpectraSP, этот поток должен обрабатывать сообщения.
        """
        msg = _MSG()
        for _ in range(limit):
            if not _PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                break
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))
else:
    def pump_messages(limit: int = 200) -> None:  # noqa: D401 — заглушка
        return None


def ping(ip: str, timeout_ms: int = 3000) -> Optional[bool]:
    """ICMP-запрос, как делает SpectraSP перед подключением камеры.

    True — ответ получен, False — нет ответа, None — проверка недоступна.
    """
    if not IS_WINDOWS:
        return None
    import socket
    from ctypes import wintypes
    try:
        addr = struct.unpack("<I", socket.inet_aton(ip))[0]
    except OSError:
        return False
    icmp = ctypes.WinDLL("iphlpapi")
    icmp.IcmpCreateFile.restype = wintypes.HANDLE
    icmp.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.WORD,
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    icmp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    h = icmp.IcmpCreateFile()
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    try:
        payload = b"SpectraSPBetter"
        reply = ctypes.create_string_buffer(256)
        n = icmp.IcmpSendEcho(h, addr, payload, len(payload), None, reply, len(reply), timeout_ms)
        return n > 0 and struct.unpack_from("<I", reply.raw, 4)[0] == 0     # Status == IP_SUCCESS
    finally:
        icmp.IcmpCloseHandle(h)


def running_process_names() -> List[str]:
    """Имена запущенных процессов (только Windows, без сторонних модулей)."""
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    names: List[str] = []
    if snap in (0, -1):
        return names
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.append(entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return names


def spectrasp_is_running() -> bool:
    return any(n.lower() in ("spectrasp.exe", "devctrl.exe") for n in running_process_names())
