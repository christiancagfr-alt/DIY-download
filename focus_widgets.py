"""Value controls only react to the mouse wheel after receiving focus."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QComboBox, QSpinBox, QListView, QStyledItemDelegate


class ComboItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(28, option.fontMetrics.height() + 10))


class FocusComboBox(QComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMaxVisibleItems(10)
        view = QListView(self)
        view.setUniformItemSizes(True)
        view.setWordWrap(False)
        view.setTextElideMode(Qt.TextElideMode.ElideRight)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setItemDelegate(ComboItemDelegate(view))
        self.setView(view)
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")

    def showPopup(self):
        # Use a scrollable list instead of Cocoa's tall menu with scroll arrows.
        row_height = max(28, self.fontMetrics().height() + 10)
        height = max(1, min(self.count(), self.maxVisibleItems())) * row_height + 4
        self.view().setMinimumHeight(0)
        self.view().setMaximumHeight(height)
        super().showPopup()

    def setEditable(self, editable):
        super().setEditable(editable)
        # Qt resets this to WheelFocus when editability changes.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus() or (self.isEditable() and self.lineEdit().hasFocus()):
            super().wheelEvent(event)
        else:
            event.ignore()  # Let the enclosing scroll area scroll the page.


class FocusSpinBox(QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus() or self.lineEdit().hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()
