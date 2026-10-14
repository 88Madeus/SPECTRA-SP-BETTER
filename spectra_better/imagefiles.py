"""Кадр ПЗС в TIFF и PNG — пиксель в пиксель (без сторонних библиотек).

* данные: 16-битное серое изображение (отсчёты камеры как есть); если значения не
  умещаются в 16 бит (сумма накоплений), TIFF пишется 32-битным с плавающей точкой;
* «как на экране»: 8-битное цветное изображение с палитрой и уровнями из окна программы.

Сведения о снимке (экспозиция, длина волны, первая строка ПЗС…) пишутся в TIFF в поле
ImageDescription, в PNG — в текстовые блоки tEXt, и читаются обратно (read_tiff).
"""
from __future__ import annotations

import struct
import zlib
from typing import Dict, Optional, Tuple

import numpy as np


def fits_uint16(img: np.ndarray) -> bool:
    a = np.asarray(img)
    if a.dtype == np.uint16:
        return True
    return bool(a.size and np.all(np.isfinite(a)) and a.min() >= -0.5 and a.max() <= 65535.5)


def as_uint16(img: np.ndarray) -> np.ndarray:
    a = np.asarray(img)
    if a.dtype == np.uint16:
        return a
    return np.clip(np.round(a), 0, 65535).astype(np.uint16)


def _description(meta: Optional[Dict[str, object]]) -> str:
    return "".join(f"{k}: {v}\n" for k, v in (meta or {}).items())


# ---------------------------------------------------------------- TIFF
_TAG_TYPES = {"H": 3, "I": 4, "ascii": 2, "rational": 5}


def write_tiff(path: str, img: np.ndarray, meta: Optional[Dict[str, object]] = None) -> None:
    """Несжатый TIFF (little-endian, одна полоса): 2-D uint16 / float32 или (H, W, 3) uint8."""
    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] == 3:
        a = np.ascontiguousarray(a, dtype=np.uint8)
        bits, spp, photometric, sample_format = (8, 8, 8), 3, 2, 1
    elif a.ndim == 2 and a.dtype == np.uint16:
        a = np.ascontiguousarray(a)
        bits, spp, photometric, sample_format = (16,), 1, 1, 1
    elif a.ndim == 2:
        a = np.ascontiguousarray(a, dtype="<f4")
        bits, spp, photometric, sample_format = (32,), 1, 1, 3
    else:
        raise ValueError("Ожидается кадр (строки × столбцы) или цветное изображение (строки × столбцы × 3)")
    h, w = a.shape[:2]
    data = a.astype(a.dtype.newbyteorder("<"), copy=False).tobytes()
    desc = _description(meta).encode("utf-8") + b"\0"

    entries = []           # (tag, type, count, value-bytes)

    def add(tag, typ, values):
        if typ == "ascii":
            raw, count = values, len(values)
        elif typ == "rational":
            raw = b"".join(struct.pack("<II", n, d) for n, d in values)
            count = len(values)
        else:
            raw = b"".join(struct.pack("<" + typ, v) for v in values)
            count = len(values)
        entries.append((tag, _TAG_TYPES[typ], count, raw))

    add(256, "I", [w])                       # ImageWidth
    add(257, "I", [h])                       # ImageLength
    add(258, "H", list(bits))                # BitsPerSample
    add(259, "H", [1])                       # Compression: нет
    add(262, "H", [photometric])             # 1 — чёрный = 0, 2 — RGB
    add(270, "ascii", desc)                  # ImageDescription
    add(273, "I", [0])                       # StripOffsets — заполняется ниже
    add(277, "H", [spp])                     # SamplesPerPixel
    add(278, "I", [h])                       # RowsPerStrip — весь кадр одной полосой
    add(279, "I", [len(data)])               # StripByteCounts
    add(282, "rational", [(72, 1)])          # XResolution
    add(283, "rational", [(72, 1)])          # YResolution
    add(284, "H", [1])                       # PlanarConfiguration: чередование
    add(296, "H", [2])                       # ResolutionUnit: дюйм
    add(339, "H", [sample_format] * spp)     # SampleFormat: 1 — целые, 3 — с плавающей точкой
    entries.sort(key=lambda e: e[0])

    ifd_offset = 8
    ifd_size = 2 + 12 * len(entries) + 4
    extra_offset = ifd_offset + ifd_size
    extra = b""
    packed = []
    for tag, typ, count, raw in entries:
        if len(raw) <= 4:
            value = raw.ljust(4, b"\0")
        else:
            if (extra_offset + len(extra)) % 2:
                extra += b"\0"
            value = struct.pack("<I", extra_offset + len(extra))
            extra += raw
        packed.append([tag, typ, count, value])
    data_offset = extra_offset + len(extra)
    data_offset += data_offset % 2
    for p in packed:
        if p[0] == 273:
            p[3] = struct.pack("<I", data_offset)
    with open(path, "wb") as f:
        f.write(b"II*\0" + struct.pack("<I", ifd_offset))
        f.write(struct.pack("<H", len(packed)))
        for tag, typ, count, value in packed:
            f.write(struct.pack("<HHI", tag, typ, count) + value)
        f.write(struct.pack("<I", 0))
        f.write(extra)
        f.write(b"\0" * (data_offset - extra_offset - len(extra)))
        f.write(data)


def read_tiff(path: str) -> Tuple[np.ndarray, Dict[str, str]]:
    """Несжатый TIFF (как пишет write_tiff и многие программы): серый 8/16/32 бита или RGB.
    Сжатые и прочие — ValueError (их читает Qt, см. storage.load_image)."""
    with open(path, "rb") as f:
        buf = f.read()
    if buf[:2] == b"II":
        e = "<"
    elif buf[:2] == b"MM":
        e = ">"
    else:
        raise ValueError("Не TIFF")
    if struct.unpack(e + "H", buf[2:4])[0] != 42:
        raise ValueError("Не TIFF")
    off = struct.unpack(e + "I", buf[4:8])[0]
    n = struct.unpack(e + "H", buf[off:off + 2])[0]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}
    fmt = {1: "B", 2: "c", 3: "H", 4: "I"}
    tags: Dict[int, object] = {}
    for i in range(n):
        p = off + 2 + 12 * i
        tag, typ, count = struct.unpack(e + "HHI", buf[p:p + 8])
        size = sizes.get(typ, 1) * count
        raw = buf[p + 8:p + 12] if size <= 4 else buf[struct.unpack(e + "I", buf[p + 8:p + 12])[0]:][:size]
        if typ == 2:
            tags[tag] = raw[:count].rstrip(b"\0").decode("utf-8", "replace")
        elif typ in fmt:
            tags[tag] = list(struct.unpack(e + fmt[typ] * count, raw[:size]))
    if tags.get(259, [1])[0] != 1:
        raise ValueError("Сжатый TIFF")
    w, h = tags[256][0], tags[257][0]
    bits = tags.get(258, [1])
    spp = tags.get(277, [1])[0]
    sf = tags.get(339, [1])[0]
    dt = {(8, 1): "u1", (16, 1): "u2", (32, 1): "u4", (32, 3): "f4", (16, 2): "i2", (32, 2): "i4"}.get((bits[0], sf))
    if dt is None:
        raise ValueError(f"Неподдерживаемый формат TIFF: {bits[0]} бит")
    offsets, counts = tags[273], tags[279]
    data = b"".join(buf[o:o + c] for o, c in zip(offsets, counts))
    a = np.frombuffer(data, dtype=np.dtype(dt).newbyteorder(e))
    a = a[:w * h * spp].reshape((h, w, spp) if spp > 1 else (h, w)).astype(np.dtype(dt).newbyteorder("="))
    meta: Dict[str, str] = {}
    for line in str(tags.get(270, "")).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return a, meta


# ---------------------------------------------------------------- PNG
def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: str, img: np.ndarray, meta: Optional[Dict[str, object]] = None) -> None:
    """PNG: 2-D uint16 (16-битный серый) или (H, W, 3) uint8 (цветной)."""
    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] == 3:
        a = np.ascontiguousarray(a, dtype=np.uint8)
        bit_depth, color_type = 8, 2
        rows = a.reshape(a.shape[0], -1)
    elif a.ndim == 2 and a.dtype == np.uint16:
        bit_depth, color_type = 16, 0
        rows = np.ascontiguousarray(a.astype(">u2")).view(np.uint8).reshape(a.shape[0], -1)
    else:
        raise ValueError("В PNG пишется только 16-битный серый кадр или цветное изображение")
    h, w = a.shape[:2]
    raw = np.hstack([np.zeros((h, 1), dtype=np.uint8), rows]).tobytes()     # фильтр 0 у каждой строки
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bit_depth, color_type, 0, 0, 0))
    for k, v in (meta or {}).items():
        key = str(k).encode("latin-1", "replace")[:79]
        out += _chunk(b"iTXt", key + b"\0\0\0\0\0" + str(v).encode("utf-8"))
    out += _chunk(b"IDAT", zlib.compress(raw, 6))
    out += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(out)


def read_png_text(path: str) -> Dict[str, str]:
    """Текстовые блоки PNG (tEXt/iTXt) — сведения о снимке, записанные write_png."""
    meta: Dict[str, str] = {}
    with open(path, "rb") as f:
        buf = f.read()
    p = 8
    while p + 8 <= len(buf):
        n = struct.unpack(">I", buf[p:p + 4])[0]
        kind, data = buf[p + 4:p + 8], buf[p + 8:p + 8 + n]
        if kind == b"tEXt" and b"\0" in data:
            k, v = data.split(b"\0", 1)
            meta[k.decode("latin-1")] = v.decode("latin-1")
        elif kind == b"iTXt" and b"\0" in data:
            k, rest = data.split(b"\0", 1)
            parts = rest[2:].split(b"\0", 2)            # флаг и метод сжатия, язык, перевод ключа, текст
            if len(parts) == 3 and rest[0] == 0:
                meta[k.decode("latin-1")] = parts[2].decode("utf-8", "replace")
        if kind == b"IEND":
            break
        p += 12 + n
    return meta
