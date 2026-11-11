"""Editable upload fields; stable source keys are separate from user-facing headers."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
                              QTableWidgetItem, QComboBox, QPushButton, QHeaderView, QInputDialog)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMenu
from transfer_widgets import SafeEditTable as QTableWidget
from focus_widgets import FocusComboBox as QComboBox, FocusSpinBox as QSpinBox
from sheets_batch_downloader import column_to_number, number_to_column

SOURCES = {
    'date': '日期', 'cat1': '一级分类', 'cat2': '二级分类', 'cat3': '三级分类',
    'type': '类型', 'clean_meta': '清理元数据', 'reviewer': '审核人员',
    'uploader': '上传者', 'copyright': '版权', 'file_url': '文件链接',
    'status': '状态', 'done_date': '完成日期', 'file_name': '原文件名',
    'folder_url': '云端文件夹链接', 'custom': '自定义值', 'unassigned': '暂不回填',
}
DEFAULT_COLUMNS = dict(zip(list(SOURCES)[:12], 'ABCDEFGHIJKL'))


def default_mappings(legacy=None):
    cols = dict(DEFAULT_COLUMNS)
    cols.update(legacy or {})
    return [dict(header=SOURCES[key], column=col, source=key, value='')
            for key, col in cols.items() if key in SOURCES]


# Explicit names only: unknown business fields are never guessed by position.
HEADER_SOURCES = {label: key for key, label in SOURCES.items() if key not in ('custom', 'unassigned')}
HEADER_SOURCES.update({'一级': 'cat1', '二级': 'cat2', '三级': 'cat3',
                       '上传日期': 'date', '上传时间': 'date', '文件地址': 'file_url',
                       '素材地址': 'file_url', '上传人员': 'uploader', '审核人': 'reviewer',
                       '完成日': 'done_date', '是否清理元数据': 'clean_meta',
                       '文件名': 'file_name', '文件夹链接': 'folder_url'})


def mappings_from_headers(headers, previous=()):
    by_name = {}
    for row in previous:
        by_name.setdefault(str(row.get('header', '')).strip(), []).append(row)
    result = []
    for i, header in enumerate(headers, 1):
        header = str(header or '').strip()
        if not header:
            continue
        matches = by_name.get(header, [])
        prior = matches[0] if len(matches) == 1 else {}
        result.append(dict(header=header, column=number_to_column(i),
                           source=prior.get('source', HEADER_SOURCES.get(header, 'unassigned')),
                           value=prior.get('value', '')))
    return result


def validate_mappings(rows):
    result, seen = [], set()
    for i, row in enumerate(rows, 1):
        col = str(row.get('column', '')).strip().upper()
        if not col:  # Leaving a target empty disables this field.
            continue
        n = column_to_number(col)
        if not 1 <= n <= 18278:
            raise ValueError(f'第 {i} 项：列必须在 A–ZZZ 之间')
        col = number_to_column(n)
        if col in seen:
            raise ValueError(f'目标列 {col} 重复，请为每个字段指定不同列')
        seen.add(col)
        source = row.get('source', 'custom')
        if source not in SOURCES:
            raise ValueError(f'第 {i} 项：未知数据来源')
        header = str(row.get('header', '')).strip()
        if not header:
            raise ValueError(f'第 {i} 项：请填写表头名称')
        result.append(dict(header=header, column=col, source=source,
                           value=str(row.get('value', ''))))
    return result


def mapped_cells(mappings, values):
    return {m['column']: m['value'] if m['source'] == 'custom'
            else values.get(m['source'], '') for m in validate_mappings(mappings) if m['source'] != 'unassigned'}


class MappingEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['表头名称', '目标列', '数据来源', '自定义值'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, width in enumerate((130, 65, 160, 140)):
            self.table.setColumnWidth(i, width)
        self.table.setColumnHidden(2, True)
        self.table.setColumnHidden(3, True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(250)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton('添加字段')
        remove = QPushButton('删除所选字段')
        add.clicked.connect(self.choose_field)
        remove.clicked.connect(self.remove_selected)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.field_menu)
        self.table.itemChanged.connect(self.header_edited)
        self.set_rows([])

    def choose_field(self):
        keys = [key for key in SOURCES if key not in ('custom', 'unassigned')]
        label, ok = QInputDialog.getItem(self, '添加回填字段', '选择需要自动回填的字段',
                                         [SOURCES[key] for key in keys], 0, False)
        if ok:
            key = next(key for key in keys if SOURCES[key] == label)
            self.add_row(dict(header=label, column='', source=key, value=''))

    def add_row(self, row=None):
        row = row or dict(header='新字段', column='', source='custom', value='')
        i = self.table.rowCount()
        self.table.insertRow(i)
        for col, key in ((0, 'header'), (1, 'column'), (3, 'value')):
            self.table.setItem(i, col, QTableWidgetItem(str(row.get(key, ''))))
        combo = QComboBox()
        for key, label in SOURCES.items():
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(row.get('source', 'custom'))))
        self.table.setCellWidget(i, 2, combo)
        self.update_hint(i)

    def update_hint(self, row):
        combo = self.table.cellWidget(row, 2)
        if combo is not None:
            source = combo.currentData()
            hint = ("暂不回填；可右键指定回填内容" if source == 'unassigned'
                    else "自动回填：" + SOURCES[source] + "；以目标列为准")
            self.table.item(row, 0).setToolTip(hint)

    def header_edited(self, item):
        if item.column() != 0:
            return
        combo = self.table.cellWidget(item.row(), 2)
        if combo and combo.currentData() == 'unassigned':
            source = HEADER_SOURCES.get(item.text().strip())
            if source:
                combo.setCurrentIndex(combo.findData(source))
        self.update_hint(item.row())

    def field_menu(self, position):
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        action = menu.addAction('指定此字段回填内容')
        if menu.exec(self.table.viewport().mapToGlobal(position)) == action:
            keys = [key for key in SOURCES if key != 'custom']
            label, ok = QInputDialog.getItem(self, '回填内容', '选择程序自动填写的内容',
                                             [SOURCES[key] for key in keys], 0, False)
            if ok:
                key = next(k for k in keys if SOURCES[k] == label)
                combo = self.table.cellWidget(row, 2)
                combo.setCurrentIndex(combo.findData(key))
                self.update_hint(row)

    def remove_selected(self):
        for i in sorted({x.row() for x in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(i)

    def set_rows(self, rows):
        self.table.setRowCount(0)
        for row in rows:
            self.add_row(row)

    def rows(self):
        return [dict(header=self.table.item(i, 0).text(),
                     column=self.table.item(i, 1).text(),
                     source=self.table.cellWidget(i, 2).currentData(),
                     value=self.table.item(i, 3).text()) for i in range(self.table.rowCount())]
