"""Шкала длин волн.

Источники шкалы (по убыванию приоритета):
1. «Модель прибора» — функция GetCalibrationByTurret из SSPL_IM/DevCtrl, та же,
   что использует SpectraSP. Параметры (центральный пиксель, ширина пикселя)
   по умолчанию берутся из настроек SpectraSP, чтобы шкала совпадала со старой.
2. «Геометрическая модель» — уравнение решётки для схемы Черни–Тёрнера по
   параметрам из MSxxxx.cfg (штрихи/мм, фокус, угол). Используется, если
   модель прибора недоступна, и в режиме имитации.
3. Линейная шкала (нм/пиксель) или просто номера пикселей.

Поверх любой модели можно наложить поправку по реперным линиям
(полином степени 0…2 от номера пикселя), отдельно для каждой решётки.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

MODE_DEVICE = "device"
MODE_MODEL = "model"
MODE_LINEAR = "linear"
MODE_PIXELS = "pixels"
MODES = {
    MODE_DEVICE: "Модель прибора (как в SpectraSP)",
    MODE_MODEL: "Геометрическая модель (уравнение решётки)",
    MODE_LINEAR: "Линейная (нм/пиксель)",
    MODE_PIXELS: "Номера пикселей",
}


@dataclass
class GratingGeometry:
    lines_per_mm: float
    focus_mm: float
    half_deviation_rad: float
    tilt_rad: float = 0.0
    order: int = 1

    def wavelengths(self, center_wl: float, center_pixel: float, pixel_um: float,
                    n_pixels: int, direction: int = 1) -> np.ndarray:
        """Длины волн пикселей 0..n-1 для центральной длины волны center_wl (нм)."""
        d = 1e6 / self.lines_per_mm                        # период, нм
        k = self.order
        s = k * center_wl / (2.0 * d * math.cos(self.half_deviation_rad))
        if not -1.0 < s < 1.0:
            raise ValueError("Длина волны недостижима для этой решётки")
        psi = math.asin(s)
        alpha = psi + self.half_deviation_rad
        beta_c = psi - self.half_deviation_rad
        x = (np.arange(n_pixels, dtype=float) - center_pixel) * pixel_um * 1e-3 * direction   # мм
        dbeta = np.arctan2(x * math.cos(self.tilt_rad), self.focus_mm + x * math.sin(self.tilt_rad))
        return d / k * (math.sin(alpha) + np.sin(beta_c + dbeta))


@dataclass
class Correction:
    """Поправка λ_истинная − λ_модели = полином(пиксель)."""
    coeffs: List[float] = field(default_factory=list)     # numpy.polyfit, старшая степень первой
    points: List[Tuple[float, float]] = field(default_factory=list)   # (пиксель, λ эталона)

    def apply(self, pixels: np.ndarray, wl: np.ndarray) -> np.ndarray:
        if not self.coeffs:
            return wl
        return wl + np.polyval(self.coeffs, pixels)

    def to_dict(self) -> dict:
        return {"coeffs": list(self.coeffs), "points": [list(p) for p in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "Correction":
        return cls(coeffs=[float(c) for c in d.get("coeffs", [])],
                   points=[(float(a), float(b)) for a, b in d.get("points", [])])


def fit_correction(pixels: Sequence[float], true_wl: Sequence[float], model_wl: np.ndarray,
                   max_degree: int = 2) -> Tuple[Correction, float]:
    """Подобрать поправку по реперам. Возвращает (поправка, СКО остатков, нм)."""
    px = np.asarray(pixels, dtype=float)
    tw = np.asarray(true_wl, dtype=float)
    if px.size == 0:
        return Correction(), 0.0
    idx = np.arange(model_wl.size, dtype=float)
    mw = np.interp(px, idx, model_wl)
    # степень выбираем так, чтобы оставалась проверка остатками: 1 точка — сдвиг,
    # 2–3 точки — линейная поправка, от 4 точек — квадратичная
    deg = int(min(max_degree, 0 if px.size == 1 else (1 if px.size <= 3 else 2)))
    coeffs = np.polyfit(px, tw - mw, deg) if deg >= 0 else np.array([])
    resid = tw - (mw + np.polyval(coeffs, px))
    rms = float(np.sqrt(np.mean(resid ** 2))) if px.size > deg + 1 else 0.0
    return Correction(coeffs=[float(c) for c in coeffs], points=list(zip(px.tolist(), tw.tolist()))), rms


@dataclass
class CalibrationSettings:
    mode: str = MODE_DEVICE
    center_pixel: float = 1024.0
    device_pixel_width: float = 12.0     # параметр «ширина пикселя» для модели прибора (как в SpectraSP)
    pixel_um: float = 12.0               # физический шаг пикселя для геометрической модели
    nm_per_pixel: float = 0.01           # для линейной шкалы
    corrections: Dict[int, Correction] = field(default_factory=dict)   # по номеру решётки

    def to_dict(self) -> dict:
        return {"mode": self.mode, "center_pixel": self.center_pixel,
                "device_pixel_width": self.device_pixel_width, "pixel_um": self.pixel_um,
                "nm_per_pixel": self.nm_per_pixel,
                "corrections": {str(k): v.to_dict() for k, v in self.corrections.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "CalibrationSettings":
        c = cls()
        for key in ("mode", "center_pixel", "device_pixel_width", "pixel_um", "nm_per_pixel"):
            if key in d:
                setattr(c, key, type(getattr(c, key))(d[key]))
        c.corrections = {int(k): Correction.from_dict(v) for k, v in d.get("corrections", {}).items()}
        return c


class WavelengthAxis:
    """Вычисляет шкалу для текущего состояния монохроматора с кэшированием."""

    def __init__(self, settings: CalibrationSettings, geometries: Dict[int, GratingGeometry]):
        self.settings = settings
        self.geometries = geometries
        self._cache_key = None
        self._cache: Optional[np.ndarray] = None
        self.source = ""      # чем реально посчитана шкала (для строки состояния)

    def invalidate(self) -> None:
        self._cache_key = None

    def axis(self, n_pixels: int, center_wl: Optional[float], grating: Optional[int],
             device_model=None) -> Tuple[np.ndarray, str]:
        """Вернуть (x, единицы). device_model(center_wl, center_px, px_width, n) -> ndarray|None."""
        s = self.settings
        key = (n_pixels, round(center_wl or 0.0, 6), grating, s.mode, s.center_pixel, s.device_pixel_width,
               s.pixel_um, s.nm_per_pixel,
               tuple(s.corrections.get(grating, Correction()).coeffs) if grating is not None else ())
        if key == self._cache_key and self._cache is not None:
            return self._cache, ("px" if s.mode == MODE_PIXELS or center_wl is None else "nm")
        pixels = np.arange(n_pixels, dtype=float)
        wl = None
        if s.mode != MODE_PIXELS and center_wl:
            if s.mode == MODE_DEVICE and device_model is not None:
                try:
                    wl = device_model(center_wl, s.center_pixel, s.device_pixel_width, n_pixels)
                    self.source = "модель прибора" if wl is not None else ""
                except Exception:  # noqa: BLE001 — переходим к запасной модели
                    wl = None
            if wl is None and s.mode in (MODE_DEVICE, MODE_MODEL) and grating in self.geometries:
                try:
                    wl = self.geometries[grating].wavelengths(center_wl, s.center_pixel, s.pixel_um, n_pixels)
                    self.source = "геометрическая модель"
                except ValueError:
                    wl = None
            if wl is None and s.mode != MODE_PIXELS:
                wl = center_wl + (pixels - s.center_pixel) * s.nm_per_pixel
                self.source = "линейная шкала"
            if grating is not None and grating in s.corrections:
                wl = s.corrections[grating].apply(pixels, wl)
                self.source += " + поправка по реперам"
        if wl is None:
            x, unit = pixels, "px"
            self.source = "пиксели"
        else:
            x, unit = np.asarray(wl, dtype=float), "nm"
        self._cache_key, self._cache = key, x
        return x, unit


def raman_shift(wl_nm: np.ndarray, laser_nm: float) -> np.ndarray:
    """Комбинационный сдвиг, см⁻¹."""
    return 1e7 / laser_nm - 1e7 / np.asarray(wl_nm, dtype=float)


def wavenumber(wl_nm: np.ndarray) -> np.ndarray:
    return 1e7 / np.asarray(wl_nm, dtype=float)


def geometries_from_config(mono_cfg) -> Dict[int, GratingGeometry]:
    out: Dict[int, GratingGeometry] = {}
    for g in getattr(mono_cfg, "gratings", []):
        if g.lines_per_mm > 0:
            out[g.index] = GratingGeometry(lines_per_mm=g.lines_per_mm, focus_mm=g.focus_m * 1000.0,
                                           half_deviation_rad=g.theta2, tilt_rad=g.fc_angle)
    return out
