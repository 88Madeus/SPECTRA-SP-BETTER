"""Общие типы данных и интерфейсы устройств.

Реальные драйверы (psi_camera, sspl_mono) и имитаторы (simulated) реализуют
одинаковые интерфейсы, поэтому остальная программа не знает, с чем работает.
"""
from __future__ import annotations

import abc
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger(__name__)


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


# До этой температуры ПЗС плавно отогревается перед выключением охлаждения и отключением
# (как в SpectraSP): резкий переход от глубокого холода грозит конденсатом и термоударом.
WARM_UP_TARGET_C = 2


def warm_up_camera(get_state: Callable[[], CameraState], set_cooling: Callable[[bool, int], None],
                   target_c: int = WARM_UP_TARGET_C, timeout_s: float = 180.0,
                   say: Optional[Callable[[str], None]] = None, cancel: Optional[threading.Event] = None,
                   poll_s: float = 2.0) -> bool:
    """Отогреть ПЗС: уставка target_c и ожидание, пока датчик не покажет не ниже target_c − 1 °C.

    Ждём именно температуру, а не признак «стабилизировано»: сразу после смены
    уставки камера ещё сообщает о стабилизации на старой. Возвращает True, если
    отогрев завершён или не требовался; False — прерван (cancel) или истёк срок.
    Уставка target_c при этом остаётся, камера продолжит отогреваться сама.
    """
    st = get_state()
    if st.temperature is None or st.temperature >= 0:
        return True
    log.info("Отогрев ПЗС: %+.0f °C → %+d °C", st.temperature, target_c)
    set_cooling(True, target_c)
    t0 = time.monotonic()
    shown = None
    while time.monotonic() - t0 < timeout_s:
        if cancel is not None:
            if cancel.wait(poll_s):
                log.warning("Отогрев ПЗС прерван пользователем (уставка %+d °C оставлена)", target_c)
                return False
        else:
            time.sleep(poll_s)
        st = get_state()
        if st.temperature is None:
            continue
        if say and round(st.temperature) != shown:
            shown = round(st.temperature)
            say(f"Отогрев ПЗС: {st.temperature:+.0f} °C → {target_c:+d} °C")
        if st.temperature >= target_c - 1:
            log.info("ПЗС отогрета до %+.0f °C", st.temperature)
            return True
    log.warning("ПЗС не отогрелась до %+d °C за %.0f с (уставка оставлена)", target_c, timeout_s)
    return False


@dataclass(frozen=True)
class AcqSettings:
    """Параметры экспозиции, которые реально уходят в камеру."""
    exposure_us: int
    readout_speed: int                  # кГц, из списка CameraInfo.readout_speeds
    regions: Tuple[Region, ...]
    sync_out: Tuple[int, int] = (-1, -1)   # режимы выходов синхронизации 1 и 2 (−1 — не менять)

    @property
    def out_shape(self) -> Tuple[int, int]:
        r = self.regions[0]
        return r.out_rows, r.out_cols


# Режимы выходов синхронизации камеры PSI («Выход синхр. 1/2» в SpectraSP). Номер режима —
# значение поля wSynchOutNType параметров камеры; список и порядок — как у SpectraSP для
# PSI_CCD_VER3 (camera info сообщает 9 режимов, девятый SpectraSP не показывает).
SYNC_OUT_MODES = {
    0: "Экспозиция, импульс «−»",
    1: "Экспозиция, импульс «+»",
    2: "Перед экспозицией, импульс «−»",
    3: "После экспозиции, импульс «−»",
    4: "Внешняя синхронизация 0",
    5: "Логическая 1 (постоянно)",
    6: "Логический 0 (постоянно)",
    7: "Программный старт",
}
SYNC_OUT_SPECTRASP = {0: "EXP polar negative", 1: "EXP polar positive", 2: "PRED DELAY polar negative",
                      3: "POST DELAY polar negative", 4: "EXTERNAL SYNCH 0", 5: "LOGICAL 1", 6: "LOGICAL 0",
                      7: "SOFT START SCAN"}
SYNC_LOGICAL_1, SYNC_LOGICAL_0 = 5, 6
SYNC_EXPOSURE_MODES = (0, 1)      # выход выдаёт импульс на время экспозиции (им можно открывать шторку)


class Camera(abc.ABC):
    """Интерфейс камеры. Все методы, кроме abort(), вызываются из одного потока."""

    info: CameraInfo

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    @abc.abstractmethod
    def open(self) -> CameraInfo: ...

    @abc.abstractmethod
    def close(self, warm_up: bool = False) -> None:
        """Отключиться; warm_up=True — сначала отогреть ПЗС (warm_up_camera)."""

    @abc.abstractmethod
    def get_state(self) -> CameraState: ...

    @abc.abstractmethod
    def set_cooling(self, enabled: bool, target_c: int) -> None: ...

    @abc.abstractmethod
    def configure(self, settings: AcqSettings) -> None:
        """Передать параметры в камеру (только если они изменились)."""

    @property
    def sync_out(self) -> Tuple[int, int]:
        """Текущие режимы выходов синхронизации 1 и 2 (−1 — неизвестно)."""
        return (-1, -1)

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
    shutter_ttl: bool = False      # шторку можно перевести на внешний TTL (сигнал экспозиции камеры)
    wl_range: Tuple[float, float] = (0.0, 0.0)
    # предельные положения шаговых двигателей (MaxMayStepCount из MSxxxx.cfg): (механизм, номер) -> шаги
    step_limits: Dict[Tuple[str, int], int] = field(default_factory=dict)
    # наибольшая допустимая ширина каждой щели, мкм (0 — неизвестна)
    slit_max_um: List[float] = field(default_factory=list)


# Механизмы монохроматора для сервисных операций (режим «Тест» окна DevCtrl)
MECH_GRATING = "grating"     # привод решётки (длина волны)
MECH_TURRET = "turret"       # турель (смена решётки)
MECH_SLIT = "slit"
MECH_MIRROR = "mirror"
MECH_TITLES = {MECH_GRATING: "Решётка (длина волны)", MECH_TURRET: "Турель решёток",
               MECH_SLIT: "Щель", MECH_MIRROR: "Выходное зеркало"}


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

    def set_shutter_ttl(self, on: bool, index: int = 0) -> None:
        """Шторка по экспозиции: True — её открывает внешний TTL-сигнал (выход синхронизации
        камеры), False — ручное управление (кнопки «Internal»/«TTL» штатного окна DevCtrl)."""
        raise DeviceError("Управление шторкой по TTL не поддерживается")

    @abc.abstractmethod
    def pixel_wavelengths(self, center_wl: float, center_pixel: float,
                          pixel_width_um: float, n_pixels: int) -> Optional[np.ndarray]:
        """Длины волн пикселей по модели прибора (как «Автоматическая» калибровка SpectraSP).

        Возвращает None, если модель недоступна.
        """

    # --- сервисные операции (режим «Тест» штатного окна DevCtrl) --------------
    def get_steps(self, mech: str, index: int = 0) -> int:
        """Текущее положение шагового двигателя механизма, шаги."""
        raise DeviceError("Операция не поддерживается")

    def set_steps(self, mech: str, index: int, position: int, timeout_s: float = 300.0) -> None:
        """Переместить механизм в абсолютное положение (шаги) и дождаться окончания."""
        raise DeviceError("Операция не поддерживается")

    def move_steps(self, mech: str, index: int, delta: int, timeout_s: float = 300.0) -> None:
        """Сдвинуть механизм на delta шагов (решётка и щели)."""
        raise DeviceError("Операция не поддерживается")

    def reset(self, mech: str, index: int = 0, timeout_s: float = 300.0) -> None:
        """Сброс механизма: поиск нулевого положения по датчику."""
        raise DeviceError("Операция не поддерживается")

    def show_native_window(self) -> None:
        """Открыть штатное окно драйвера DevCtrl (как скрытая кнопка SpectraSP)."""
        raise DeviceError("Штатное окно недоступно")


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


MIN_EXPOSURE_US = 10        # камера сообщает dwMinExposure = 10 (мкс); SpectraSP при подключении ставит 10 мкс


def camera_exposure_us(requested_us: float, min_us: int = MIN_EXPOSURE_US) -> int:
    """Экспозиция, которую камера получит на самом деле: не меньше минимальной и с точностью
    формата PSI (до 65,5 мс — 1 мкс, до 65,5 с — 1 мс, дальше — 1 с)."""
    us = max(float(min_us or MIN_EXPOSURE_US), float(requested_us))
    return int(round(unit_value_to_us(*exposure_to_unit_value(us))))


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
