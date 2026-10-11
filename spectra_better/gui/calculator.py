"""Калькулятор спектров (как «Calculator» SpectraSP): операции над текущим спектром и спектрами
буфера — разность, отношение, пропускание, оптическая плотность, нормировка. Результат — в буфер."""
from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Callable, List, Optional, Tuple

import numpy as np
from PyQt5 import QtWidgets

from .. import processing as P
from ..instrument import Measurement

Source = Tuple[str, Measurement]          # (название, спектр)


class CalculatorDialog(QtWidgets.QDialog):
    def __init__(self, sources: Callable[[], List[Source]], on_result: Callable[[Measurement, str, dict], None],
                 parent=None):
        super().__init__(parent)
        self.sources = sources
        self.on_result = on_result
        self.setWindowTitle("Калькулятор спектров")
        form = QtWidgets.QFormLayout(self)
        intro = QtWidgets.QLabel("Операнды — текущий спектр и спектры буфера. Если оси разные, B "
                                 "пересчитывается на длины волн A. Результат кладётся в буфер.")
        intro.setWordWrap(True)
        form.addRow(intro)
        self.a = QtWidgets.QComboBox()
        self.op = QtWidgets.QComboBox()
        for key, (title, _b, _k) in P.OPERATIONS.items():
            self.op.addItem(title, key)
        self.b = QtWidgets.QComboBox()
        self.k = QtWidgets.QDoubleSpinBox()
        self.k.setRange(-1e9, 1e9)
        self.k.setDecimals(6)
        self.k.setValue(1.0)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("по умолчанию — формула")
        form.addRow("A:", self.a)
        form.addRow("Операция:", self.op)
        form.addRow("B:", self.b)
        form.addRow("k:", self.k)
        form.addRow("Название:", self.name)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        form.addRow(self.status)
        bb = QtWidgets.QDialogButtonBox()
        self.btn_calc = bb.addButton("Вычислить", QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton("Закрыть", QtWidgets.QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.calculate)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.op.currentIndexChanged.connect(self._update)
        self.refresh()
        self.resize(480, 300)

    def refresh(self) -> None:
        """Обновить списки операндов (после изменения буфера или нового снимка)."""
        items = self.sources()
        for combo in (self.a, self.b):
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            for title, _m in items:
                combo.addItem(title)
            i = combo.findText(cur)
            combo.setCurrentIndex(i if i >= 0 else (1 if combo is self.b and len(items) > 1 else 0))
            combo.blockSignals(False)
        self._update()

    def _update(self) -> None:
        _title, need_b, need_k = P.OPERATIONS[self.op.currentData()]
        self.b.setEnabled(need_b)
        self.k.setEnabled(need_k)
        self.btn_calc.setEnabled(self.a.count() > 0 and (not need_b or self.b.count() > 0))

    def _get(self, combo: QtWidgets.QComboBox) -> Optional[Source]:
        items = self.sources()
        i = combo.currentIndex()
        return items[i] if 0 <= i < len(items) else None

    def calculate(self) -> Optional[Measurement]:
        op = self.op.currentData()
        _title, need_b, _need_k = P.OPERATIONS[op]
        sa = self._get(self.a)
        sb = self._get(self.b) if need_b else None
        if sa is None or (need_b and sb is None):
            return None
        (na, a) = sa
        try:
            if need_b:
                nb, b = sb
                if (a.x_unit == "nm") != (b.x_unit == "nm"):
                    raise ValueError("У одного спектра шкала в нм, у другого — в пикселях")
                y = P.spectrum_math(op, a.x, a.y, b.x, b.y, same_axis=a.x_unit != "nm")
            else:
                y = P.spectrum_math(op, a.x, a.y, k=self.k.value())
        except ValueError as e:
            self.status.setText(f"<span style='color:#b00020'>{e}</span>")
            return None
        formula = {"sub": f"[{na}] − [{nb}]" if need_b else "", "add": f"[{na}] + [{nb}]" if need_b else "",
                   "div": f"[{na}] ÷ [{nb}]" if need_b else "", "mul": f"[{na}] × [{nb}]" if need_b else "",
                   "trans": f"[{na}] ÷ [{nb}] × 100 %" if need_b else "",
                   "absorb": f"−lg([{na}] ÷ [{nb}])" if need_b else "",
                   "scale": f"[{na}] × {self.k.value():g}", "offset": f"[{na}] + {self.k.value():g}",
                   "norm_max": f"[{na}] ÷ max", "norm_area": f"[{na}] ÷ площадь"}[op]
        name = self.name.text().strip() or formula
        m = dataclasses.replace(a, y=y, raw=y, image=None, tracks=None, track_rows=a.track_rows[:1],
                                background_used=False, saturated=False, intensity_corrected="",
                                is_background=False, label=name, timestamp=_dt.datetime.now())
        props = {k: str(v) for k, v in a.meta().items()}
        props["formula"] = formula
        props["type"] = "result"
        self.on_result(m, name, props)
        good = int(np.isfinite(y).sum())
        self.status.setText(f"Готово: «{name}» в буфере ({good} точек из {y.size}).")
        return m
