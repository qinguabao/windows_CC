#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成复选框对号/半选横杠白色图标（透明底 PNG），供 QSS 叠在蓝色 indicator 背景上。"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QPixmap, QPainter, QPen, QColor
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'icons')
os.makedirs(OUT, exist_ok=True)

SIZE = 64


def _new_pixmap():
    pm = QPixmap(SIZE, SIZE)
    pm.fill(Qt.transparent)
    return pm


def _pen():
    p = QPen(QColor('#ffffff'))
    p.setWidthF(8.0)
    p.setCapStyle(Qt.RoundCap)
    p.setJoinStyle(Qt.RoundJoin)
    return p


def make_check():
    """白色对号 ✓"""
    pm = _new_pixmap()
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.Antialiasing)
    pt.setPen(_pen())
    pt.drawLine(15, 34, 27, 47)
    pt.drawLine(27, 47, 50, 19)
    pt.end()
    path = os.path.join(OUT, 'check_white.png')
    pm.save(path, 'PNG')
    print('saved', path)


def make_dash():
    """白色横杠 −（半选状态）"""
    pm = _new_pixmap()
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.Antialiasing)
    pt.setPen(_pen())
    pt.drawLine(16, 32, 48, 32)
    pt.end()
    path = os.path.join(OUT, 'dash_white.png')
    pm.save(path, 'PNG')
    print('saved', path)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    make_check()
    make_dash()
    print('done')
