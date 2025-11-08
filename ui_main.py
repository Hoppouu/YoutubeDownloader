# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'main_guiAdCPHR.ui'
##
## Created by: Qt User Interface Compiler version 6.8.2
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QAction, QBrush, QColor, QConicalGradient,
    QCursor, QFont, QFontDatabase, QGradient,
    QIcon, QImage, QKeySequence, QLinearGradient,
    QPainter, QPalette, QPixmap, QRadialGradient,
    QTransform)
from PySide6.QtWidgets import (QApplication, QGridLayout, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMenuBar,
    QProgressBar, QPushButton, QSizePolicy, QStatusBar,
    QWidget)

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        if not MainWindow.objectName():
            MainWindow.setObjectName(u"MainWindow")
        MainWindow.setEnabled(True)
        MainWindow.resize(780, 420)
        MainWindow.setMinimumSize(QSize(780, 420))
        MainWindow.setMaximumSize(QSize(780, 420))
        
        font = QFont()
        font.setFamilies([u"\ub9d1\uc740 \uace0\ub515"])
        font.setPointSize(12)
        font.setBold(False)
        MainWindow.setFont(font)
        self.action_2 = QAction(MainWindow)
        self.action_2.setObjectName(u"action_2")
        font1 = QFont()
        font1.setFamilies([u"\ub9d1\uc740 \uace0\ub515"])
        self.action_2.setFont(font1)
        self.action_3 = QAction(MainWindow)
        self.action_3.setObjectName(u"action_3")
        self.action_3.setCheckable(True)
        self.action_3.setChecked(False)
        self.action_3.setFont(font1)
        self.action_4 = QAction(MainWindow)
        self.action_4.setObjectName(u"action_4")
        self.action_4.setCheckable(True)
        self.action_4.setChecked(False)
        self.action_4.setFont(font1)
        self.centralwidget = QWidget(MainWindow)
        self.centralwidget.setObjectName(u"centralwidget")
        self.gridLayout = QGridLayout(self.centralwidget)
        self.gridLayout.setObjectName(u"gridLayout")
        self.lineEdit = QLineEdit(self.centralwidget)
        self.lineEdit.setObjectName(u"lineEdit")
        self.lineEdit.setMaxLength(85)

        self.gridLayout.addWidget(self.lineEdit, 1, 0, 1, 1)

        self.progressBar = QProgressBar(self.centralwidget)
        self.progressBar.setObjectName(u"progressBar")
        self.progressBar.setValue(0)

        self.gridLayout.addWidget(self.progressBar, 0, 0, 1, 2)

        self.listWidget = QListWidget(self.centralwidget)
        self.listWidget.setObjectName(u"listWidget")
        font2 = QFont()
        font2.setPointSize(12)
        self.listWidget.setFont(font2)
        self.listWidget.setIconSize(QSize(150, 150))
        self.listWidget.setSelectionMode(QListWidget.NoSelection)

        self.gridLayout.addWidget(self.listWidget, 2, 0, 1, 2)

        self.pushButton = QPushButton(self.centralwidget)
        self.pushButton.setObjectName(u"pushButton")
        font3 = QFont()
        font3.setBold(False)
        self.pushButton.setFont(font3)

        self.gridLayout.addWidget(self.pushButton, 1, 1, 1, 1)

        MainWindow.setCentralWidget(self.centralwidget)
        self.statusbar = QStatusBar(MainWindow)
        self.statusbar.setObjectName(u"statusbar")
        MainWindow.setStatusBar(self.statusbar)
        self.menuBar = QMenuBar(MainWindow)
        self.menuBar.setObjectName(u"menuBar")
        self.menuBar.setGeometry(QRect(0, 0, 780, 21))
        self.menu = QMenu(self.menuBar)
        self.menu.setObjectName(u"menu")
        MainWindow.setMenuBar(self.menuBar)

        self.menuBar.addAction(self.menu.menuAction())
        self.menu.addAction(self.action_2)
        self.menu.addAction(self.action_3)
        self.menu.addAction(self.action_4)

        self.retranslateUi(MainWindow)
        self.action_2.triggered.connect(MainWindow.set_directory)
        self.pushButton.clicked.connect(MainWindow.downloadEvent)
        self.action_3.changed.connect(MainWindow.run_ClipboardThread)
        self.action_4.changed.connect(MainWindow.setAutoDownload)

        QMetaObject.connectSlotsByName(MainWindow)
    # setupUi

    def retranslateUi(self, MainWindow):
        MainWindow.setWindowTitle(QCoreApplication.translate("MainWindow", u"Youtube Downloader", None))
        self.action_2.setText(QCoreApplication.translate("MainWindow", u"\ud30c\uc77c\uacbd\ub85c", None))
        self.action_3.setText(QCoreApplication.translate("MainWindow", u"\uc790\ub3d9 \ubd99\uc5ec\ub123\uae30", None))
        self.action_4.setText("자동 다운로드")
        self.lineEdit.setText("")
        self.pushButton.setText(QCoreApplication.translate("MainWindow", u"\ub2e4\uc6b4\ub85c\ub4dc", None))
        self.menu.setTitle(QCoreApplication.translate("MainWindow", u"\uc124\uc815", None))
    # retranslateUi

