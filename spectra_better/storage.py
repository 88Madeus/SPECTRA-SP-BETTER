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
from struct import error as struct_error
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
    """Кадр ПЗС: .npy (без потерь, быстро), .csv/.txt (матрица чисел) или .tif/.png — пиксель в пиксель:
    16-битный серый (отсчёты как есть; TIFF — 32-битный, если сумма накоплений больше 65535). Цветное
    изображение (строки × столбцы × 3, «как на экране») — 8-битный цветной TIFF/PNG."""
    from . import imagefiles as F
    ext = os.path.splitext(path)[1].lower()
    a = np.asarray(image)
    if ext in (".tif", ".tiff"):
        F.write_tiff(path, a if a.ndim == 3 else (F.as_uint16(a) if F.fits_uint16(a) else a), meta)
        return
    if ext == ".png":
        if a.ndim == 2 and not F.fits_uint16(a):
            raise ValueError("Значения кадра больше 65535 (сумма накоплений) — в 16-битный PNG они не "
                             "помещаются. Сохраните в TIFF (32 бита) или NPY.")
        F.write_png(path, a if a.ndim == 3 else F.as_uint16(a), meta)
        return
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


def _qt_gray16(path: str) -> np.ndarray:
    """Серое изображение через Qt (сжатые TIFF, PNG); цветное — (строки × столбцы × 3)."""
    from PyQt5 import QtGui
    q = QtGui.QImage(path)
    if q.isNull():
        raise ValueError("Не удалось прочитать изображение")
    F = QtGui.QImage
    gray = q.format() in (F.Format_Grayscale8, F.Format_Grayscale16) or \
        (q.format() in (F.Format_Indexed8, F.Format_Mono, F.Format_MonoLSB) and q.isGrayscale())
    if not gray:                                   # по формату файла, а не по содержимому
        q = q.convertToFormat(QtGui.QImage.Format_RGB888)
        ptr = q.constBits()
        ptr.setsize(q.bytesPerLine() * q.height())
        rows = np.frombuffer(ptr, dtype=np.uint8).reshape(q.height(), q.bytesPerLine())
        return rows[:, :q.width() * 3].reshape(q.height(), q.width(), 3).copy()
    if q.format() != QtGui.QImage.Format_Grayscale16:
        q = q.convertToFormat(QtGui.QImage.Format_Grayscale16)
    ptr = q.constBits()
    ptr.setsize(q.bytesPerLine() * q.height())
    rows = np.frombuffer(ptr, dtype=np.uint16).reshape(q.height(), q.bytesPerLine() // 2)
    return rows[:, :q.width()].copy()


def load_image(path: str) -> Tuple[np.ndarray, Dict[str, str]]:
    """Кадр ПЗС из файла: .npy (+ _info.txt), .txt/.csv (матрица, «# ключ: значение» в начале),
    TIFF (16-битный, как сохраняет SpectraSP, или свой 32-битный) или 16-битный PNG.
    Возвращает (кадр float64, сведения; «first_row» — с какой строки ПЗС начинается кадр)."""
    ext = os.path.splitext(path)[1].lower()
    meta: Dict[str, str] = {}
    if ext == ".npy":
        img = np.load(path, allow_pickle=False)
        info = os.path.splitext(path)[0] + "_info.txt"
        if os.path.isfile(info):
            with open(info, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
    elif ext in (".tif", ".tiff", ".png"):
        from . import imagefiles as F
        img = None
        if ext != ".png":
            try:
                img, meta = F.read_tiff(path)          # свои файлы и любые несжатые (в т. ч. 32-битные)
            except (ValueError, KeyError, IndexError, struct_error):
                img = None
        if img is None:
            img = _qt_gray16(path)                      # сжатые TIFF, PNG — через Qt
            if ext == ".png":
                meta = F.read_png_text(path)
        if img.ndim == 3:
            raise ValueError("Это цветная картинка («как на экране»), а не отсчёты камеры — для обработки "
                             "откройте кадр, сохранённый как данные (TIFF/PNG 16 бит, NPY, TXT)")
    else:
        data = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if ":" in line:
                        k, v = line[1:].split(":", 1)
                        meta[k.strip()] = v.strip()
                    continue
                sep = ";" if ";" in line else None
                data.append([float(v.replace(",", ".")) for v in line.split(sep)])
        if not data or len({len(r) for r in data}) != 1:
            raise ValueError("Файл не похож на кадр: строки разной длины или нет данных")
        img = np.asarray(data)
    img = np.asarray(img, dtype=np.float64)
    if img.ndim != 2 or min(img.shape) < 1:
        raise ValueError("В файле не двумерный кадр")
    return img, meta


def unique_path(directory: str, stem: str, ext: str) -> str:
    """Имя файла без перезаписи: stem_001.ext, stem_002.ext, ..."""
    os.makedirs(directory, exist_ok=True)
    for i in range(1, 100000):
        p = os.path.join(directory, f"{stem}_{i:03d}{ext}")
        if not os.path.exists(p):
            return p
    raise OSError("Не удалось подобрать имя файла")
