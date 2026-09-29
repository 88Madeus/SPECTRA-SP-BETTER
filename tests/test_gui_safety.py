"""Защиты в главном окне (имитатор, без экрана): охлаждение, перегрев, вентилятор, запущенная SpectraSP."""
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

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
