"""Графики: спектр (с наложениями, курсором и пиками) и изображение ПЗС.

Над спектром — панель вида: в каких единицах показывать ось X (снизу и, при
желании, дополнительная шкала сверху) и какой диапазон по X и Y показывать.
Выбор запоминается в настройках программы.
"""
from __future__ import annotations

from math import ceil, log10
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ..config import AppSettings
from ..units import X_DECIMALS, X_NM, X_PX, X_RAMAN, X_SHORT, X_UNITS, AxisMap, axis_label, convert_range

OVERLAY_COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]
# полосы строк ПЗС: 1 — жёлтая (основная), дальше — свои цвета (и на изображении, и на графике)
STRIPE_COLORS = ["#e6b800", "#ff7f0e", "#2ca02c", "#9467bd", "#17becf", "#e377c2", "#8c564b", "#7f7f7f"]

Fn = Callable[[np.ndarray], np.ndarray]
Mapping = Optional[Tuple[Fn, Fn]]


def format_ticks(values: Sequence[float], spacing: float) -> List[str]:
    """Подписи делений целиком (19500, а не 1.95e+04) с числом знаков по шагу."""
    places = min(6, max(0, int(ceil(-log10(spacing))))) if spacing > 0 else 0
    out = []
    for v in values:
        v = float(v)
        if abs(v) < spacing * 1e-6:
            v = 0.0                       # без «-0»
        out.append(f"{v:.{places}f}" if v == 0 or abs(v) >= 1e-4 else f"{v:g}")
    return out


class LinkedAxis(pg.AxisItem):
    """Ось, подписанная в других единицах, чем координаты графика.

    mapping = (fwd, inv): fwd переводит координаты графика в единицы подписи,
    inv — обратно (обе монотонны). Деления ставятся на «круглые» значения в
    единицах подписи. Без mapping — обычная ось.
    """

    def __init__(self, orientation, **kw):
        super().__init__(orientation, **kw)
        self.enableAutoSIPrefix(False)
        self._map: Mapping = None

    def set_mapping(self, mapping: Mapping) -> None:
        self._map = mapping
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        if self._map is None:
            return super().tickValues(minVal, maxVal, size)
        fwd, inv = self._map
        try:
            with np.errstate(all="ignore"):
                a, b = np.asarray(fwd(np.array([minVal, maxVal], dtype=float)), dtype=float)
        except ValueError:
            return []
        if not (np.isfinite(a) and np.isfinite(b)) or a == b:
            return []
        out = []
        for spacing, vals in super().tickValues(min(a, b), max(a, b), size):
            if len(vals):
                with np.errstate(all="ignore"):
                    prim = np.asarray(inv(np.asarray(vals, dtype=float)), dtype=float)
                vals = [float(p) for p in prim[np.isfinite(prim)]]
            out.append((spacing, vals))
        return out

    def tickStrings(self, values, scale, spacing):
        if self._map is not None and len(values):
            with np.errstate(all="ignore"):
                values = np.asarray(self._map[0](np.asarray(values, dtype=float)), dtype=float)
        return format_ticks(values, spacing * scale)


def _range_spin() -> QtWidgets.QDoubleSpinBox:
    sp = QtWidgets.QDoubleSpinBox()
    sp.setRange(-1e9, 1e9)
    sp.setKeyboardTracking(False)          # значение применяется по Enter / уходу из поля
    sp.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
    sp.setFixedWidth(86)
    sp.setAlignment(QtCore.Qt.AlignRight)
    return sp


def _fmt_pos(v: float, unit: str) -> str:
    if unit == X_PX:
        return f"пиксель {v:.1f}"
    if unit == X_RAMAN:
        return f"сдвиг {v:.1f} см⁻¹"
    return f"{v:.{X_DECIMALS.get(unit, 3)}f} {X_SHORT.get(unit, unit)}"


class SpectrumPlot(QtWidgets.QWidget):
    cursor_moved = QtCore.pyqtSignal(float, float)
    units_changed = QtCore.pyqtSignal()     # сменились единицы оси X или длина волны лазера

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.s = settings
        if settings.x_unit not in X_UNITS:
            settings.x_unit = X_NM
        if settings.x_top_unit not in X_UNITS:
            settings.x_top_unit = ""
        self._amap = AxisMap(None, settings.laser_nm)
        self._unit = settings.x_unit           # в каких единицах реально построен график
        self._applied_unit: Optional[str] = None
        self._info, self._info_warn, self._note = "", False, ""
        self._x = np.array([])
        self._y = np.array([])

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self._build_bar())
        self.info = QtWidgets.QLabel(" ")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.info.setContentsMargins(6, 0, 6, 0)
        lay.addWidget(self.info)

        self.bottom_axis = LinkedAxis("bottom")
        self.top_axis = LinkedAxis("top")
        self.plot = pg.PlotWidget(axisItems={"bottom": self.bottom_axis, "top": self.top_axis})
        self.plot.hideButtons()                 # вместо кнопки «A» — «Весь спектр» (Ctrl+A)
        self.plot.showAxis("top", False)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Интенсивность, отсч.")
        self.legend = self.plot.addLegend(offset=(-10, 10))
        self.vb = self.plot.getPlotItem().getViewBox()
        self.vb.setDefaultPadding(0.06)         # запас по Y, чтобы подписи пиков у верхнего края не обрезались
        lay.addWidget(self.plot, 1)
        self.readout = QtWidgets.QLabel(" ")
        self.readout.setStyleSheet("color: #444; padding: 2px 6px;")
        self.readout.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.readout)

        self.curve = self.plot.plot([], [], pen=pg.mkPen("#d62728", width=1.4), name="Текущий")
        self.bg_curve = self.plot.plot([], [], pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.DashLine))
        self.bg_curve.setVisible(False)
        self._bg_in_legend = False
        self.overlays: Dict[str, pg.PlotDataItem] = {}
        self.track_curves: List[pg.PlotDataItem] = []       # полосы 2…8 текущего измерения
        self.peak_items: List[pg.GraphicsObject] = []

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#999", width=1))
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#999", width=1))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        self._proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved)

        self.vb.sigRangeChanged.connect(self._sync_range_fields)
        self.vb.sigRangeChangedManually.connect(self._range_changed_by_mouse)
        self.vb.menu.viewAll.triggered.connect(self.show_all)
        self.set_scale(self._amap, self._unit)
        self._apply_y()

    # ------------------------------------------------------------------ панель вида
    def _build_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setFrameShape(QtWidgets.QFrame.StyledPanel)
        v = QtWidgets.QVBoxLayout(bar)
        v.setContentsMargins(6, 3, 6, 3)
        v.setSpacing(3)

        row = QtWidgets.QHBoxLayout()
        self.unit_box = QtWidgets.QComboBox()
        self.unit_box.setToolTip("В каких единицах подписана нижняя ось X")
        self.top_box = QtWidgets.QComboBox()
        self.top_box.setToolTip("Вторая шкала над графиком — например, пиксели ПЗС или рамановский сдвиг")
        self.top_box.addItem("нет", "")
        for k, text in X_UNITS.items():
            self.unit_box.addItem(text, k)
            self.top_box.addItem(text, k)
        for cb in (self.unit_box, self.top_box):     # чтобы панель не расширяла окно на малых экранах
            cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(8)
            cb.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.unit_box.setCurrentIndex(max(0, self.unit_box.findData(self.s.x_unit)))
        self.top_box.setCurrentIndex(max(0, self.top_box.findData(self.s.x_top_unit)))
        self.lbl_laser = QtWidgets.QLabel("лазер:")
        self.laser = QtWidgets.QDoubleSpinBox()
        self.laser.setRange(100, 3000)
        self.laser.setDecimals(2)
        self.laser.setSuffix(" нм")
        self.laser.setKeyboardTracking(False)
        self.laser.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.laser.setFixedWidth(100)
        self.laser.setValue(self.s.laser_nm)
        self.laser.setToolTip("Длина волны возбуждающего лазера для рамановского сдвига")
        for w in (QtWidgets.QLabel("Ось X:"), self.unit_box, QtWidgets.QLabel("сверху:"), self.top_box,
                  self.lbl_laser, self.laser):
            row.addWidget(w)
        v.addLayout(row)

        g = QtWidgets.QGridLayout()
        g.setHorizontalSpacing(6)
        self.x_from, self.x_to = _range_spin(), _range_spin()
        self.y_from, self.y_to = _range_spin(), _range_spin()
        for sp in (self.y_from, self.y_to):
            sp.setDecimals(0)
        self.x_unit_lbl = QtWidgets.QLabel("нм")
        self.x_auto = QtWidgets.QCheckBox("весь спектр")
        self.x_auto.setToolTip("Каждый новый спектр показывается целиком. Снимите флажок (или задайте "
                               "границы, или увеличьте участок колесом мыши), чтобы держать выбранный участок.\n"
                               "Вернуть весь спектр по X и Y — кнопка «Весь спектр» на панели инструментов (Ctrl+A).")
        self.y_auto = QtWidgets.QCheckBox("авто")
        self.y_auto.setToolTip("Масштаб по Y подбирается по видимому участку спектра")
        for r, (name, a, b, unit, auto) in enumerate((
                ("Диапазон X: от", self.x_from, self.x_to, self.x_unit_lbl, self.x_auto),
                ("Диапазон Y: от", self.y_from, self.y_to, QtWidgets.QLabel("отсч."), self.y_auto))):
            for c, w in enumerate((QtWidgets.QLabel(name), a, QtWidgets.QLabel("до"), b, unit, auto)):
                g.addWidget(w, r, c)
        g.setColumnStretch(6, 1)
        v.addLayout(g)

        self.unit_box.currentIndexChanged.connect(self._unit_selected)
        self.top_box.currentIndexChanged.connect(self._top_selected)
        self.laser.valueChanged.connect(self._laser_changed)
        self.x_from.valueChanged.connect(self._x_edited)
        self.x_to.valueChanged.connect(self._x_edited)
        self.y_from.valueChanged.connect(self._y_edited)
        self.y_to.valueChanged.connect(self._y_edited)
        self.x_auto.toggled.connect(self._x_auto_toggled)
        self.y_auto.toggled.connect(self._y_auto_toggled)
        self._update_laser_visibility()
        return bar

    def _update_laser_visibility(self) -> None:
        on = X_RAMAN in (self.s.x_unit, self.s.x_top_unit)
        self.lbl_laser.setVisible(on)
        self.laser.setVisible(on)

    def _unit_selected(self) -> None:
        unit = self.unit_box.currentData()
        if unit and unit != self.s.x_unit:
            self.s.x_unit = unit
            self._update_laser_visibility()
            self.units_changed.emit()

    def _top_selected(self) -> None:
        self.s.x_top_unit = self.top_box.currentData() or ""
        self._update_laser_visibility()
        self.units_changed.emit()          # главное окно заново решит, какую шкалу можно показать сверху
        self._update_info()

    def _laser_changed(self, value: float) -> None:
        self.s.laser_nm = float(value)
        self.units_changed.emit()

    # ------------------------------------------------------------------ шкала и диапазон
    def set_scale(self, amap: AxisMap, unit: str, pixels_ok: bool = True) -> None:
        """Шкала текущих данных: amap — пересчёт единиц, unit — в чём построены данные;
        pixels_ok=False — точки не соответствуют пикселям ПЗС (сшитая панорама): шкалу
        и положение курсора в пикселях не показываем."""
        self._amap, self._unit = amap, unit
        self._pixels_ok = pixels_ok
        self.plot.setLabel("bottom", axis_label(unit, amap.laser_nm)
                           + ("" if unit == self.s.x_unit else "  (нет шкалы длин волн)"))
        top = self.s.x_top_unit
        if top and top != unit and (pixels_ok or top != X_PX) and amap.can_convert(unit, top):
            self.top_axis.set_mapping((lambda v: amap.convert(v, unit, top),
                                       lambda v: amap.convert(v, top, unit)))
            self.plot.setLabel("top", axis_label(top, amap.laser_nm))
            self.plot.showAxis("top", True)
        else:
            self.top_axis.set_mapping(None)
            self.plot.showAxis("top", False)
        self.x_unit_lbl.setText("см⁻¹" if unit == X_RAMAN else X_SHORT.get(unit, unit))
        dec = X_DECIMALS.get(unit, 3)
        for sp in (self.x_from, self.x_to):
            sp.blockSignals(True)            # округление при смене точности — не ввод пользователя
            sp.setDecimals(dec)
            sp.blockSignals(False)
        self._sync_range_fields()

    def refresh_view(self) -> None:
        """После обновления данных: при «весь спектр» — подогнать X, при смене единиц — пересчитать диапазон."""
        if self.s.x_auto or self._applied_unit != self._unit:
            self._apply_x()
        else:
            self._update_info()

    def show_all(self) -> None:
        self.s.x_auto = self.s.y_auto = True
        self._apply_x()
        self._apply_y()

    def auto_range(self) -> None:          # совместимость со старым именем
        self.show_all()

    def _data_x_bounds(self) -> Optional[Tuple[float, float]]:
        arrays = [self._x] + [it.xData for it in self.overlays.values() if it.isVisible()] \
            + [it.xData for it in self.track_curves]
        lo, hi = [], []
        for a in arrays:
            if a is not None and np.size(a):
                a = np.asarray(a, dtype=float)
                a = a[np.isfinite(a)]
                if a.size:
                    lo.append(float(a.min()))
                    hi.append(float(a.max()))
        if not lo:
            return None
        a, b = min(lo), max(hi)
        return (a - 0.5, b + 0.5) if a == b else (a, b)

    def _apply_x(self) -> None:
        self._note = ""
        rng = None
        if not self.s.x_auto and len(self.s.x_range) == 2:
            rng = convert_range(self.s.x_range, self.s.x_range_unit, self._unit, self._amap, self.s.laser_nm)
            if rng is None:
                self._note = (f"Заданный диапазон X ({X_SHORT.get(self.s.x_range_unit, '')}) нельзя показать "
                              "в пикселях без шкалы длин волн — показан весь спектр.")
        if rng is None:
            rng = self._data_x_bounds()
        if rng is not None:
            self.vb.setXRange(rng[0], rng[1], padding=0)
            if self.s.y_auto:
                self.vb.updateAutoRange()     # Y — по новому видимому участку
        self._applied_unit = self._unit
        self._sync_range_fields()
        self._update_info()

    def _apply_y(self) -> None:
        if self.s.y_auto or len(self.s.y_range) != 2:
            self.s.y_auto = True
            self.vb.setAutoVisible(y=True)
            self.vb.enableAutoRange(y=True)
            self.vb.updateAutoRange()
        else:
            self.vb.setYRange(self.s.y_range[0], self.s.y_range[1], padding=0)
        self._sync_range_fields()

    def _x_edited(self) -> None:
        a, b = self.x_from.value(), self.x_to.value()
        if a == b:
            self._sync_range_fields()
            return
        self.s.x_auto = False
        self.s.x_range = [min(a, b), max(a, b)]
        self.s.x_range_unit = self._unit
        self._apply_x()

    def _y_edited(self) -> None:
        a, b = self.y_from.value(), self.y_to.value()
        if a == b:
            self._sync_range_fields()
            return
        self.s.y_auto = False
        self.s.y_range = [min(a, b), max(a, b)]
        self._apply_y()

    def _x_auto_toggled(self, on: bool) -> None:
        self.s.x_auto = on
        if on:
            self._apply_x()
        else:                                   # удерживать то, что видно сейчас
            self.s.x_range = [float(v) for v in self.vb.viewRange()[0]]
            self.s.x_range_unit = self._unit

    def _y_auto_toggled(self, on: bool) -> None:
        if on:
            self.s.y_auto = True
        else:
            self.s.y_auto = False
            self.s.y_range = [float(v) for v in self.vb.viewRange()[1]]
            self.vb.enableAutoRange(y=False)
        self._apply_y()

    def _range_changed_by_mouse(self, mask) -> None:
        """Пользователь сдвинул/увеличил график мышью — запоминаем показанный участок."""
        xr, yr = self.vb.viewRange()
        if mask[0]:
            self.s.x_auto = False
            self.s.x_range = [float(v) for v in xr]
            self.s.x_range_unit = self._unit
            self._applied_unit = self._unit
        if self.s.y_auto and not self.vb.autoRangeEnabled()[1]:
            self.s.y_auto = False
        if not self.s.y_auto:
            self.s.y_range = [float(v) for v in yr]
        elif mask[0]:
            self.vb.updateAutoRange()
        self._sync_range_fields()

    def _sync_range_fields(self, *_) -> None:
        xr, yr = self.vb.viewRange()
        for sp, val in ((self.x_from, xr[0]), (self.x_to, xr[1]), (self.y_from, yr[0]), (self.y_to, yr[1])):
            if not sp.hasFocus():
                sp.blockSignals(True)
                sp.setValue(float(val))
                sp.blockSignals(False)
        for cb, val in ((self.x_auto, self.s.x_auto), (self.y_auto, self.s.y_auto)):
            cb.blockSignals(True)
            cb.setChecked(bool(val))
            cb.blockSignals(False)

    # ------------------------------------------------------------------ подпись над графиком
    def set_info(self, text: str, warn: bool = False) -> None:
        """Что охватывает кадр (или почему нет шкалы длин волн)."""
        self._info, self._info_warn = text, warn
        self._update_info()

    def _update_info(self) -> None:
        text = self._info
        if self._note:
            text = (text + "  " if text else "") + self._note
        warn = self._info_warn or bool(self._note)
        self.info.setText(text or " ")
        self.info.setStyleSheet("color: #a15c00;" if warn else "color: #444;")

    # ------------------------------------------------------------------ данные
    def set_x_label(self, text: str) -> None:
        self.plot.setLabel("bottom", text)

    def set_data(self, x: np.ndarray, y: np.ndarray, name: str = "Текущий") -> None:
        self._x, self._y = np.asarray(x), np.asarray(y)
        self.curve.setData(self._x, self._y)
        self.legend.removeItem(self.curve)
        self.legend.addItem(self.curve, name)

    def set_background(self, x: Optional[np.ndarray], y: Optional[np.ndarray], visible: bool) -> None:
        if x is None or y is None:
            self.bg_curve.setData([], [])
            visible = False
        else:
            self.bg_curve.setData(x, y)
        self.bg_curve.setVisible(visible)
        if visible and not self._bg_in_legend:
            self.legend.addItem(self.bg_curve, "Фон")
            self._bg_in_legend = True
        elif not visible and self._bg_in_legend:
            self.legend.removeItem(self.bg_curve)
            self._bg_in_legend = False

    def set_tracks(self, tracks: Sequence[tuple]) -> None:
        """Спектры дополнительных полос текущего измерения: [(x, y, название), …] (полосы 2, 3, …)."""
        while len(self.track_curves) > len(tracks):
            item = self.track_curves.pop()
            self.plot.removeItem(item)
            self.legend.removeItem(item)
        for i, (x, y, name) in enumerate(tracks):
            color = STRIPE_COLORS[(i + 1) % len(STRIPE_COLORS)]
            if i >= len(self.track_curves):
                self.track_curves.append(self.plot.plot([], [], pen=pg.mkPen(color, width=1.3), name=name))
            item = self.track_curves[i]
            if item.name() != name:
                self.legend.removeItem(item)
                item.opts["name"] = name
                self.legend.addItem(item, name)
            if x is None:
                item.setData([], [])
            else:
                item.setData(x, y)

    def add_overlay(self, key: str, x: Optional[np.ndarray], y: np.ndarray, name: str,
                    color: Optional[str] = None) -> None:
        self.remove_overlay(key)
        color = color or OVERLAY_COLORS[len(self.overlays) % len(OVERLAY_COLORS)]
        self.overlays[key] = self.plot.plot([], [], pen=pg.mkPen(color, width=1.1), name=name)
        self.update_overlay(key, x, y)

    def update_overlay(self, key: str, x: Optional[np.ndarray], y: np.ndarray) -> None:
        """x=None — спектр нельзя показать в текущих единицах (например, файл в пикселях)."""
        item = self.overlays.get(key)
        if item is None:
            return
        if x is None:
            item.setData([], [])
            item.setVisible(False)
        else:
            item.setData(x, y)
            item.setVisible(True)

    def remove_overlay(self, key: str) -> None:
        item = self.overlays.pop(key, None)
        if item is not None:
            self.plot.removeItem(item)
            self.legend.removeItem(item)

    def clear_overlays(self) -> None:
        for k in list(self.overlays):
            self.remove_overlay(k)

    def show_peaks(self, xs: Sequence[float], ys: Sequence[float], labels: Sequence[str]) -> None:
        for it in self.peak_items:
            self.plot.removeItem(it)
        self.peak_items = []
        if not len(xs):
            return
        sc = pg.ScatterPlotItem(list(xs), list(ys), symbol="t", size=9, brush=pg.mkBrush("#ff7f0e"), pen=None)
        self.plot.addItem(sc)
        self.peak_items.append(sc)
        for x, y, t in zip(xs, ys, labels):
            ti = pg.TextItem(t, color="#b35900", anchor=(0.5, 1.2))
            ti.setPos(x, y)
            self.plot.addItem(ti)
            self.peak_items.append(ti)

    # ------------------------------------------------------------------ курсор
    def cursor_text(self, x: float, y: float) -> str:
        parts = [_fmt_pos(x, self._unit)]
        for u in (X_NM, X_PX, self.s.x_top_unit):
            if u == X_PX and not self._pixels_ok:
                continue
            if u and u != self._unit and self._amap.can_convert(self._unit, u):
                txt = _fmt_pos(float(self._amap.convert([x], self._unit, u)[0]), u)
                if txt not in parts:
                    parts.append(txt)
        text = "Курсор: " + "  |  ".join(parts) + f"      I = {y:.1f}"
        if self._x.size:
            i = int(np.argmin(np.abs(self._x - x)))
            text += f"      спектр здесь: I = {self._y[i]:.1f} ({'пиксель' if self._pixels_ok else 'точка'} {i})"
        return text

    def _mouse_moved(self, evt) -> None:
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        p = self.vb.mapSceneToView(pos)
        self.vline.setPos(p.x())
        self.hline.setPos(p.y())
        self.readout.setText(self.cursor_text(p.x(), p.y()))
        self.cursor_moved.emit(p.x(), p.y())


class ImagePanel(QtWidgets.QWidget):
    """Кадр ПЗС и выбор полос строк для спектров."""
    rows_changed = QtCore.pyqtSignal(int, int)
    stripes_changed = QtCore.pyqtSignal(list)      # [(верх, низ), …] — после перетаскивания любой полосы

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.x_axis = LinkedAxis("bottom")
        self.top_axis = LinkedAxis("top")
        self.plot_item = pg.PlotItem(axisItems={"bottom": self.x_axis, "top": self.top_axis})
        self.plot_item.setLabel("left", "Строка ПЗС")
        self.view = pg.ImageView(view=self.plot_item)
        self.view.ui.roiBtn.hide()
        self.view.ui.menuBtn.hide()
        self.view.setPredefinedGradient("viridis")
        self.view.getView().setAspectLocked(False)
        lay.addWidget(self.view)
        hint = QtWidgets.QLabel("Перетащите жёлтую полосу, чтобы выбрать строки ПЗС, по которым строится спектр. "
                                "Дополнительные полосы (свой спектр с каждой) — «Полосы…» на панели «Экспозиция».")
        hint.setStyleSheet("color: #555; padding: 2px 6px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.region = pg.LinearRegionItem(values=(280, 310), orientation="horizontal",
                                          brush=pg.mkBrush(255, 220, 0, 50),
                                          pen=pg.mkPen("#e6b800", width=1.5))
        self.view.getView().addItem(self.region)
        self.region.sigRegionChangeFinished.connect(self._region_changed)
        self.extra_regions: List[pg.LinearRegionItem] = []
        self.labels: List[pg.TextItem] = []
        self._first = True
        self._suppress = False
        self.set_x_scale(AxisMap(None), X_PX, 0)

    def set_x_scale(self, amap: AxisMap, unit: str, n_columns: int) -> None:
        """Подписать ось столбцов в выбранных единицах (столбец i занимает [i, i+1])."""
        centre = (lambda v: np.asarray(v, dtype=float) - 0.5, lambda v: np.asarray(v, dtype=float) + 0.5)
        ok = (unit != X_PX and amap.calibrated and amap.wl.size == n_columns and amap.can_convert(X_PX, unit))
        if ok:
            self.x_axis.set_mapping((lambda v: amap.convert(np.asarray(v, dtype=float) - 0.5, X_PX, unit),
                                     lambda v: amap.convert(v, unit, X_PX) + 0.5))
            self.plot_item.setLabel("bottom", axis_label(unit, amap.laser_nm))
            self.top_axis.set_mapping(centre)
            self.plot_item.setLabel("top", "Столбец ПЗС (пиксель)")
            self.plot_item.showAxis("top", True)
        else:
            self.x_axis.set_mapping(centre)
            self.plot_item.setLabel("bottom", "Столбец ПЗС (пиксель)")
            self.top_axis.set_mapping(None)
            self.plot_item.showAxis("top", False)

    def set_image(self, image: np.ndarray) -> None:
        # ImageView ожидает (x, y): транспонируем, чтобы столбцы ПЗС шли по горизонтали
        img = np.asarray(image, dtype=np.float32).T
        if self._first:
            # уровни по процентилям, а не по мин./макс.: одна космическая частица
            # иначе «растягивает» шкалу, и весь кадр выглядит чёрным
            lo, hi = (float(v) for v in np.percentile(img, (0.5, 99.95)))
            self.view.setImage(img, autoLevels=False, levels=(lo, max(hi, lo + 1)), autoRange=True)
        else:
            self.view.setImage(img, autoLevels=False, autoRange=False)
        self._first = False

    def set_rows(self, top: int, bottom: int) -> None:
        self._suppress = True
        self.region.setRegion((top, bottom + 1))
        self._suppress = False

    def set_stripes(self, stripes) -> None:
        """Показать все полосы: первая — жёлтая, остальные — своими цветами, с номерами."""
        stripes = list(stripes) or [(0, 0)]
        self._suppress = True
        self.region.setRegion((stripes[0][0], stripes[0][1] + 1))
        view = self.view.getView()
        while len(self.extra_regions) > len(stripes) - 1:
            view.removeItem(self.extra_regions.pop())
        while len(self.extra_regions) < len(stripes) - 1:
            color = QtGui.QColor(STRIPE_COLORS[(len(self.extra_regions) + 1) % len(STRIPE_COLORS)])
            fill = QtGui.QColor(color)
            fill.setAlpha(45)
            reg = pg.LinearRegionItem(orientation="horizontal", brush=pg.mkBrush(fill),
                                      pen=pg.mkPen(color, width=1.5))
            reg.sigRegionChangeFinished.connect(self._region_changed)
            view.addItem(reg)
            self.extra_regions.append(reg)
        for reg, (t, b) in zip(self.extra_regions, stripes[1:]):
            reg.setRegion((t, b + 1))
        for lab in self.labels:
            view.removeItem(lab)
        self.labels = []
        if len(stripes) > 1:
            for i, (t, b) in enumerate(stripes):
                lab = pg.TextItem(str(i + 1), color=STRIPE_COLORS[i % len(STRIPE_COLORS)], anchor=(0, 0.5))
                lab.setPos(4, (t + b + 1) / 2)
                view.addItem(lab)
                self.labels.append(lab)
        self._suppress = False

    def stripes(self) -> List[tuple]:
        out = []
        for reg in [self.region] + self.extra_regions:
            a, b = reg.getRegion()
            top = int(round(min(a, b)))
            bottom = int(round(max(a, b))) - 1
            out.append((max(0, top), max(top, bottom)))
        return out

    def _region_changed(self) -> None:
        if self._suppress:
            return
        self.stripes_changed.emit(self.stripes())
