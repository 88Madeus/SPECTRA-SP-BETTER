import os
import struct

import numpy as np
import pytest

from spectra_better import logsp
from spectra_better.storage import load_spectrum, save_spectrum, unique_path
from spectra_better.tools.tune_spectrasp import plan_changes

from .conftest import DATA


@pytest.mark.parametrize("ext", [".txt", ".csv"])
def test_save_load_roundtrip(tmp_path, ext):
    x = np.linspace(400, 410, 50)
    y = np.random.default_rng(1).normal(1000, 30, 50)
    p = str(tmp_path / f"s{ext}")
    save_spectrum(p, x, y, {"exposure_ms": 1000, "grating": "№4: 600 штр/мм"}, extra={"pixel": np.arange(50)})
    x2, y2, meta, header = load_spectrum(p)
    assert np.allclose(x, x2, atol=1e-6) and np.allclose(y, y2, atol=1e-5)
    assert meta["exposure_ms"] == "1000"
    assert header[:2] == ["wavelength_nm", "intensity"]
    if ext == ".csv":
        assert "," in open(p, encoding="utf-8-sig").read().splitlines()[-1]      # десятичная запятая


def test_load_foreign_formats(tmp_path):
    p = tmp_path / "a.dat"
    p.write_text("400.5 12\n401.0 13.5\n", encoding="utf-8")
    x, y, _, _ = load_spectrum(str(p))
    assert list(x) == [400.5, 401.0] and list(y) == [12, 13.5]
    p.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    x, y, _, header = load_spectrum(str(p))
    assert list(x) == [1, 3] and header == ["x", "y"]


def test_unique_path(tmp_path):
    a = unique_path(str(tmp_path), "s", ".txt")
    open(a, "w").close()
    b = unique_path(str(tmp_path), "s", ".txt")
    assert a != b and b.endswith("s_002.txt")


def test_logsp_real_sample():
    recs = logsp.read(os.path.join(DATA, "sample_log.logsp"))
    assert len(recs) == 180
    assert recs[0].text.startswith("Mailslot Mon")
    assert any("Created SpectraSP ver. 1.1.9.4601" in r.text for r in recs)
    assert recs[0].time.year == 2026


def test_logsp_synthetic():
    def rec(days, level, text):
        body = struct.pack("<di", days, level) + text.encode("utf-8") + b"\0"
        body = body.ljust(64, b"\0")
        return bytes(b ^ 0xAA for b in body)
    data = b"LOGSPV01" + struct.pack("<II", 64, 2) + rec(46000.5, 4, "привет") + rec(46000.75, 2, "ok")
    recs = list(logsp.parse(data))
    assert [r.text for r in recs] == ["привет", "ok"]
    assert recs[0].time.hour == 12
    with pytest.raises(ValueError):
        list(logsp.parse(b"garbage" * 5))


def test_tune_only_touches_used_flags():
    text = open(os.path.join(DATA, "spectrasp", "SpectraSP.xml"), encoding="utf-8").read()
    new = plan_changes(text, ["Sensor_ANDOR", "Sensor_PSI_CCD_VER5_IR"])
    assert '<Sensor_ANDOR Used="false"/>' in new
    assert '<Sensor_PSI_CCD_VER5_IR Used="false"/>' in new
    assert '<Sensor_PSI_CCD_VER3 Used="true"/>' in new
    # кроме двух флагов ничего не изменилось
    diff = [(a, b) for a, b in zip(text.splitlines(), new.splitlines()) if a != b]
    assert len(diff) == 2
    assert "192.168." not in "".join(a + b for a, b in diff)
