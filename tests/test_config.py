import json

from spectra_better.calibration import CalibrationSettings, Correction
from spectra_better.config import AppSettings
from spectra_better.spectrasp_config import find_spectrasp_dir, read_installation, read_mono_cfg


def test_read_installation(spectrasp_dir):
    inst = read_installation(spectrasp_dir)
    assert inst.sensor.ip == "192.168.240.181"
    assert inst.sensor.sensor_type == "PSI_VER3"
    assert inst.sensor.readout_speed == 250
    assert inst.sensor.calib_center_pixel == 469
    assert inst.devctrl.address == "192.168.120.120"
    assert inst.devctrl.interface_text == "Ethernet"
    assert inst.stripe == (282, 311)
    assert inst.main.wl_presets[:3] == [290.0, 320.0, 460.0]
    assert inst.main.used_sensors["Sensor_PSI_CCD_VER3"] is True
    assert inst.sensor.sync_out == (0, 6)        # «Выход синхр. 1/2»: EXP polar negative / LOGICAL 0


def test_mono_cfg(spectrasp_dir):
    mc = read_mono_cfg(spectrasp_dir)
    assert mc.caption == "MS7504i"
    assert mc.serial == "20126"
    assert [g.lines_per_mm for g in mc.gratings] == [1800, 600, 1800, 600]
    assert mc.slits == ["Entrance Slit", "Exit Slit Lateral"]
    # индекс 0 в SSPL соответствует позиции 2 DevCtrl (выход на ПЗС)
    assert mc.mirror_positions[0].startswith("Top")
    assert mc.has_shutter
    assert mc.current_grating == 3
    assert mc.path.endswith("MS7504_20126.cfg")       # а не *_old.cfg
    # пределы шаговых двигателей для сервисного окна
    assert mc.step_limits[("grating", 0)] == 960000
    assert mc.step_limits[("slit", 0)] == mc.step_limits[("slit", 1)] == 4262
    assert mc.step_limits[("mirror", 0)] == 48700
    assert 69538 < mc.step_limits[("turret", 0)] < 69538 + 20000   # чуть дальше последней позиции турели
    # (4262 − 299 нулевое положение) × 0,5 мкм/шаг
    assert mc.slit_max_um == [1981.0, 2010.0]


def test_find_dir(spectrasp_dir, tmp_path):
    assert find_spectrasp_dir(spectrasp_dir) == spectrasp_dir
    assert find_spectrasp_dir(str(tmp_path / "nope")) in (None, find_spectrasp_dir(None))


def test_settings_roundtrip(tmp_path):
    s = AppSettings()
    s.exposure_ms = 123.5
    s.wl_presets = [500.0, 656.28]
    s.calibration = CalibrationSettings(center_pixel=469, corrections={3: Correction([0.1, -2.0], [(10.0, 500.0)])})
    p = tmp_path / "s.json"
    s.save(str(p))
    s2 = AppSettings.load(str(p))
    assert s2.exposure_ms == 123.5
    assert s2.wl_presets == [500.0, 656.28]
    assert s2.calibration.center_pixel == 469
    assert s2.calibration.corrections[3].coeffs == [0.1, -2.0]


def test_settings_corrupted(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert AppSettings.load(str(p)).exposure_ms == AppSettings().exposure_ms
    p.write_text(json.dumps({"exposure_ms": "abc", "accumulations": 5}), encoding="utf-8")
    s = AppSettings.load(str(p))
    assert s.accumulations == 5 and s.exposure_ms == AppSettings().exposure_ms


def test_cooling_setpoint_not_remembered(tmp_path):
    # при запуске в поле уставки всегда безопасные +20 °C; старое значение из файла не подхватывается
    from spectra_better.config import COOLING_TARGET_DEFAULT_C
    assert COOLING_TARGET_DEFAULT_C == 20
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"cooling_target_c": -20, "exposure_ms": 50}), encoding="utf-8")
    s = AppSettings.load(str(p))
    assert s.exposure_ms == 50 and not hasattr(s, "cooling_target_c")
