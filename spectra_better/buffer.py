"""Буфер спектров (как «Буфер спектров» SpectraSP): снятые спектры откладываются в список,
любые из них показываются поверх текущего для сравнения, переименовываются, сохраняются.

Буфер переживает перезапуск программы: при выходе он записывается в buffer.npz в каталоге
настроек и при следующем запуске читается обратно.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import numpy as np

from .instrument import Measurement

log = logging.getLogger(__name__)

MAX_ENTRIES = 300          # спектр — десятки килобайт; ограничение защищает от «забытой» непрерывной съёмки


@dataclass
class BufferEntry:
    key: str
    name: str
    m: Measurement
    visible: bool = True
    props: Dict[str, str] = field(default_factory=dict)


# названия свойств для таблицы «Свойства» (ключи — из Measurement.meta())
PROP_TITLES = {
    "comment": "Комментарий",
    "measurement_time": "Время",
    "type": "Тип",
    "exposure_ms": "Экспозиция, мс",
    "accumulations": "Накопление",
    "mode": "Режим",
    "rows": "Строки ПЗС",
    "center_wavelength_nm": "Центр, нм",
    "grating": "Решётка",
    "entrance_slit_um": "Входная щель, мкм",
    "ccd_temperature_C": "Температура ПЗС, °C",
    "background_subtracted": "Фон вычтен",
    "saturated": "Насыщение",
    "calibration": "Калибровка",
    "source_file": "Файл",
}


class SpectrumBuffer:
    def __init__(self) -> None:
        self.entries: List[BufferEntry] = []
        self._seq = 0

    # ---------------------------------------------------------------- состав
    def __len__(self) -> int:
        return len(self.entries)

    def add(self, m: Measurement, name: Optional[str] = None, visible: bool = True,
            props: Optional[Dict[str, str]] = None) -> BufferEntry:
        """Положить спектр в буфер. Кадр изображения не хранится (он большой) — только спектр."""
        self._seq += 1
        m = dataclasses.replace(m, image=None)
        if props is None:
            props = {k: str(v) for k, v in m.meta().items()}
        e = BufferEntry(f"b{self._seq}", name or self.default_name(m), m, visible, props)
        self.entries.append(e)
        while len(self.entries) > MAX_ENTRIES:
            old = self.entries.pop(0)
            log.warning("Буфер спектров полон (%d) — удалён самый старый спектр «%s»", MAX_ENTRIES, old.name)
        return e

    @staticmethod
    def default_name(m: Measurement) -> str:
        name = m.label or f"{m.timestamp:%H:%M:%S}"
        if m.comment and not m.label:
            name = f"{m.comment} · {name}"
        if m.center_wl is not None and not m.label:
            name += f"  {m.center_wl:.1f} нм"
        if m.exposure_ms and not m.label:
            name += f", {m.exposure_ms:g} мс"
        return name

    def get(self, key: str) -> Optional[BufferEntry]:
        return next((e for e in self.entries if e.key == key), None)

    def remove(self, keys: Iterable[str]) -> None:
        keys = set(keys)
        self.entries = [e for e in self.entries if e.key not in keys]

    def clear(self) -> None:
        self.entries.clear()

    def rename(self, key: str, name: str) -> None:
        e = self.get(key)
        if e is not None and name.strip():
            e.name = name.strip()

    def set_visible(self, key: str, on: bool) -> None:
        e = self.get(key)
        if e is not None:
            e.visible = bool(on)

    def set_all_visible(self, on: Optional[bool]) -> None:
        """True/False — показать/скрыть все, None — обратить отметки."""
        for e in self.entries:
            e.visible = (not e.visible) if on is None else bool(on)

    def visible(self) -> List[BufferEntry]:
        return [e for e in self.entries if e.visible]

    def properties(self, key: str) -> List[tuple]:
        e = self.get(key)
        if e is None:
            return []
        rows = [("Название", e.name), ("Точек", str(e.m.y.size)),
                ("Ось X", "длина волны, нм" if e.m.x_unit == "nm" else "пиксели")]
        for k, title in PROP_TITLES.items():
            if k in e.props and e.props[k] not in ("", "None"):
                v = e.props[k]
                if k == "measurement_time":
                    try:
                        v = _dt.datetime.fromisoformat(v).strftime("%d.%m.%Y %H:%M:%S")
                    except ValueError:
                        pass
                rows.append((title, v))
        for k, v in e.props.items():                 # прочие свойства (например, из файла) — как есть
            if k not in PROP_TITLES and k not in ("label", "name"):
                rows.append((k, v))
        return rows

    # ---------------------------------------------------------------- между запусками
    def save_state(self, path: str) -> None:
        """Записать весь буфер в один файл .npz (без потерь)."""
        if not self.entries:
            if os.path.exists(path):
                os.remove(path)
            return
        arrays: Dict[str, np.ndarray] = {}
        index = []
        for i, e in enumerate(self.entries):
            arrays[f"x{i}"] = np.asarray(e.m.x, dtype=float)
            arrays[f"y{i}"] = np.asarray(e.m.y, dtype=float)
            arrays[f"raw{i}"] = np.asarray(e.m.raw, dtype=float)
            index.append({
                "name": e.name, "visible": e.visible, "props": e.props, "x_unit": e.m.x_unit,
                "stitched": e.m.stitched, "center_wl": e.m.center_wl, "exposure_ms": e.m.exposure_ms,
                "accumulations": e.m.accumulations, "background_used": e.m.background_used,
                "timestamp": e.m.timestamp.isoformat(timespec="seconds"), "label": e.m.label,
            })
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp.npz"
        np.savez_compressed(tmp, index=np.array(json.dumps(index, ensure_ascii=False)), **arrays)
        os.replace(tmp, path)

    def load_state(self, path: str) -> int:
        """Прочитать буфер, сохранённый save_state. Возвращает число загруженных спектров."""
        if not os.path.isfile(path):
            return 0
        with np.load(path, allow_pickle=False) as z:
            index = json.loads(str(z["index"]))
            for i, d in enumerate(index):
                try:
                    ts = _dt.datetime.fromisoformat(d.get("timestamp", ""))
                except ValueError:
                    ts = _dt.datetime.now()
                m = Measurement(x=z[f"x{i}"], x_unit=d.get("x_unit", "nm"), y=z[f"y{i}"], raw=z[f"raw{i}"],
                                exposure_ms=float(d.get("exposure_ms") or 0), center_wl=d.get("center_wl"),
                                accumulations=int(d.get("accumulations") or 1),
                                background_used=bool(d.get("background_used")), timestamp=ts,
                                label=d.get("label", ""), stitched=bool(d.get("stitched")))
                self.add(m, d.get("name"), bool(d.get("visible", True)), dict(d.get("props") or {}))
        return len(index)
