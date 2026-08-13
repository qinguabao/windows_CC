#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""管理员提权工具：检测并可选地以管理员权限重启自身。"""

import sys
import ctypes


def is_admin():
    """当前进程是否具有管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员权限重新启动当前程序。

    返回 True 表示当前进程应继续运行（已是管理员或用户拒绝/提权失败）；
    返回 False 表示已发起提权重启，当前进程应退出。
    """
    if is_admin():
        return True
    try:
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包后的 exe
            exe = sys.executable
            params = ""
        else:
            exe = sys.executable
            params = " ".join('"%s"' % a for a in sys.argv)
        # "runas" 触发 UAC 弹窗
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return False
    except Exception:
        # 提权失败则继续以普通权限运行（部分清理项会受限）
        return True
