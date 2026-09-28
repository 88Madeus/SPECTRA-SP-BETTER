"""Общие типы данных и интерфейсы устройств.

Реальные драйверы (psi_camera, sspl_mono) и имитаторы (simulated) реализуют
одинаковые интерфейсы, поэтому остальная программа не знает, с чем работает.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np


class DeviceError(RuntimeError):
    """Ошибка устройства или драйвера (с понятным пользователю текстом)."""


# ---------------------------------------------------------------- камера ---

@dataclass(frozen=True)
class Region:
    """Область считывания ПЗС (координаты включительно, как в драйвере PSI).

    bin_x/bin_y — аппаратное объединение пикселей (суперпиксель).
    Для спектра обычно bin_y = высота полосы: матрица отдаёт одну строку.
    """
    left: int
    top: int
    right: int
    bottom: int
    bin_x: int = 1
    bin_y: int = 1

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def out_cols(self) -> int:
        return self.width // self.bin_x

    @property
    def out_rows(self) -> int:
        return self.height // self.bin_y

    @property
    def out_pixels(self) -> int:
        return self.out_cols * self.out_rows

    def validate(self, sensor_w: int, sensor_h: int) -> None:
        if not (0 <= self.left <= self.right < sensor_w):
            raise ValueError(f"Неверные столбцы области: {self.left}..{self.right}")
        if not (0 <= self.top <= self.bottom < sensor_h):
            raise ValueError(f"Неверные строки области: {self.top}..{self.bottom}")
        if self.bin_x < 1 or self.bin_y < 1:
            raise ValueError("Биннинг должен быть >= 1")
        if self.width % self.bin_x or self.height % self.bin_y:
            raise ValueError("Размер области должен делиться на биннинг")


@dataclass
class CameraInfo:
    model: str = ""
    serial: str = ""
    width: int = 0
    height: int = 0
    pixel_width_um: float = 0.0
    pixel_height_um: float = 0.0
    readout_speeds: List[int] = field(default_factory=list)   # кГц
    temp_min: int = 0
    temp_max: int = 0
    cooling_type: int = 0
    max_bin_x: int = 1
    max_bin_y: int = 1
    min_exposure: int = 0
    max_exposure: int = 0
    firmware: str = ""
    dll_name: str = ""


@dataclass
class CameraState:
    temperature: Optional[float] = None       # °C, датчик ПЗС
    temperature2: Optional[float] = None
    cooling_state: int = 0                    # 0 выкл., 1 охлаждение, 2 температура достигнута
    fan_on: bool = False
    overheat: bool = False
    error_code: int = 0
    fpga_status: int = 0

    COOLING_TEXT = {0: "выключено", 1: "выход на режим", 2: "стабилизировано"}

    @property
    def cooling_text(self) -> str:
        return self.COOLING_TEXT.get(self.cooling_state, f"код {self.cooling_state}")


@dataclass(frozen=True)
class AcqSettings:
    """Параметры экспозиции, которые реально уходят в камеру."""
    exposure_us: int
    readout_speed: int                  # кГц, из списка CameraInfo.readout_speeds
    regions: Tuple[Region, ...]

    @property
    def out_shape(self) -> Tuple[int, int]:
        r = self.regions[0]
        return r.out_rows, r.out_cols


class Camera(abc.ABC):
    """Интерфейс камеры. Все методы, кроме abort(), вызываются из одного потока."""

    info: CameraInfo

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    @abc.abstractmethod
    def open(self) -> CameraInfo: ...

    @abc.abstractmethod
    def close(self, warm_up: bool = True) -> None: ...

    @abc.abstractmethod
    def get_state(self) -> CameraState: ...

    @abc.abstractmethod
    def set_cooling(self, enabled: bool, target_c: int) -> None: ...

    @abc.abstractmethod
    def configure(self, settings: AcqSettings) -> None:
        """Передать параметры в камеру (только если они изменились)."""

    @abc.abstractmethod
    def acquire(self) -> np.ndarray:
        """Одна экспозиция. Возвращает uint16-массив (строки, столбцы)."""

    @abc.abstractmethod
    def abort(self) -> None:
        """Прервать идущую экспозицию. Допускается вызов из другого потока."""


# ---------------------------------------------------------- монохроматор ---

@dataclass
class GratingInfo:
    index: int              # 0..N-1, как в SSPL_IM
    lines_per_mm: float
    blaze: str = ""

    @property
    def title(self) -> str:
        t = f"№{self.index + 1}: {self.lines_per_mm:g} штр/мм"
        return f"{t} ({self.blaze})" if self.blaze else t


@dataclass
class MonoInfo:
    model: str = ""
    serial: str = ""
    device_type: int = 0
    gratings: List[GratingInfo] = field(default_factory=list)
    slits: List[str] = field(default_factory=list)          # названия, индекс = номер щели SSPL
    mirror_positions: List[str] = field(default_factory=list)
    has_shutter: bool = True
    wl_range: Tuple[float, float] = (0.0, 0.0)


class Monochromator(abc.ABC):
    info: MonoInfo

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    @abc.abstractmethod
    def open(self) -> MonoInfo: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def is_busy(self) -> bool: ...

    @abc.abstractmethod
    def wait_idle(self, timeout_s: float) -> None: ...

    @abc.abstractmethod
    def get_wavelength(self) -> float: ...

    @abc.abstractmethod
    def set_wavelength(self, wl_nm: float, wait: bool = True, timeout_s: float = 300.0) -> None: ...

    @abc.abstractmethod
    def get_grating(self) -> int: ...

    @abc.abstractmethod
    def set_grating(self, index: int, wait: bool = True, timeout_s: float = 300.0) -> None: ...

    @abc.abstractmethod
    def get_slit(self, index: int) -> float: ...

    @abc.abstractmethod
    def set_slit(self, index: int, width_um: float) -> None: ...

    @abc.abstractmethod
    def get_mirror(self, index: int) -> int: ...

    @abc.abstractmethod
    def set_mirror(self, index: int, position: int) -> None: ...

    @abc.abstractmethod
    def get_shutter(self, index: int = 0) -> Optional[bool]: ...

    @abc.abstractmethod
    def set_shutter(self, is_open: bool, index: int = 0) -> None: ...

    @abc.abstractmethod
    def pixel_wavelengths(self, center_wl: float, center_pixel: float,
                          pixel_width_um: float, n_pixels: int) -> Optional[np.ndarray]:
        """Длины волн пикселей по модели прибора (как «Автоматическая» калибровка SpectraSP).

        Возвращает None, если модель недоступна.
        """


def exposure_to_unit_value(exposure_us: float) -> Tuple[int, int]:
    """Перевод экспозиции в пару (единица, значение) формата PSI.

    Повторяет логику SpectraSP: 1 — мкс, 2 — мс, 3 — с, 4 — мин;
    значение 16-битное (0..65535), выбирается самая мелкая подходящая единица.
    """
    value = float(exposure_us)
    for unit, divider in ((1, 1.0), (2, 1e3), (3, 1e3), (4, 60.0)):
        value /= divider
        if 0 <= value <= 65535:
            return unit, int(round(value))
    raise ValueError(f"Экспозиция вне допустимого диапазона: {exposure_us} мкс")


UNIT_TO_US = {1: 1.0, 2: 1e3, 3: 1e6, 4: 60e6}


def unit_value_to_us(unit: int, value: int) -> float:
    return UNIT_TO_US.get(unit, 1.0) * value


def spectrum_region(width: int, height: int, top: int, bottom: int,
                    hardware_binning: bool = True) -> Region:
    """Полоса строк top..bottom по всей ширине матрицы."""
    top = max(0, min(top, height - 1))
    bottom = max(top, min(bottom, height - 1))
    bin_y = bottom - top + 1 if hardware_binning else 1
    return Region(0, top, width - 1, bottom, 1, bin_y)


def full_frame_region(width: int, height: int) -> Region:
    return Region(0, 0, width - 1, height - 1, 1, 1)


def as_tuple(regions: Sequence[Region]) -> Tuple[Region, ...]:
    return tuple(regions)
