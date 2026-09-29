"""Ярлыки на рабочем столе: скрипт PowerShell, «только при первом запуске», файл значка."""
import base64
import os
import struct
from pathlib import Path

import pytest

from spectra_better.tools import shortcuts as sc


def test_script_escapes_paths_and_keeps_cyrillic():
    root = Path(r"C:\Лаборатория\O'Brien\SpectraSP-Better")
    specs = sc.shortcut_specs(root, root / ".venv32" / "Scripts" / "pythonw.exe", str(root / sc.ICON_NAME) + ",0")
    script = sc.powershell_script(specs)
    assert script.count("CreateShortcut(") == 2 and script.count("$l.Save()") == 2
    assert "'SpectraSP Better (имитация).lnk'" in script
    assert "O''Brien" in script and "O'Brien" not in script.replace("O''Brien", "")
    assert "$l.Arguments = '-m spectra_better --simulate'" in script
    assert "GetFolderPath('Desktop')" in script
    assert script.startswith("$ErrorActionPreference") and "} catch {" in script


def test_powershell_gets_utf16_encoded_script(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "C:\\Users\\Иван\\Desktop\n".encode("utf-8"), b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()
    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    out = sc.run_powershell("Write-Output 'Спектр'")
    assert out == "C:\\Users\\Иван\\Desktop"
    enc = seen["cmd"][seen["cmd"].index("-EncodedCommand") + 1]
    assert base64.b64decode(enc).decode("utf-16-le") == "Write-Output 'Спектр'"


def test_powershell_error_message(monkeypatch):
    class R:
        returncode, stdout, stderr = 1, "ERROR: Доступ запрещён\n".encode("utf-8"), b"#< CLIXML <Objs/>"
    monkeypatch.setattr(sc.subprocess, "run", lambda cmd, **kw: R())
    with pytest.raises(RuntimeError, match="^Доступ запрещён$"):
        sc.run_powershell("x")


def test_created_only_once_and_after_move(tmp_path):
    calls = []

    def create(root):
        calls.append(root)
        return "Desktop"
    ok, text = sc.ensure_shortcuts(tmp_path, True, create)
    assert ok and len(calls) == 1 and "созданы" in text
    assert sc.ensure_shortcuts(tmp_path, True, create) is None          # второй запуск — ничего
    assert len(calls) == 1
    (tmp_path / sc.MARKER).write_text(r"D:\old\place", encoding="utf-8")   # папку перенесли
    assert sc.ensure_shortcuts(tmp_path, True, create)[0]
    assert len(calls) == 2
    assert sc.ensure_shortcuts(tmp_path, False, create)[0]              # create_shortcuts.bat — всегда
    assert len(calls) == 3


def test_failure_is_reported_once(tmp_path):
    def create(root):
        raise RuntimeError("COM запрещён")
    ok, text = sc.ensure_shortcuts(tmp_path, True, create)
    assert not ok and "COM запрещён" in text and "create_shortcuts.bat" in text
    assert sc.ensure_shortcuts(tmp_path, True, create) is None          # не мучаем при каждом запуске


def test_not_windows_is_quiet(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    assert sc.main(["--if-needed"]) == 0


def test_icon_file(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from spectra_better.gui.icons import ICO_SIZES, write_app_ico
    p = tmp_path / "a.ico"
    write_app_ico(str(p))
    data = p.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind, count) == (0, 1, len(ICO_SIZES))
    sizes = []
    for i in range(count):
        w, h, _, _, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + size <= len(data)
        sizes.append(w or 256)
    assert sizes == list(ICO_SIZES) and app is not None
