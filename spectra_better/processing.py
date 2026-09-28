"""Обработка данных: объединение строк, накопление, фон, космические частицы, пики."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

SATURATION = 65535
_trapz = getattr(np, "trapezoid", None) or getattr(np, "trapz")


def bin_rows(image: np.ndarray, top: int = 0, bottom: Optional[int] = None) -> np.ndarray:
    """Программное суммирование строк top..bottom (включительно) → спектр float64."""
    img = np.asarray(image)
    if img.ndim == 1:
        return img.astype(np.float64)
    bottom = img.shape[0] - 1 if bottom is None else bottom
    top = max(0, min(top, img.shape[0] - 1))
    bottom = max(top, min(bottom, img.shape[0] - 1))
    return img[top:bottom + 1].astype(np.float64).sum(axis=0)


def to_spectrum(frame: np.ndarray) -> np.ndarray:
    """Кадр от камеры → спектр. При аппаратном биннинге кадр уже из одной строки."""
    frame = np.asarray(frame)
    if frame.ndim == 2 and frame.shape[0] == 1:
        return frame[0].astype(np.float64)
    return bin_rows(frame)


def saturated(frame: np.ndarray) -> bool:
    return bool(np.any(np.asarray(frame) >= SATURATION))


def remove_spikes(spectrum: np.ndarray, threshold: float = 10.0) -> np.ndarray:
    """Убрать одиночные выбросы (космические частицы) в одиночном спектре.

    Точка — выброс, если она выше медианы окна из 7 точек на threshold·σ
    (σ — шум, оценённый по разностям соседних точек) и при этом резко выше
    обоих соседей. Спектральные линии шире одного пикселя не затрагиваются.
    Для надёжного подавления используйте накопление ≥3 кадров.
    """
    y = np.asarray(spectrum, dtype=np.float64).copy()
    n = y.size
    if n < 7:
        return y
    pad = np.pad(y, 3, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(pad, 7), axis=1)
    d = np.diff(y)
    sigma = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)
    if sigma <= 0:
        sigma = 1.0
    excess = y - med
    for i in np.flatnonzero(excess > threshold * sigma):
        left = y[i - 1] if i > 0 else med[i]
        right = y[i + 1] if i < n - 1 else med[i]
        if y[i] - max(left, right) > 0.7 * excess[i]:
            y[i] = med[i]
    return y


def combine_frames(spectra: Sequence[np.ndarray], mode: str = "average", reject_cosmics: bool = True) -> np.ndarray:
    """Накопление серии: 'sum' или 'average'. При ≥3 кадрах выбросы
    отбрасываются сравнением с медианой по кадрам."""
    stack = np.asarray([np.asarray(s, dtype=np.float64) for s in spectra])
    if stack.ndim != 2 or stack.shape[0] == 0:
        raise ValueError("Нет кадров для накопления")
    n = stack.shape[0]
    if reject_cosmics and n >= 3:
        med = np.median(stack, axis=0)
        noise = np.sqrt(np.clip(med, 1.0, None)) + 1.4826 * np.median(np.abs(stack - med), axis=0)
        mask = (stack - med) > 6.0 * noise
        if np.any(mask):
            stack = np.where(mask, med, stack)
    return stack.sum(axis=0) if mode == "sum" else stack.mean(axis=0)


@dataclass
class Peak:
    index: float        # дробный номер пикселя (центр)
    x: float            # положение в единицах оси
    height: float
    fwhm: float         # в единицах оси
    area: float


def find_peaks(x: np.ndarray, y: np.ndarray, min_prominence: Optional[float] = None,
               max_peaks: int = 30, min_distance: int = 3) -> List[Peak]:
    """Простой поиск пиков с уточнением центра параболой и оценкой ширины."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    if n < 5:
        return []
    base = np.median(y)
    noise = 1.4826 * np.median(np.abs(np.diff(y))) / np.sqrt(2) + 1e-9
    if min_prominence is None:
        min_prominence = max(8.0 * noise, 0.02 * (y.max() - base))
    cand = np.flatnonzero((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:])) + 1
    cand = [i for i in cand if y[i] - base >= min_prominence]
    cand.sort(key=lambda i: -y[i])
    chosen: List[int] = []
    for i in cand:
        if all(abs(i - j) >= min_distance for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_peaks:
            break
    peaks: List[Peak] = []
    idx = np.arange(n, dtype=np.float64)
    for i in sorted(chosen):
        y0, y1, y2 = y[i - 1], y[i], y[i + 1]
        denom = y0 - 2 * y1 + y2
        shift = 0.5 * (y0 - y2) / denom if denom != 0 else 0.0
        shift = float(np.clip(shift, -0.5, 0.5))
        ci = i + shift
        height = y1 - base
        half = base + height / 2
        left = i
        while left > 0 and y[left] > half:
            left -= 1
        right = i
        while right < n - 1 and y[right] > half:
            right += 1
        # линейная интерполяция краёв на полувысоте
        def edge(a, b):
            if y[b] == y[a]:
                return float(a)
            return a + (half - y[a]) / (y[b] - y[a]) * (b - a)
        li = edge(left, left + 1) if left < i else float(left)
        ri = edge(right - 1, right) if right > i else float(right)
        xc = float(np.interp(ci, idx, x))
        fwhm = abs(float(np.interp(ri, idx, x) - np.interp(li, idx, x)))
        lo, hi = max(0, left), min(n, right + 1)
        area = float(_trapz(y[lo:hi] - base, x[lo:hi])) if hi - lo > 1 else 0.0
        peaks.append(Peak(index=ci, x=xc, height=height, fwhm=fwhm, area=abs(area)))
    return peaks


def stitch(segments: Sequence[tuple], step: Optional[float] = None) -> tuple:
    """Сшить спектры (x, y) с перекрытием в панораму с плавным переходом.

    Каждый сегмент — (x, y) с монотонной осью x. Результат на общей сетке.
    """
    segs = []
    for x, y in segments:
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if x[0] > x[-1]:
            x, y = x[::-1], y[::-1]
        segs.append((x, y))
    if not segs:
        raise ValueError("Нет сегментов")
    if step is None:
        step = min(np.median(np.diff(x)) for x, _ in segs)
    lo = min(x[0] for x, _ in segs)
    hi = max(x[-1] for x, _ in segs)
    grid = np.arange(lo, hi + step / 2, step)
    acc = np.zeros_like(grid)
    wsum = np.zeros_like(grid)
    for x, y in segs:
        inside = (grid >= x[0]) & (grid <= x[-1])
        g = grid[inside]
        # вес — «шапка»: к краям сегмента уменьшается (там хуже фокусировка и виньетирование)
        rel = (g - x[0]) / max(x[-1] - x[0], 1e-12)
        w = np.clip(np.minimum(rel, 1 - rel) * 10.0, 1e-3, 1.0)
        acc[inside] += w * np.interp(g, x, y)
        wsum[inside] += w
    ok = wsum > 0
    return grid[ok], acc[ok] / wsum[ok]
