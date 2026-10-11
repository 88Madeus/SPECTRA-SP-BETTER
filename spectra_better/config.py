"""Собственные настройки программы (JSON в профиле пользователя).

Адреса устройств здесь НЕ хранятся: они каждый раз читаются из файлов
установленной SpectraSP, поэтому обе программы всегда используют одни и те же
порты. Поле camera_ip_override нужно только для особых случаев и по умолчанию
пустое.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import List, Optional

from .calibration import CalibrationSettings
from .units import X_NM

log = logging.getLogger(__name__)

ACQ_SPECTRUM = "spectrum"
ACQ_IMAGE = "image"

# Уставка в поле при каждом запуске. Выше комнатной почти не охлаждает, поэтому
# случайное нажатие «Задать» не заморозит ПЗС. Прошлая уставка не запоминается.
COOLING_TARGET_DEFAULT_C = 20


def settings_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "SpectraSP-Better")
    return os.path.join(os.path.expanduser("~"), ".config", "spectrasp-better")


_PAIR_LISTS = ("extra_stripes", "intensity_corr_points")
_INT_LISTS = ("defect_pixels",)
_DICT_LISTS = ("bands",)


def _list_value(name: str, v) -> list:
    """Списки из файла настроек: числа, пары чисел или словари — по имени поля."""
    if not isinstance(v, list):
        raise TypeError("ожидается список")
    if name in _PAIR_LISTS:
        return [[float(a), float(b)] for a, b in v]
    if name in _INT_LISTS:
        return [int(x) for x in v]
    if name in _DICT_LISTS:
        return [dict(x) for x in v if isinstance(x, dict)]
    return [float(x) for x in v]


@dataclass
class AppSettings:
    spectrasp_dir: str = ""
    camera_dll: str = "auto"
    camera_ip_override: str = ""

    # экспозиция
    exposure_ms: float = 1000.0
    accumulations: int = 1
    accumulate_mode: str = "average"          # average | sum
    readout_speed: int = 0                    # 0 — как в SpectraSP / текущая в камере
    acq_mode: str = ACQ_SPECTRUM
    rows_top: int = -1                        # -1 — взять полосу из SpectraSP
    rows_bottom: int = -1
    # дополнительные полосы строк (2…8) [[верх, низ], …] — свой спектр с каждой (как «Stripe» SpectraSP)
    extra_stripes: List[List[float]] = field(default_factory=list)
    hardware_binning: bool = True
    flip_x: bool = False
    comment: str = ""                         # комментарий к снимкам (образец, условия) — в файлы и в буфер
    frame_timeout_s: float = 30.0             # запас сверх ожидаемого времени кадра

    # охлаждение (уставка не хранится — см. COOLING_TARGET_DEFAULT_C)
    warm_up_on_close: bool = True

    # выходы синхронизации камеры (SynchroOut1/2 в SpectraSP; к ним подключают лазер)
    sync_out1: int = -1                       # режим выхода 1 (−1 — взять из настроек SpectraSP)
    sync_out2: int = -1
    laser_output: int = 2                     # к какому выходу подключён лазер: 0 — ни к какому, 1, 2
    laser_active_high: bool = True            # лазер включается логической 1 (иначе — логическим 0)

    # обработка
    subtract_background: bool = True
    close_shutter_for_background: bool = True
    buffer_auto: bool = False                 # класть каждый снимок в буфер спектров
    buffer_offset: float = 0.0                # «лесенка»: сдвиг по Y между спектрами буфера (0 — наложение)
    shutter_ttl: bool = False                 # шторку открывает сигнал экспозиции камеры (режим TTL DevCtrl)
    remove_cosmics: bool = False
    laser_nm: float = 532.0
    # дефектные пиксели: номера столбцов ПЗС (как их отдаёт камера, до разворота), заменяются соседями
    defect_correction: bool = True
    defect_pixels: List[int] = field(default_factory=list)
    # коррекция интенсивности (спектральной чувствительности): y × K(x), K — по точкам таблицы
    intensity_corr: bool = False
    intensity_corr_by: str = "wl"             # wl — K от длины волны, нм; px — от номера пикселя
    intensity_corr_points: List[List[float]] = field(default_factory=list)    # [[x, K], …]
    # полосы интегрирования [{"lo":…, "hi":…, "unit": "nm"|"px", "baseline": bool}, …]
    bands: List[dict] = field(default_factory=list)

    # отображение спектра: единицы и диапазон по осям
    x_unit: str = X_NM                        # основная шкала (снизу)
    x_top_unit: str = ""                      # дополнительная шкала сверху ("" — нет)
    x_auto: bool = True                       # по X показывать весь спектр
    x_range: List[float] = field(default_factory=list)   # [от, до] в единицах x_range_unit
    x_range_unit: str = X_NM
    y_auto: bool = True                       # по Y — по данным в видимой области
    y_range: List[float] = field(default_factory=list)

    # монохроматор
    wl_presets: List[float] = field(default_factory=list)
    mono_timeout_s: float = 300.0

    # файлы
    save_dir: str = ""
    save_format: str = ".txt"
    autosave_series: bool = True

    calibration: CalibrationSettings = field(default_factory=CalibrationSettings)
    calibration_imported: bool = False
    window_state: str = ""

    # -------------------------------------------------------------- файлы
    @staticmethod
    def default_path() -> str:
        return os.path.join(settings_dir(), "settings.json")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppSettings":
        path = path or cls.default_path()
        s = cls()
        s._path = path          # не поле dataclass: в файл не пишется
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return s
        except (OSError, ValueError) as e:
            log.warning("Настройки %s повреждены (%s), используются значения по умолчанию", path, e)
            return s
        names = {f.name: f for f in fields(cls)}
        for k, v in data.items():
            if k == "calibration" and isinstance(v, dict):
                s.calibration = CalibrationSettings.from_dict(v)
            elif k in names:
                try:
                    default = getattr(s, k)
                    if isinstance(default, bool):
                        v = bool(v)
                    elif isinstance(default, int):
                        v = int(v)
                    elif isinstance(default, float):
                        v = float(v)
                    elif isinstance(default, list):
                        v = _list_value(k, v)
                    setattr(s, k, v)
                except (TypeError, ValueError):
                    log.warning("Пропущено неверное значение настройки %s=%r", k, v)
        return s

    def save(self, path: Optional[str] = None) -> None:
        path = path or getattr(self, "_path", None) or self.default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = asdict(self)
        data["calibration"] = self.calibration.to_dict()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
