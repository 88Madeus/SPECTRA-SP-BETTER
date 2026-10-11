"""Снимки экрана всех окон программы для README (в режиме имитации, без приборов).

    python -m spectra_better.tools.screenshots --out docs/screenshots

В Linux без экрана запускать с QT_QPA_PLATFORM=offscreen. Настройки пользователя
не трогаются: используется временный файл настроек и копия конфигурации
SpectraSP из tests/data/spectrasp (те же названия прибора, решёток и щелей).
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg

    tmp = tempfile.mkdtemp(prefix="spectrasp-shots-")
    sp_dir = os.path.join(tmp, "SpectraSP for MS7504i N020126")
    shutil.copytree(os.path.join(ROOT, "tests", "data", "spectrasp"), sp_dir)
    open(os.path.join(sp_dir, "SSPL_IM.dll"), "wb").close()      # чтобы каталог распознался как SpectraSP

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    qt_ru = QtCore.QTranslator(app)
    if qt_ru.load("qtbase_ru", QtCore.QLibraryInfo.location(QtCore.QLibraryInfo.TranslationsPath)):
        app.installTranslator(qt_ru)
    pg.setConfigOptions(background="w", foreground="k", antialias=True)

    from ..config import AppSettings
    from ..gui.icons import app_icon, app_icon_image
    from ..gui.log_widget import QtLogHandler
    from ..gui.main_window import MainWindow
    app.setWindowIcon(app_icon())
    handler = QtLogHandler()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    s = AppSettings()
    s._path = os.path.join(tmp, "settings.json")
    s.spectrasp_dir = sp_dir
    s.exposure_ms = 500
    s.x_top_unit = "px"
    s.save_dir = r"C:\Спектры"               # не показывать в README пути с лабораторного компьютера

    def pump(t: float = 0.3) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < t:
            app.processEvents()
            time.sleep(0.01)

    def save(widget, name: str) -> None:
        path = os.path.join(out, name)
        widget.grab().save(path, "PNG", 0)        # 0 — наибольшее сжатие PNG (без потерь)
        print("  ", os.path.relpath(path, ROOT))

    def wait(cond, timeout: float = 60.0) -> None:
        t0 = time.monotonic()
        while not cond() and time.monotonic() - t0 < timeout:
            pump(0.05)

    def capture_modal(trigger, name: str, prepare=None, finish="reject") -> None:
        """Открыть модальное окно (trigger), снять его и закрыть."""
        def grab():
            wdg = QtWidgets.QApplication.activeModalWidget()
            if wdg is None:
                QtCore.QTimer.singleShot(100, grab)
                return
            if prepare:
                prepare(wdg)
            pump(0.4)
            save(wdg, name)
            if isinstance(finish, str):
                getattr(wdg, finish)()
            else:
                finish(wdg)
        QtCore.QTimer.singleShot(300, grab)
        trigger()
        pump(0.3)

    w = MainWindow(s, simulate=True, log_handler=handler)
    w.resize(1400, 900)
    w.show()
    pump(0.5)
    w.connect_devices()
    wait(lambda: w.inst.camera_connected and w.inst.mono_connected and not w.tasks.active)
    inst = w.inst
    inst.camera.time_scale = 0.05             # охлаждение и экспозиции имитатора — быстрее
    inst.set_cooling(True, -20)
    wait(lambda: inst.cam_thread.call(inst.camera.get_state).temperature <= -19, 30)
    inst.set_grating(1)                       # 600 штр/мм — окно ~50 нм
    inst.goto_wavelength(565.0)
    w._update_mono_view()
    w._refresh_mono_misc()
    pump(1.6)                                 # опрос температуры камеры

    def snap():
        w.snap()
        wait(lambda: not w._acquiring())
        pump(0.3)

    print("Снимки в", out)
    # фон и спектр ртутно-неоновой лампы
    w.take_background()
    wait(lambda: not w._acquiring())
    s.exposure_ms = 250
    w.acq_panel.load_from_settings()
    snap()
    w.pin_current()
    s.exposure_ms = 500
    w.acq_panel.load_from_settings()
    w._acq_settings_changed()
    snap()
    pump(1.6)
    save(w, "main.png")

    # единицы оси: рамановский сдвиг снизу, нм сверху, заданный участок
    p = w.plot
    p.top_box.setCurrentIndex(p.top_box.findData("nm"))
    p.unit_box.setCurrentIndex(p.unit_box.findData("raman"))
    pump(0.3)
    p.x_from.setValue(300)
    p.x_to.setValue(1700)
    pump(0.3)
    save(w, "units.png")
    p.unit_box.setCurrentIndex(p.unit_box.findData("nm"))
    p.top_box.setCurrentIndex(p.top_box.findData("px"))
    p.show_all()
    pump(0.3)

    # изображение ПЗС
    w.acq_panel.mode.setCurrentIndex(w.acq_panel.mode.findData("image"))
    pump(0.2)
    w.snap()
    wait(lambda: not w._acquiring())
    w.tabs.setCurrentIndex(1)
    pump(0.8)
    save(w, "image.png")
    w.tabs.setCurrentIndex(0)

    # несколько полос строк (два «волокна») и интегрирование в участке
    inst.camera.fibers = [(150, 4, 1.0), (350, 4, 0.45)]
    inst.set_stripes([(140, 160), (338, 362)])
    w.acq_panel.load_from_settings()
    w._acq_settings_changed()
    w.snap()
    wait(lambda: not w._acquiring())
    w.tabs.setCurrentIndex(1)
    pump(0.8)
    save(w, "stripes.png")
    w.tabs.setCurrentIndex(0)
    w.acq_panel.mode.setCurrentIndex(w.acq_panel.mode.findData("spectrum"))
    s.remove_cosmics = True                   # космическая частица в кадре фона дала бы провал
    w.take_background()                       # фон для новой раскладки полос
    wait(lambda: not w._acquiring())
    snap()
    s.bands = [{"lo": 545.6, "hi": 546.6, "unit": "nm"}, {"lo": 576.5, "hi": 577.5, "unit": "nm"}]
    w.analysis_tabs.setCurrentWidget(w.bands_panel)
    w._redraw()
    pump(0.6)
    save(w, "analysis.png")
    s.bands = []
    s.remove_cosmics = False
    w.analysis_tabs.setCurrentIndex(0)
    inst.camera.fibers = []
    inst.set_stripes([(282, 311)])
    w.acq_panel.load_from_settings()
    w._acq_settings_changed()
    snap()

    # сервисное окно монохроматора
    w.mono_service()
    wait(lambda: w._service_dlg is not None and not w._service_dlg._busy, 20)
    pump(0.5)
    save(w._service_dlg, "service.png")
    w._service_dlg.close()
    pump(0.3)

    # диалоги
    def find_lines(dlg):
        for prefix in ("Найти реперы", "Рассчитать поправку"):
            for b in dlg.findChildren(QtWidgets.QPushButton):
                if b.text().startswith(prefix):
                    b.click()
            pump(0.5)
    capture_modal(w.calibration, "calibration.png", prepare=find_lines)
    capture_modal(w.series, "series.png")
    capture_modal(w.settings_dialog, "settings.png")

    # панорама 530–650 нм: окно параметров и результат
    def pano_range(dlg):
        spins = dlg.findChildren(QtWidgets.QDoubleSpinBox)
        if len(spins) >= 2:
            spins[0].setValue(530)
            spins[1].setValue(650)
    capture_modal(w.panorama, "panorama_dialog.png", prepare=pano_range, finish="accept")
    wait(lambda: not w._acquiring(), 120)
    wait(lambda: not inst.mono_busy(), 60)    # монохроматор вернулся на исходную длину волны
    pump(1.6)
    w.clear_overlays()
    p.show_all()
    pump(1.0)
    save(w, "panorama.png")

    # защита охлаждения: подтверждение уставки ниже 0 °C и выключение холодной ПЗС
    w.cam_panel.target.setValue(-30)
    capture_modal(w.cam_panel.btn_on.click, "cooling_confirm.png", finish="reject")
    capture_modal(w.cam_panel.btn_off.click, "cooling_off.png", finish="reject")
    w.cam_panel.target.setValue(20)

    # окно отключения: отогрев ПЗС и ожидание монохроматора
    inst.camera.time_scale = 0.3
    w.disconnect_devices()
    t0 = time.monotonic()
    shot = False
    while time.monotonic() - t0 < 60:
        pump(0.1)
        dlg = [d for d in app.topLevelWidgets() if d.__class__.__name__ == "BusyDialog" and d.isVisible()]
        if not dlg:
            break
        if not shot and "Отогрев" in dlg[0].label.text():
            save(dlg[0], "shutdown.png")
            shot = True

    app_icon_image(128).save(os.path.join(out, "icon.png"), "PNG", 0)
    print("  ", os.path.relpath(os.path.join(out, "icon.png"), ROOT))
    w._closing = True
    inst.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
