"""Единицы оси X и пересчёт между ними.

Все спектральные единицы выражаются через длину волны: нм ↔ см⁻¹ ↔
рамановский сдвиг ↔ эВ пересчитываются аналитически, а пиксели ↔ нм — по
шкале конкретного спектра (массив длин волн по пикселям).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

X_NM, X_PX, X_WAVENUMBER, X_RAMAN, X_EV = "nm", "px", "cm-1", "raman", "ev"

X_UNITS = {
    X_NM: "Длина волны, нм",
    X_PX: "Пиксель",
    X_WAVENUMBER: "Волновое число, см⁻¹",
    X_RAMAN: "Рамановский сдвиг, см⁻¹",
    X_EV: "Энергия фотона, эВ",
}
X_SHORT = {X_NM: "нм", X_PX: "пикс.", X_WAVENUMBER: "см⁻¹", X_RAMAN: "см⁻¹ (сдвиг)", X_EV: "эВ"}
X_DECIMALS = {X_NM: 3, X_PX: 1, X_WAVENUMBER: 1, X_RAMAN: 1, X_EV: 4}

HC_EV_NM = 1239.841984          # h·c, эВ·нм


def is_spectral(unit: str) -> bool:
    """Единица выражается через длину волны (то есть не пиксели)."""
    return unit in (X_NM, X_WAVENUMBER, X_RAMAN, X_EV)


def axis_label(unit: str, laser_nm: float = 532.0) -> str:
    text = X_UNITS.get(unit, unit)
    if unit == X_RAMAN:
        text += f" (лазер {laser_nm:g} нм)"
    return text


def _inv(v: np.ndarray, k: float) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = k / v
    return np.where(np.isfinite(out), out, np.nan)


def from_nm(nm, unit: str, laser_nm: float = 532.0) -> np.ndarray:
    """Длина волны → спектральная единица."""
    nm = np.asarray(nm, dtype=float)
    if unit == X_NM:
        return nm.copy()
    if unit == X_WAVENUMBER:
        return _inv(nm, 1e7)
    if unit == X_RAMAN:
        return 1e7 / laser_nm - _inv(nm, 1e7)
    if unit == X_EV:
        return _inv(nm, HC_EV_NM)
    raise ValueError(f"{unit} не выражается через длину волны")


def to_nm(v, unit: str, laser_nm: float = 532.0) -> np.ndarray:
    """Спектральная единица → длина волны."""
    v = np.asarray(v, dtype=float)
    if unit == X_NM:
        return v.copy()
    if unit == X_WAVENUMBER:
        return _inv(v, 1e7)
    if unit == X_RAMAN:
        return _inv(1e7 / laser_nm - v, 1e7)
    if unit == X_EV:
        return _inv(v, HC_EV_NM)
    raise ValueError(f"{unit} не выражается через длину волны")


def _interp_extrap(x, xp: np.ndarray, fp: np.ndarray) -> np.ndarray:
    """np.interp с линейным продолжением за края (xp — по возрастанию)."""
    x = np.asarray(x, dtype=float)
    y = np.interp(x, xp, fp)
    if xp.size >= 2:
        lo, hi = x < xp[0], x > xp[-1]
        k0 = (fp[1] - fp[0]) / (xp[1] - xp[0])
        k1 = (fp[-1] - fp[-2]) / (xp[-1] - xp[-2])
        y = np.where(lo, fp[0] + (x - xp[0]) * k0, y)
        y = np.where(hi, fp[-1] + (x - xp[-1]) * k1, y)
    return y


class AxisMap:
    """Пересчёт положения по оси X между любыми единицами для одного спектра.

    wl_nm — длины волн пикселей (None, если шкалы длин волн нет: тогда
    доступны только пиксели).
    """

    def __init__(self, wl_nm: Optional[Sequence[float]] = None, laser_nm: float = 532.0):
        self.laser_nm = laser_nm
        self.wl = None
        if wl_nm is not None:
            wl = np.asarray(wl_nm, dtype=float)
            if wl.size >= 2 and np.all(np.isfinite(wl)) and np.all(np.diff(wl) != 0) and \
                    (np.all(np.diff(wl) > 0) or np.all(np.diff(wl) < 0)):
                self.wl = wl
        self.px = np.arange(self.wl.size, dtype=float) if self.wl is not None else None

    @property
    def calibrated(self) -> bool:
        return self.wl is not None

    def available(self, unit: str) -> bool:
        return unit == X_PX or self.calibrated

    def _px_to_nm(self, px) -> np.ndarray:
        return _interp_extrap(px, self.px, self.wl)

    def _nm_to_px(self, nm) -> np.ndarray:
        if self.wl[0] < self.wl[-1]:
            return _interp_extrap(nm, self.wl, self.px)
        return _interp_extrap(nm, self.wl[::-1], self.px[::-1])

    def convert(self, values, src: str, dst: str) -> np.ndarray:
        """Пересчитать значения из src в dst; ValueError, если нужной шкалы нет."""
        v = np.asarray(values, dtype=float)
        if src == dst:
            return v.copy()
        if is_spectral(src) and is_spectral(dst):
            return from_nm(to_nm(v, src, self.laser_nm), dst, self.laser_nm)
        if not self.calibrated:
            raise ValueError("Нет шкалы длин волн: пересчёт из пикселей невозможен")
        nm = self._px_to_nm(v) if src == X_PX else to_nm(v, src, self.laser_nm)
        return self._nm_to_px(nm) if dst == X_PX else from_nm(nm, dst, self.laser_nm)

    def can_convert(self, src: str, dst: str) -> bool:
        return src == dst or (is_spectral(src) and is_spectral(dst)) or self.calibrated


def convert_range(rng: Sequence[float], src: str, dst: str, amap: Optional[AxisMap] = None,
                  laser_nm: float = 532.0) -> Optional[tuple]:
    """Пересчитать диапазон [a, b] в другие единицы (результат по возрастанию) или None."""
    amap = amap or AxisMap(None, laser_nm)
    if not amap.can_convert(src, dst):
        return None
    a, b = amap.convert(np.asarray(rng, dtype=float), src, dst)
    if not (np.isfinite(a) and np.isfinite(b)) or a == b:
        return None
    return (float(min(a, b)), float(max(a, b)))
