# Parsec Cloud (https://parsec.cloud) Copyright (c) AGPLv3 2019 Scille SAS

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog

from parsec.core.gui.ui.loading_dialog import Ui_LoadingDialog


class LoadingDialog(QDialog, Ui_LoadingDialog):
    def __init__(self, total_size, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setupUi(self)
        self.setWindowFlags(Qt.SplashScreen)
        self.setModal(True)
        self.progress_bar.setMaximum(total_size)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setValue(0)

    def set_current_file(self, f):
        if len(f) > 35:
            self.label.setText('"' + f[:26] + "..." + f[-6:] + '"')
        else:
            self.label.setText('"' + f + '"')

    def set_progress(self, size):
        self.progress_bar.setValue(size)
