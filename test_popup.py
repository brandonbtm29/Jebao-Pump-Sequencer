import sys
from PyQt6.QtWidgets import QApplication, QComboBox, QWidget, QVBoxLayout, QAbstractItemView
from PyQt6.QtGui import QStandardItemModel, QStandardItem
from PyQt6.QtCore import Qt, QEvent

class CheckableComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.view().setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.model().dataChanged.connect(self.updateText)
        
        self._display_item = QStandardItem("1")
        self.model().appendRow(self._display_item)
        self.view().setRowHidden(0, True)
        
        self.view().viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.view().viewport():
            if event.type() == QEvent.Type.MouseButtonRelease:
                index = self.view().indexAt(event.pos())
                if index.isValid() and index.row() > 0:
                    item = self.model().itemFromIndex(index)
                    if item.checkState() == Qt.CheckState.Checked:
                        item.setCheckState(Qt.CheckState.Unchecked)
                    else:
                        item.setCheckState(Qt.CheckState.Checked)
                return True
        return super().eventFilter(obj, event)

    def addItem(self, text, userData=None):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)

    def checkedItems(self):
        checked = []
        for i in range(1, self.model().rowCount()):
            item = self.model().item(i)
            if item.checkState() == Qt.CheckState.Checked:
                checked.append(item.text())
        return checked

    def updateText(self):
        checked = self.checkedItems()
        if checked:
            self._display_item.setText(",".join(checked))
        else:
            self._display_item.setText("None")
        self.setCurrentIndex(0)

def test_script():
    app = QApplication(sys.argv)
    w = QWidget()
    l = QVBoxLayout(w)
    cb = CheckableComboBox()
    cb.addItem("1")
    cb.addItem("2")
    cb.addItem("3")
    cb.addItem("4")
    l.addWidget(cb)
    w.show()
    
    # We can't really simulate a mouse click easily in a headless test without QTest
    # but I can just compile it to make sure there are no syntax errors
    print("Syntax OK")

if __name__ == "__main__":
    test_script()
