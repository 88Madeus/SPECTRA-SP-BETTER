"""Окна настройки полос, дефектных пикселей и коррекции интенсивности (без экрана)."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from spectra_better import processing as P  # noqa: E402
from spectra_better.config import AppSettings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    QMB = QtWidgets.QMessageBox
    for name in ("information", "warning"):
        monkeypatch.setattr(QMB, name, staticmethod(lambda *a, **k: QMB.Ok))


def test_stripes_dialog_find_and_limits(app):
    from spectra_better.gui.tools_dialogs import StripesDialog
    rows = np.arange(506)[:, None]
    img = 300 + 2000 * np.exp(-0.5 * ((rows - 100) / 3) ** 2) + 800 * np.exp(-0.5 * ((rows - 400) / 3) ** 2) \
        + np.zeros((1, 64))
    d = StripesDialog([(280, 310)], 506, img, lambda st: 0.5 * len(st))
    d._find()
    st = d.stripes()
    assert len(st) == 2 and st[0][0] <= 100 <= st[0][1] and st[1][0] <= 400 <= st[1][1]
    assert "Кадр ≈ 1.00 с" in d.info.text()
    for _ in range(10):
        d._add()
    assert len(d.stripes()) == 8 and not d.btn_add.isEnabled()


def test_defects_dialog_find(app):
    from spectra_better.gui.tools_dialogs import DefectPixelsDialog
    s = AppSettings()
    rng = np.random.default_rng(1)
    dark = 300 + rng.normal(0, 3, 2048)
    dark[[77, 1234]] += 500
    d = DefectPixelsDialog(s, 2048, dark)
    d._find()
    d._ok()
    assert s.defect_pixels == [77, 1234] and s.defect_correction


def test_intensity_dialog_from_lamp(app):
    from spectra_better.gui.tools_dialogs import IntensityCorrectionDialog
    s = AppSettings()
    x = np.linspace(450, 750, 2048)
    measured = P.planck(x, 2900) * (1.2 - 0.0008 * (x - 450))       # чувствительность падает к красному
    d = IntensityCorrectionDialog(s, x, measured)
    d.temp.setValue(2900)
    d._from_planck()
    d._ok()
    assert s.intensity_corr and s.intensity_corr_by == "wl" and len(s.intensity_corr_points) >= 30
    k = P.correction_curve(x, s.intensity_corr_points)
    assert k[-1] > k[0]                                               # к красному поправка больше
    d2 = IntensityCorrectionDialog(s)                                 # без эталона — только таблица
    assert not d2.btn_planck.isEnabled()
    d2.set_points([[500, -1.0], [600, 1.0]])
    d2._ok()                                                          # K ≤ 0 не принимается
    assert s.intensity_corr_points != [[500, -1.0], [600, 1.0]]
