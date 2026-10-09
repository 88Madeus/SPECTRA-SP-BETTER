"""Панель «Буфер спектров»: список отложенных спектров с отметками «показывать на графике»."""
from __future__ import annotations

from typing import List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ..buffer import BufferEntry, SpectrumBuffer
from .plots import OVERLAY_COLORS

KEY_ROLE = QtCore.Qt.UserRole


def entry_color(e: BufferEntry) -> str:
    """Постоянный цвет спектра в буфере (по его номеру, а не по месту в списке)."""
    return OVERLAY_COLORS[(int(e.key[1:]) - 1) % len(OVERLAY_COLORS)]


def _swatch(color: str) -> QtGui.QIcon:
    pm = QtGui.QPixmap(14, 14)
    pm.fill(QtGui.QColor(color))
    return QtGui.QIcon(pm)


class BufferPanel(QtWidgets.QWidget):
    """Сигналы: changed — изменились состав, отметки или названия (перерисовать график);
    make_current(key) — показать спектр из буфера как текущий; add_current/load_files/save_entries —
    действия, которые выполняет главное окно."""
    changed = QtCore.pyqtSignal()
    make_current = QtCore.pyqtSignal(str)
    add_current = QtCore.pyqtSignal()
    load_files = QtCore.pyqtSignal()
    save_entries = QtCore.pyqtSignal(list)

    def __init__(self, buffer: SpectrumBuffer, parent=None):
        super().__init__(parent)
        self.buffer = buffer
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QtWidgets.QLabel("Отметьте флажком спектры, которые показать на графике вместе с текущим.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        lay.addWidget(hint)

        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("В буфер")
        self.btn_add.setToolTip("Положить текущий спектр в буфер (Ctrl+D)")
        self.btn_load = QtWidgets.QPushButton("Загрузить…")
        self.btn_load.setToolTip("Загрузить спектры из файлов в буфер")
        self.btn_save = QtWidgets.QPushButton("Сохранить…")
        self.btn_save.setToolTip("Сохранить выделенные (или отмеченные) спектры в файлы")
        for b in (self.btn_add, self.btn_load, self.btn_save):
            bar.addWidget(b)
        lay.addLayout(bar)

        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setEditTriggers(QtWidgets.QAbstractItemView.EditKeyPressed
                                  | QtWidgets.QAbstractItemView.SelectedClicked)
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.setToolTip("Флажок — показать на графике. Двойной щелчок — сделать текущим "
                             "(пики, сохранение). F2 или щелчок по выделенному — переименовать.")
        lay.addWidget(self.list, 3)

        bar2 = QtWidgets.QHBoxLayout()
        self.btn_all = QtWidgets.QToolButton()
        self.btn_all.setText("Все")
        self.btn_all.setToolTip("Показать все")
        self.btn_none = QtWidgets.QToolButton()
        self.btn_none.setText("Ни одного")
        self.btn_none.setToolTip("Скрыть все")
        self.btn_del = QtWidgets.QToolButton()
        self.btn_del.setText("Удалить")
        self.btn_del.setToolTip("Удалить выделенные из буфера (Delete)")
        self.btn_clear = QtWidgets.QToolButton()
        self.btn_clear.setText("Очистить")
        self.btn_clear.setToolTip("Удалить из буфера все спектры")
        for b in (self.btn_all, self.btn_none, self.btn_del, self.btn_clear):
            bar2.addWidget(b)
        bar2.addStretch(1)
        lay.addLayout(bar2)
        self.auto = QtWidgets.QCheckBox("класть в буфер каждый снимок")
        self.auto.setToolTip("Каждый «Снимок», серия и панорама попадают в буфер автоматически "
                             "(кадры непрерывной съёмки — нет)")
        lay.addWidget(self.auto)

        lay.addWidget(QtWidgets.QLabel("Свойства:"))
        self.props = QtWidgets.QTableWidget(0, 2)
        self.props.horizontalHeader().setVisible(False)
        self.props.verticalHeader().setVisible(False)
        self.props.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.props.horizontalHeader().setStretchLastSection(True)
        self.props.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.props, 2)

        self.btn_add.clicked.connect(self.add_current.emit)
        self.btn_load.clicked.connect(self.load_files.emit)
        self.btn_save.clicked.connect(lambda: self.save_entries.emit(self._targets()))
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_del.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self._ask_clear)
        self.list.itemChanged.connect(self._item_changed)
        self.list.itemDoubleClicked.connect(lambda it: self.make_current.emit(it.data(KEY_ROLE)))
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show_props(cur))
        self.list.customContextMenuRequested.connect(self._menu)
        QtWidgets.QShortcut(QtGui.QKeySequence.Delete, self.list, self.remove_selected,
                            context=QtCore.Qt.WidgetShortcut)
        self.refresh()

    # ---------------------------------------------------------------- отображение
    def refresh(self) -> None:
        cur = self.list.currentItem()
        cur_key = cur.data(KEY_ROLE) if cur is not None else None
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.buffer.entries:
            it = QtWidgets.QListWidgetItem(_swatch(entry_color(e)), e.name)
            it.setData(KEY_ROLE, e.key)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEditable)
            it.setCheckState(QtCore.Qt.Checked if e.visible else QtCore.Qt.Unchecked)
            self.list.addItem(it)
            if e.key == cur_key:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        self._show_props(self.list.currentItem())
        has = len(self.buffer) > 0
        for b in (self.btn_save, self.btn_all, self.btn_none, self.btn_del, self.btn_clear):
            b.setEnabled(has)

    def select(self, key: str) -> None:
        """Выделить спектр (и показать его свойства)."""
        for i in range(self.list.count()):
            if self.list.item(i).data(KEY_ROLE) == key:
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i))
                self._show_props(self.list.item(i))
                return

    def mark_unavailable(self, keys: set, why: str) -> None:
        """Серым — спектры, которые нельзя показать в текущих единицах оси."""
        for i in range(self.list.count()):
            it = self.list.item(i)
            off = it.data(KEY_ROLE) in keys
            it.setForeground(QtGui.QColor("#999" if off else "#000"))
            it.setToolTip(why if off else "")

    def _show_props(self, item: Optional[QtWidgets.QListWidgetItem]) -> None:
        rows = self.buffer.properties(item.data(KEY_ROLE)) if item is not None else []
        self.props.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.props.setItem(r, 0, QtWidgets.QTableWidgetItem(k))
            self.props.setItem(r, 1, QtWidgets.QTableWidgetItem(v))

    # ---------------------------------------------------------------- действия
    def _targets(self) -> List[str]:
        """Выделенные спектры, а если ничего не выделено — отмеченные флажком."""
        sel = [it.data(KEY_ROLE) for it in self.list.selectedItems()]
        return sel or [e.key for e in self.buffer.visible()]

    def _item_changed(self, item: QtWidgets.QListWidgetItem) -> None:
        key = item.data(KEY_ROLE)
        self.buffer.set_visible(key, item.checkState() == QtCore.Qt.Checked)
        e = self.buffer.get(key)
        if e is not None and item.text().strip() and item.text() != e.name:
            self.buffer.rename(key, item.text())
            self._show_props(item)
        self.changed.emit()

    def _set_all(self, on: Optional[bool]) -> None:
        self.buffer.set_all_visible(on)
        self.refresh()
        self.changed.emit()

    def remove_selected(self) -> None:
        keys = [it.data(KEY_ROLE) for it in self.list.selectedItems()]
        if keys:
            self.buffer.remove(keys)
            self.refresh()
            self.changed.emit()

    def _ask_clear(self) -> None:
        if not len(self.buffer):
            return
        r = QtWidgets.QMessageBox.question(self, "Буфер спектров",
                                           f"Удалить из буфера все спектры ({len(self.buffer)})?")
        if r == QtWidgets.QMessageBox.Yes:
            self.clear()

    def clear(self) -> None:
        self.buffer.clear()
        self.refresh()
        self.changed.emit()

    def _menu(self, pos: QtCore.QPoint) -> None:
        item = self.list.itemAt(pos)
        menu = QtWidgets.QMenu(self)
        if item is not None:
            key = item.data(KEY_ROLE)
            menu.addAction("Сделать текущим", lambda: self.make_current.emit(key))
            menu.addAction("Переименовать", lambda: self.list.editItem(item))
            menu.addAction("Сохранить…", lambda: self.save_entries.emit(self._targets()))
            menu.addAction("Удалить", self.remove_selected)
            menu.addSeparator()
        menu.addAction("Показать все", lambda: self._set_all(True))
        menu.addAction("Скрыть все", lambda: self._set_all(False))
        menu.addAction("Обратить отметки", lambda: self._set_all(None))
        menu.exec_(self.list.viewport().mapToGlobal(pos))
