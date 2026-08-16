#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成一个简单的应用图标 cleaner.ico（用 PySide6 渲染，offscreen 模式）。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
from PySide6.QtWidgets import QApplication

_app = QApplication([])  # 初始化 Qt GUI 子系统（offscreen）

HERE = os.path.dirname(os.path.abspath(__file__))
ico_dir = os.path.join(HERE, "icons")
os.makedirs(ico_dir, exist_ok=True)
ico_path = os.path.join(ico_dir, "cleaner.ico")

size = 256
pm = QPixmap(size, size)
pm.fill(QColor(0, 0, 0, 0))  # 透明背景
p = QPainter(pm)
p.setRenderHint(QPainter.Antialiasing)

# 圆角矩形底（品牌蓝）
p.setBrush(QBrush(QColor("#4f7cff")))
p.setPen(Qt.NoPen)
p.drawRoundedRect(16, 16, size - 32, size - 32, 48, 48)

# 白色圆环
p.setPen(QPen(QColor("#ffffff"), 16))
p.setBrush(Qt.NoBrush)
p.drawEllipse(70, 70, 116, 116)

# 勾
p.setPen(QPen(QColor("#ffffff"), 18))
p.drawLine(96, 132, 120, 158)
p.drawLine(120, 158, 168, 100)

p.end()
ok = pm.save(ico_path, "ICO")
print("图标生成:", ico_path, "成功" if ok else "失败")
