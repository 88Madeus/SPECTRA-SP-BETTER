"""Сохранение и загрузка спектров.

* .txt / .dat — табуляция, десятичная точка (Origin, MATLAB, Python);
* .csv — точка с запятой и десятичная запятая (русский Excel открывает двойным щелчком).
Метаданные пишутся в начало файла строками «# ключ: значение».
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import __version__


def _fmt(v: float, comma: bool) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".") if abs(v) < 1e7 else f"{v:.6e}"
    if s in ("", "-"):
        s = "0"
    return s.replace(".", ",") if comma else s


def save_spectrum(path: str, x: np.ndarray, y: np.ndarray, meta: Optional[Dict[str, object]] = None,
                  x_name: str = "wavelength_nm", y_name: str = "intensity",
                  extra: Optional[Dict[str, np.ndarray]] = None) -> None:
    ext = os.path.splitext(path)[1].lower()
    comma = ext == ".csv"
    sep = ";" if comma else "\t"
    lines = [f"# SpectraSP Better {__version__}",
             f"# date: {_dt.datetime.now().isoformat(timespec='seconds')}"]
    for k, v in (meta or {}).items():
        lines.append(f"# {k}: {v}")
    cols = [x_name, y_name] + list((extra or {}).keys())
    lines.append(sep.join(cols))
    data = [np.asarray(x, dtype=float), np.asarray(y, dtype=float)] + \
        [np.asarray(v, dtype=float) for v in (extra or {}).values()]
    n = min(len(d) for d in data)
    for i in range(n):
        lines.append(sep.join(_fmt(d[i], comma) for d in data))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig" if comma else "utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _detect_separator(lines: List[str]) -> Tuple[Optional[str], bool]:
    """(разделитель столбцов или None для пробелов, допускается ли десятичная запятая)."""
    sample = [l for l in lines if l and not l.startswith("#")][:20]
    if any(";" in l for l in sample):
        return ";", True
    if any("\t" in l for l in sample):
        return "\t", True
    comma_lines = [l for l in sample if "," in l]
    if comma_lines and all(len(l.split(",")) == len(comma_lines[0].split(",")) for l in comma_lines) \
            and len(comma_lines[0].split(",")) >= 2 and not any(" " in l.strip() for l in comma_lines):
        return ",", False
    return None, True


def load_spectrum(path: str) -> Tuple[np.ndarray, np.ndarray, Dict[str, str], List[str]]:
    """Загрузить 2+ столбца чисел. Понимает разделители tab ; , пробел и десятичную запятую."""
    with open(path, "rb") as f:
        raw = f.read()
    text = None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("latin1")
    lines = [l.strip() for l in io.StringIO(text)]
    sep, decimal_comma = _detect_separator(lines)
    meta: Dict[str, str] = {}
    header: List[str] = []
    rows: List[List[float]] = []
    for s in lines:
        if not s:
            continue
        if s.startswith("#"):
            m = re.match(r"#\s*([^:]+):\s*(.*)", s)
            if m:
                meta[m.group(1).strip()] = m.group(2).strip()
            continue
        parts = s.split(sep) if sep else s.split()
        if decimal_comma:
            parts = [p.replace(",", ".") for p in parts]
        try:
            vals = [float(p) for p in parts if p.strip() != ""]
        except ValueError:
            if not rows:
                header = [p.strip() for p in parts]
            continue
        if len(vals) >= 2:
            rows.append(vals[:2])
        elif len(vals) == 1:
            rows.append([float(len(rows)), vals[0]])
    if not rows:
        raise ValueError(f"В файле {os.path.basename(path)} не найдено числовых данных")
    arr = np.asarray(rows, dtype=float)
    return arr[:, 0], arr[:, 1], meta, header


def save_image(path: str, image: np.ndarray, meta: Optional[Dict[str, object]] = None) -> None:
    """Кадр ПЗС: .npy (без потерь, быстро) или .csv/.txt (матрица чисел)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".npy":
        np.save(path, np.asarray(image))
        if meta:
            with open(os.path.splitext(path)[0] + "_info.txt", "w", encoding="utf-8") as f:
                for k, v in meta.items():
                    f.write(f"{k}: {v}\n")
        return
    sep = ";" if ext == ".csv" else "\t"
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        for k, v in (meta or {}).items():
            f.write(f"# {k}: {v}\n")
        for row in np.asarray(image):
            f.write(sep.join(str(int(v)) for v in row) + "\n")


def unique_path(directory: str, stem: str, ext: str) -> str:
    """Имя файла без перезаписи: stem_001.ext, stem_002.ext, ..."""
    os.makedirs(directory, exist_ok=True)
    for i in range(1, 100000):
        p = os.path.join(directory, f"{stem}_{i:03d}{ext}")
        if not os.path.exists(p):
            return p
    raise OSError("Не удалось подобрать имя файла")
