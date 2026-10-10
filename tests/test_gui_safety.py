"""Защиты в главном окне (имитатор, без экрана): охлаждение, перегрев, вентилятор, запущенная SpectraSP."""
import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
from PyQt5 import QtCore  # noqa: E402

from spectra_better.config import AppSettings  # noqa: E402
from spectra_better.hw.base import CameraState  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, spectrasp_dir, monkeypatch, tmp_path):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.log_widget import QtLogHandler
    msgs = []
    QMB = QtWidgets.QMessageBox
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: msgs.append(("warning", a[1])) or QMB.Cancel))
    monkeypatch.setattr(QMB, "critical", staticmethod(lambda *a, **k: msgs.append(("critical", a[1])) or QMB.Ok))
    answers = {"question": QMB.Yes}
    monkeypatch.setattr(QMB, "question",
                        staticmethod(lambda *a, **k: msgs.append(("question", a[1])) or answers["question"]))
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s._path = str(tmp_path / "settings.json")
    w = mw.MainWindow(s, True, QtLogHandler())
    w.inst.connect_camera()
    w.inst.connect_mono()
    w._camera_connected(w.inst.camera.info)
    w._mono_connected(w.inst.mono.info)
    w.inst.camera.time_scale = 0.01
    w.msgs, w.answers = msgs, answers
    yield w
    w._closing = True
    w.poll_timer.stop()
    w.inst.shutdown()
    w.deleteLater()


def pump(app, t=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < t:
        app.processEvents()
        time.sleep(0.01)


def cam(w):
    return w.inst.camera


def test_deep_cooling_needs_confirmation(win, app):
    win.answers["question"] = QtWidgets.QMessageBox.No
    win.cooling_on(-30)
    pump(app)
    assert cam(win)._target is None                          # отказались — уставка не ушла в камеру
    win.answers["question"] = QtWidgets.QMessageBox.Yes
    win.cooling_on(-30)
    pump(app)
    assert cam(win)._target == -30
    n = len(win.msgs)
    win.cooling_on(15)                                       # выше 0 °C — без вопросов
    pump(app)
    assert cam(win)._target == 15 and len(win.msgs) == n


def test_cooling_off_warms_up_first(win, app, monkeypatch):
    cam(win).set_cooling(True, -20)
    cam(win)._temp = -20.0
    win.inst.last_state = CameraState(temperature=-20, cooling_state=2, fan_on=True)
    monkeypatch.setattr(win, "_ask_cooling_off", lambda t: "warm")
    win.cooling_off()
    pump(app)
    assert win._warm_then_off and cam(win)._target == 2      # сначала уставка +2 °C, охлаждение не выключено
    win._camera_safety(CameraState(temperature=-8, cooling_state=1, fan_on=True))
    pump(app)
    assert cam(win)._target == 2
    win._camera_safety(CameraState(temperature=1, cooling_state=2, fan_on=True))
    pump(app)
    assert not win._warm_then_off and cam(win)._target is None    # отогрелась — теперь выключено


def test_cooling_off_cancel_keeps_cooling(win, app, monkeypatch):
    cam(win).set_cooling(True, -20)
    win.inst.last_state = CameraState(temperature=-20, cooling_state=2, fan_on=True)
    monkeypatch.setattr(win, "_ask_cooling_off", lambda t: "")
    win.cooling_off()
    pump(app)
    assert cam(win)._target == -20


def test_overheat_switches_cooling_off_and_stops(win, app):
    cam(win).set_cooling(True, -10)
    stop = threading.Event()
    win._acq_kind = "live"
    win._acq_thread = threading.Thread(target=stop.wait, daemon=True)
    win._acq_thread.start()
    hot = CameraState(temperature=-10, cooling_state=1, fan_on=True, overheat=True)
    win._camera_safety(hot)                                  # единичный сбой — не реагируем
    pump(app)
    assert cam(win)._target == -10
    win._camera_safety(hot)
    pump(app)
    assert cam(win)._target is None and win._acq_stop.is_set()
    assert ("critical", "Перегрев камеры") in win.msgs
    n = len(win.msgs)
    win._camera_safety(hot)                                  # не повторяем окно на каждом опросе
    assert len(win.msgs) == n
    stop.set()


def test_fan_warning_once(win):
    st = CameraState(temperature=-5, cooling_state=2, fan_on=False)
    win._camera_safety(st)
    win._camera_safety(st)
    assert [m for m in win.msgs if m[1] == "Вентилятор камеры"] == [("warning", "Вентилятор камеры")]


def test_spectrasp_started_blocks_control(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    monkeypatch.setattr(mw, "spectrasp_is_running", lambda: True)
    win._check_foreign()
    assert win._foreign and not win.act_snap.isEnabled() and not win.mono_panel.btn_go.isEnabled()
    assert ("warning", "SpectraSP запущена") in win.msgs
    wl = win.inst.mono_wl
    win.goto_wavelength(wl + 10)
    pump(app)
    assert win.inst.mono_wl == wl                            # команда не ушла
    monkeypatch.setattr(mw, "spectrasp_is_running", lambda: False)
    win._check_foreign()
    assert not win._foreign and win.act_snap.isEnabled() and win.mono_panel.btn_go.isEnabled()


def test_connect_refused_while_spectrasp_runs(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    win.inst.disconnect(warm_up=False)
    win.simulate = False                                     # проверка SpectraSP не делается только в имитации
    monkeypatch.setattr(mw, "spectrasp_is_running", lambda: True)
    win.connect_devices()                                    # ответ «Отмена»
    pump(app)
    assert not win.inst.camera_connected and not win.inst.mono_connected
    win.simulate = True


def test_mono_panel_locked_during_panorama(win):
    stop = threading.Event()
    win._acq_kind = "panorama"
    win._acq_thread = threading.Thread(target=stop.wait, daemon=True)
    win._acq_thread.start()
    win._update_actions()
    assert not win.mono_panel.btn_go.isEnabled() and not win._mono_ready()
    stop.set()
    win._acq_thread.join(2)
    win._update_actions()
    assert win.mono_panel.btn_go.isEnabled() and win._mono_ready()


def test_laser_needs_confirmation(win, app, monkeypatch):
    QMB = QtWidgets.QMessageBox
    answer = {"v": QMB.No}
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: win.msgs.append(("warning", a[1]))
                                                     or answer["v"]))
    win.cam_panel.btn_laser.click()                          # «нет» — лазер не включается
    pump(app)
    assert not win.inst.laser_on and not win.cam_panel.btn_laser.isChecked()
    answer["v"] = QMB.Yes
    win.cam_panel.btn_laser.click()
    pump(app)
    assert win.inst.laser_on and not win.lbl_laser.isHidden()     # «ЛАЗЕР ВКЛ» в строке состояния
    assert win.cam_panel.btn_laser.text() == "Выключить"
    win.cam_panel.btn_laser.click()                          # выключение — без вопросов
    pump(app)
    assert not win.inst.laser_on and win.lbl_laser.isHidden()
    assert [m for m in win.msgs if m[1] == "Включение лазера"] == [("warning", "Включение лазера")] * 2


def test_constant_high_on_other_output_asks(win, app):
    from spectra_better.hw.base import SYNC_LOGICAL_1
    win.answers["question"] = QtWidgets.QMessageBox.No
    win.set_sync_out(1, SYNC_LOGICAL_1)
    pump(app)
    assert win.inst.camera.sync_out[0] != SYNC_LOGICAL_1
    win.answers["question"] = QtWidgets.QMessageBox.Yes
    win.set_sync_out(1, SYNC_LOGICAL_1)
    pump(app)
    assert win.inst.camera.sync_out[0] == SYNC_LOGICAL_1


def test_setpoint_change_stops_live_and_blocks_until_reached(win, app):
    cam(win)._update_temp = lambda: None                     # «заморозить» тепловую модель
    win.s.warm_up_on_close = False                           # (и не ждать отогрева при выходе)
    cam(win)._temp = 20.0
    win.inst.camera_state()
    win.live()
    pump(app, 0.5)
    assert win._acquiring()
    win.cooling_on(-5)                                       # подтверждение — «Да» (фикстура)
    pump(app, 1.0)
    assert not win._acquiring()                              # непрерывная съёмка остановлена
    win._update_actions()
    assert not win.act_snap.isEnabled() and not win.act_live.isEnabled() and not win.act_bg.isEnabled()
    win.snap()                                               # даже в обход кнопки — не запускается
    assert not win._acquiring()
    cam(win)._temp = -5.0                                    # температура установилась
    win.inst.camera_state()
    win._update_actions()
    assert win.act_snap.isEnabled()


def test_shutter_button_shows_confirmed_state(win, app):
    btn = win.mono_panel.btn_shutter
    win._refresh_mono_misc()
    pump(app, 0.5)
    assert btn.text().startswith("открыта")
    btn.click()                                              # закрыть
    t0 = time.monotonic()
    while win._shutter_task and time.monotonic() - t0 < 10:
        pump(app, 0.05)
    assert btn.text().startswith("закрыта") and not btn.isChecked()
    # шторку открыли в штатном окне DevCtrl — опрос показывает это на кнопке
    win.inst.mono.shutter_open = True
    for _ in range(2):
        win._poll()
        pump(app, 0.3)
    assert btn.text().startswith("открыта") and btn.isChecked()


def test_shutter_mode_selector(win, app):
    panel = win.mono_panel
    assert panel.shutter_mode.isEnabled() and panel.shutter_mode.currentData() is False
    panel.shutter_mode.setCurrentIndex(1)
    panel.shutter_mode.activated.emit(1)                     # «по экспозиции»
    t0 = time.monotonic()
    while (win._shutter_task or not win.inst.shutter_ttl) and time.monotonic() - t0 < 10:
        pump(app, 0.05)
    assert win.inst.shutter_ttl and not panel.btn_shutter.isEnabled()
    assert "время кадра" in panel.btn_shutter.text()
    win._update_actions()                                    # общая разблокировка панели не включает кнопку
    assert not panel.btn_shutter.isEnabled()
    panel.shutter_mode.setCurrentIndex(0)
    panel.shutter_mode.activated.emit(0)
    t0 = time.monotonic()
    while (win._shutter_task or win.inst.shutter_ttl) and time.monotonic() - t0 < 10:
        pump(app, 0.05)
    pump(app, 0.5)
    assert not win.inst.shutter_ttl and panel.btn_shutter.isEnabled()


def _snap(win, app):
    win.snap()
    t0 = time.monotonic()
    while win._acquiring() and time.monotonic() - t0 < 20:
        pump(app, 0.05)
    pump(app, 0.2)


def test_spectrum_buffer_in_window(win, app):
    bp = win.buffer_panel
    win.clear_overlays()
    _snap(win, app)
    win.act_pin.trigger()                                    # «В буфер»
    assert len(win.buffer) == 1 and bp.list.count() == 1
    key = win.buffer.entries[0].key
    assert key in win.plot.overlays and win.plot.overlays[key].isVisible()
    bp.list.item(0).setCheckState(QtCore.Qt.Unchecked)       # снять отметку — спектр уходит с графика
    assert key not in win.plot.overlays
    bp.list.item(0).setText("эталон")                        # переименование
    assert win.buffer.entries[0].name == "эталон"
    bp.auto.setChecked(True)                                 # каждый снимок — в буфер
    _snap(win, app)
    assert len(win.buffer) == 2 and win.s.buffer_auto
    win.live()                                               # кадры непрерывной съёмки — нет
    pump(app, 0.8)
    win.stop()
    t0 = time.monotonic()
    while win._acquiring() and time.monotonic() - t0 < 20:
        pump(app, 0.05)
    assert len(win.buffer) == 2
    win.buffer_to_current(key)                               # «сделать текущим»
    assert win.current is win.buffer.get(key).m
    win.buffer.save_state(win._buffer_path())                # переживает перезапуск
    from spectra_better.buffer import SpectrumBuffer
    again = SpectrumBuffer()
    assert again.load_state(win._buffer_path()) == 2 and again.entries[0].name == "эталон"


def test_stripes_in_window(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    win.inst.camera.fibers = [(150, 3, 1.0), (350, 3, 0.4)]
    win.inst.camera._update_temp = lambda: None
    win.s.warm_up_on_close = False

    class FakeDlg:
        def __init__(self, stripes, height, img, estimate, parent):
            self.est = estimate([(140, 160), (340, 360)])

        def exec_(self):
            return QtWidgets.QDialog.Accepted

        def stripes(self):
            return [(140, 160), (340, 360)]
    monkeypatch.setattr(mw, "StripesDialog", FakeDlg)
    win.edit_stripes()
    assert win.inst.stripes() == [(140, 160), (340, 360)]
    assert not win.acq_panel.hw_bin.isEnabled() and "2 полосы" in win.acq_panel.stripes_info.text()
    assert len(win.image.extra_regions) == 1                  # вторая полоса нарисована на кадре
    _snap(win, app)
    assert win.current.n_tracks == 2 and len(win.plot.track_curves) == 1
    win.clear_overlays()
    win.pin_current()                                        # в буфер — обе полосы
    assert [e.name.split("·")[-1].strip() for e in win.buffer.entries] == ["полоса 1 (140–160)",
                                                                            "полоса 2 (340–360)"]
    win.image.extra_regions[0].setRegion((300, 321))          # перетащили вторую полосу
    win.image._region_changed()
    assert win.inst.stripes()[1] == (300, 320)
    win.inst.set_stripes([(140, 160)])
    win._acq_settings_changed()
    assert win.image.extra_regions == [] and win.acq_panel.hw_bin.isEnabled()


def test_corrections_panel_reflects_settings(win, app):
    p = win.proc_panel
    assert not p.defects.isEnabled() and not p.intensity.isEnabled()      # пустые списки — выключено
    win.s.defect_pixels = [10, 20]
    win.s.intensity_corr_points = [[400, 1.0], [800, 2.0]]
    win.s.intensity_corr = True
    p.load_corrections()
    assert p.defects.isChecked() and "(2)" in p.defects.text()
    assert p.intensity.isChecked() and "2 точ." in p.intensity.text()
    p.intensity.setChecked(False)
    assert not win.s.intensity_corr


def test_band_integration_and_history(win, app, tmp_path):
    win.inst.camera._update_temp = lambda: None
    win.s.warm_up_on_close = False
    win.s.bands = []
    win.goto_wavelength(546.0)
    pump(app, 1.0)
    _snap(win, app)
    win.add_band()
    assert len(win.s.bands) == 1 and win.s.bands[0]["unit"] == "nm"
    win.s.bands[0].update(lo=545.5, hi=546.6)                 # вокруг линии ртути 546.07 нм
    win._redraw()
    res = win._band_results(win.current)[0]
    assert res is not None and res.net > 1000 and abs(res.centroid - 546.07) < 0.15
    assert win.plot.band_items[0].isVisible()
    assert win.bands_panel.table.item(0, 3).text() not in ("", "—")
    win.plot.band_items[0].setRegion((545.0, 547.0))          # перетащили на графике
    win.plot._band_changed(0)
    assert win.s.bands[0]["lo"] == pytest.approx(545.0) and win.s.bands[0]["hi"] == pytest.approx(547.0)
    win.band_history.clear()
    for _ in range(3):
        _snap(win, app)
    t, v = win.band_history.series(0, "net")
    assert len(v) == 3 and np.all(v > 0)
    path = tmp_path / "h.csv"
    win.band_history.save_csv(str(path), "nm")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("time;seconds;band1_net") and len(lines) == 4
    win.show_band_history()
    assert "снимков: 3" in win._hist_win.info.text()
    win._hist_win.close()


def test_calculator_difference_into_buffer(win, app):
    win.inst.camera._update_temp = lambda: None
    win.s.warm_up_on_close = False
    win.clear_overlays()
    win.goto_wavelength(546.0)                                # линии ртути — есть пики
    pump(app, 1.0)
    _snap(win, app)
    win.pin_current()
    win.calculator()
    c = win._calc
    c.a.setCurrentIndex(0)                                    # текущий
    c.b.setCurrentIndex(1)                                    # он же из буфера
    c.op.setCurrentIndex(c.op.findData("sub"))
    m = c.calculate()
    assert m is not None and np.nanmax(np.abs(m.y)) < 1e-9     # разность с самим собой — ноль
    assert len(win.buffer) == 2 and "−" in win.buffer.entries[-1].name
    assert win.buffer.entries[-1].props["formula"].startswith("[Текущий спектр] −")
    c.op.setCurrentIndex(c.op.findData("norm_max"))
    m = c.calculate()
    assert np.nanmax(m.y) == pytest.approx(1.0)
    win.bands_from_peaks()
    assert 1 <= len(win.s.bands) <= win.MAX_BANDS
    c.close()


def test_export_plot_png_svg(win, app, tmp_path, monkeypatch):
    _snap(win, app)
    QFD = QtWidgets.QFileDialog
    for name in ("g.png", "g.svg"):
        target = str(tmp_path / name)
        monkeypatch.setattr(QFD, "getSaveFileName", staticmethod(lambda *a, **k: (target, "")))
        win.export_plot()
        assert os.path.getsize(target) > 1000
    assert (tmp_path / "g.png").read_bytes()[:4] == b"\x89PNG"
    assert b"<svg" in (tmp_path / "g.svg").read_bytes()[:2000]
