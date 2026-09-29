"""Запуск графического интерфейса."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
import traceback

from .. import APP_NAME, __version__
from ..config import AppSettings, settings_dir


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

    def excepthook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("spectra_better").error("Необработанная ошибка:\n%s", text)
        try:
            QtWidgets.QMessageBox.critical(None, APP_NAME, f"Внутренняя ошибка программы:\n{exc}\n\n"
                                           "Подробности записаны в журнал.")
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = excepthook

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
