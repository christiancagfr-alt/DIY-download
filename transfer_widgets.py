"""Shared compact transfer UI components."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QListWidget, QListWidgetItem, QMenu,
                               QVBoxLayout, QWidget, QToolButton, QHBoxLayout, QLabel)


class LinkList(QListWidget):
    """Display elided URLs while keeping the original addresses in item data."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setMinimumHeight(150)

    def setPlaceholderText(self, text):
        self.setToolTip('粘贴链接：Ctrl+V / ⌘V；每行一条，悬停显示完整地址')

    def insertFromMimeData(self, mime):
        from paste_link_download_page import extract_pasted_urls
        for url in extract_pasted_urls(mime.text(), mime.html()):
            item = QListWidgetItem(url)
            item.setData(Qt.ItemDataRole.UserRole, url)
            item.setToolTip(url)
            self.addItem(item)

    def paste(self):
        self.insertFromMimeData(QApplication.clipboard().mimeData())

    def toPlainText(self):
        return '\n'.join(self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count()))

    def toHtml(self):
        return ''

    def setPlainText(self, text):
        from PySide6.QtCore import QMimeData
        self.clear()
        mime = QMimeData(); mime.setText(text)
        self.insertFromMimeData(mime)

    def _copy(self):
        QApplication.clipboard().setText('\n'.join(i.data(Qt.ItemDataRole.UserRole) for i in self.selectedItems()))

    def _delete(self):
        for item in self.selectedItems():
            self.takeItem(self.row(item))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
        elif event.matches(QKeySequence.StandardKey.Copy):
            self._copy()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete()
        else:
            super().keyPressEvent(event)

    def _menu(self, pos):
        menu = QMenu(self)
        menu.addAction('粘贴链接', self.paste)
        menu.addAction('复制完整链接', self._copy)
        menu.addAction('删除所选', self._delete)
        menu.exec(self.viewport().mapToGlobal(pos))


def drawer(title, content, parent_layout, height=120):
    button = QToolButton()
    button.setObjectName("collapseBtn")
    button.setText('▸  ' + title)
    button.setCheckable(True)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    content.setMaximumHeight(height)
    content.hide()
    parent_layout.addWidget(button)
    parent_layout.addWidget(content)
    def toggle(opened):
        content.setVisible(opened)
        button.setText(('▾  ' if opened else '▸  ') + title)
    button.toggled.connect(toggle)
    return button


def compact_field(label, control):
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    caption = QLabel(label)
    caption.setMinimumWidth(60)
    control.setMinimumWidth(0)
    layout.addWidget(caption)
    layout.addWidget(control, 1)
    return box


from PySide6.QtWidgets import QTableWidget


class SafeEditTable(QTableWidget):
    """Only explicit editing creates an editor; IME commits belong to that editor.

    macOS Cocoa can resend a commit while QAbstractItemView starts editing and
    transfers focus, recursively reentering inputMethodEvent. Never start editing
    from the table's IME handler. Existing QLineEdit editors retain normal IME.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked |
                             QTableWidget.EditTrigger.EditKeyPressed)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)

    def inputMethodEvent(self, event):
        event.ignore()
