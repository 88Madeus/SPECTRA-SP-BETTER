"""Ярлыки программы на рабочем столе Windows.

run.bat вызывает эту утилиту с ключом --if-needed перед каждым запуском
программы: при первом запуске она создаёт на рабочем столе ярлыки
«SpectraSP Better» и «SpectraSP Better (имитация)» со значком программы, а
дальше ничего не делает (проверка — доли секунды). Ярлыки создаются заново,
только если папку программы перенесли. Удалённый вручную ярлык сам не
возвращается — для этого есть create_shortcuts.bat.

Ярлык запускает .venv32\\Scripts\\pythonw.exe -m spectra_better из папки
программы — так же, как run.bat, только без окна консоли.
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ICON_NAME = "SpectraSP-Better.ico"
MARKER = Path(".venv32") / "shortcuts_created.txt"
_QT_APP: list = []


@dataclass
class Shortcut:
    name: str            # имя ярлыка на рабочем столе (без .lnk)
    target: str
    args: str
    workdir: str
    icon: str
    description: str


def program_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pythonw_path(root: Path) -> Path:
    p = root / ".venv32" / "Scripts" / "pythonw.exe"
    if p.exists():
        return p
    alt = Path(sys.executable).with_name("pythonw.exe")
    return alt if alt.exists() else Path(sys.executable)


def shortcut_specs(root: Path, target: Path, icon: str) -> List[Shortcut]:
    common = dict(target=str(target), workdir=str(root), icon=icon)
    return [
        Shortcut(name="SpectraSP Better", args="-m spectra_better",
                 description="Управление ПЗС-камерой и монохроматором MS7504i", **common),
        Shortcut(name="SpectraSP Better (имитация)", args="-m spectra_better --simulate",
                 description="SpectraSP Better без приборов (режим имитации)", **common),
    ]


def _ps(s: str) -> str:
    """Строка PowerShell в одинарных кавычках."""
    return "'" + s.replace("'", "''") + "'"


ERROR_PREFIX = "ERROR: "


def powershell_script(shortcuts: List[Shortcut]) -> str:
    # ошибки печатаем сами в stdout: при -EncodedCommand PowerShell отдаёт stderr в виде CLIXML
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "try {",
        "$desk = [Environment]::GetFolderPath('Desktop')",      # в т. ч. рабочий стол в OneDrive
        "$ws = New-Object -ComObject WScript.Shell",
    ]
    for sc in shortcuts:
        lines += [
            f"$l = $ws.CreateShortcut([IO.Path]::Combine($desk, {_ps(sc.name + '.lnk')}))",
            f"$l.TargetPath = {_ps(sc.target)}",
            f"$l.Arguments = {_ps(sc.args)}",
            f"$l.WorkingDirectory = {_ps(sc.workdir)}",
            f"$l.IconLocation = {_ps(sc.icon)}",
            f"$l.Description = {_ps(sc.description)}",
            "$l.Save()",
        ]
    lines += [
        "[Console]::Out.WriteLine($desk)",
        "} catch {",
        f"[Console]::Out.WriteLine({_ps(ERROR_PREFIX)} + $_.Exception.Message)",
        "exit 1",
        "}",
    ]
    return "\n".join(lines)


def run_powershell(script: str) -> str:
    """Выполнить скрипт; текст передаётся в UTF-16 (-EncodedCommand), так что кириллица в путях не портится."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", encoded],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        msg = [line[len(ERROR_PREFIX):] for line in out.splitlines() if line.startswith(ERROR_PREFIX)]
        raise RuntimeError(msg[0] if msg else f"PowerShell завершился с кодом {r.returncode}")
    return out


def make_icon(root: Path) -> str:
    """Записать значок программы; вернуть IconLocation для ярлыка."""
    path = root / ICON_NAME
    try:
        from PyQt5 import QtGui
        from ..gui.icons import write_app_ico
        if QtGui.QGuiApplication.instance() is None:        # для рисования нужно приложение Qt
            _QT_APP.append(QtGui.QGuiApplication(["shortcuts"]))
        write_app_ico(str(path))
        return f"{path},0"
    except Exception as e:  # noqa: BLE001 — ярлык важнее значка
        print(f"Значок не создан ({e}), у ярлыков будет значок Python")
        return f"{pythonw_path(root)},0"


def create_shortcuts(root: Path) -> str:
    """Создать ярлыки; вернуть папку рабочего стола."""
    shortcuts = shortcut_specs(root, pythonw_path(root), make_icon(root))
    return run_powershell(powershell_script(shortcuts))


def _same_path(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a.strip())) == os.path.normcase(os.path.normpath(str(b)))


def ensure_shortcuts(root: Path, if_needed: bool,
                     create: Callable[[Path], str] = create_shortcuts) -> Optional[Tuple[bool, str]]:
    """Создать ярлыки (при if_needed — только в первый раз или после переноса папки).

    Возвращает None, если делать ничего не пришлось, иначе (успех, текст).
    Отметка пишется и при неудаче, чтобы не пытаться при каждом запуске.
    """
    marker = root / MARKER
    if if_needed and marker.exists():
        try:
            if _same_path(marker.read_text(encoding="utf-8"), root):
                return None
        except OSError:
            pass
    try:
        desk = create(root)
        result = f"Ярлыки «SpectraSP Better» и «SpectraSP Better (имитация)» созданы: {desk}"
        ok = True
    except Exception as e:  # noqa: BLE001
        result = (f"Не удалось создать ярлыки на рабочем столе: {e}\n\n"
                  "Программу можно запускать через run.bat, а создать ярлыки ещё раз — "
                  "через create_shortcuts.bat.")
        ok = False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(root), encoding="utf-8")
    except OSError:
        pass
    return ok, result


def _message_box(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "SpectraSP Better", 0x30)   # MB_ICONWARNING
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spectra_better.tools.shortcuts",
                                 description="Ярлыки SpectraSP Better на рабочем столе Windows")
    ap.add_argument("--if-needed", action="store_true",
                    help="только если ещё не создавались или папку программы перенесли (для run.bat)")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        print("Ярлыки создаются только в Windows")
        return 0 if args.if_needed else 1
    result = ensure_shortcuts(program_root(), args.if_needed)
    if result is None:
        return 0
    ok, text = result
    print(text)
    if not ok and args.if_needed:
        _message_box(text)          # окно консоли run.bat сразу закрывается — показываем окном
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
