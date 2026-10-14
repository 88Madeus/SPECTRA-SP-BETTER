"""Запуск графического интерфейса."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import traceback

from typing import Callable, Optional, Set, Tuple

from .. import APP_NAME, __version__
from ..config import AppSettings, settings_dir


class ErrorReporter:
    """Необработанные ошибки: всегда в журнал, окно — без лавины.

    Ошибка при перерисовке графика повторяется на каждой перерисовке, а окно о ней само вызывает
    перерисовку — так программа «парализовалась» окнами в лаборатории. Поэтому: одна и та же ошибка
    (тип + место) показывается один раз за сеанс, и пока открыто одно окно, новые только пишутся в журнал.
    """

    def __init__(self, show: Optional[Callable[[str], None]] = None):
        self._show = show or self._message_box
        self._shown: Set[Tuple[str, str, int]] = set()
        self._showing = False

    @staticmethod
    def _message_box(text: str) -> None:
        from PyQt5 import QtWidgets
        QtWidgets.QMessageBox.critical(None, APP_NAME, text)

    def __call__(self, exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("spectra_better").error("Необработанная ошибка:\n%s", text)
        frames = traceback.extract_tb(tb)
        where = (frames[-1].filename, frames[-1].lineno) if frames else ("", 0)
        key = (getattr(exc_type, "__name__", str(exc_type)), *where)
        if self._showing or key in self._shown:
            return
        self._shown.add(key)
        self._showing = True
        try:
            self._show(f"Внутренняя ошибка программы:\n{exc}\n\nПодробности записаны в журнал. "
                       "Если она повторится, окно больше не появится — только запись в журнал.")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._showing = False


def setup_logging(level: int = logging.INFO):
    from .log_widget import QtLogHandler
    log_dir = os.path.join(settings_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"spectrasp-better_{_dt.date.today():%Y%m%d}.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"))
    root.addHandler(sh)
    qh = QtLogHandler()
    root.addHandler(qh)
    logging.getLogger("PyQt5").setLevel(logging.WARNING)
    logging.info("Журнал: %s", path)
    return qh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spectra_better", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--simulate", action="store_true", help="работа без оборудования (имитация камеры и монохроматора)")
    ap.add_argument("--spectrasp-dir", help="каталог установленной SpectraSP (по умолчанию ищется автоматически)")
    ap.add_argument("--settings", help="файл настроек (по умолчанию в профиле пользователя)")
    ap.add_argument("--debug", action="store_true", help="подробный журнал в консоли")
    args = ap.parse_args(argv)
    if args.settings:
        args.settings = os.path.abspath(args.settings)     # текущий каталог позже сменится

    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    from .icons import app_icon
    app.setWindowIcon(app_icon())            # тот же значок, что у ярлыков на рабочем столе
    qt_ru = QtCore.QTranslator(app)          # «Да»/«Нет»/«Отмена» в стандартных окнах Qt
    if qt_ru.load("qtbase_ru", QtCore.QLibraryInfo.location(QtCore.QLibraryInfo.TranslationsPath)):
        app.installTranslator(qt_ru)
    pg.setConfigOptions(background="w", foreground="k", antialias=True)

    handler = setup_logging(logging.DEBUG if args.debug else logging.INFO)
    settings = AppSettings.load(args.settings)
    if args.spectrasp_dir:
        settings.spectrasp_dir = args.spectrasp_dir

    sys.excepthook = ErrorReporter()

    from .main_window import MainWindow
    win = MainWindow(settings, simulate=args.simulate, log_handler=handler)
    win.show()
    rc = app.exec_()
    try:
        settings.save(args.settings)
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
