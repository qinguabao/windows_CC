#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 PyInstaller 将现代版打包为单文件 EXE（无窗口、管理员提权、带图标、带版本信息）。
需在安装了 PySide6 + pyinstaller 的 Python 环境下运行本脚本。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# 从 version.py 读取版本号
sys.path.insert(0, HERE)
from version import APP_VERSION

APP_NAME = "C盘清理工具Pro"


def _generate_version_file():
    """生成 PyInstaller 所需的 Windows 版本信息文件。"""
    parts = APP_VERSION.split('.')
    while len(parts) < 4:
        parts.append('0')
    ver_tuple = ', '.join(parts[:4])
    ver_str = APP_VERSION

    content = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({ver_tuple}),
    prodvers=({ver_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'080404b0',
        [
          StringStruct(u'CompanyName', u''),
          StringStruct(u'FileDescription', u'C盘清理工具 Pro'),
          StringStruct(u'FileVersion', u'{ver_str}'),
          StringStruct(u'InternalName', u'CCleaner_Pro'),
          StringStruct(u'OriginalFilename', u'{APP_NAME}.exe'),
          StringStruct(u'ProductName', u'C盘清理工具 Pro'),
          StringStruct(u'ProductVersion', u'{ver_str}'),
        ])
      ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
'''
    path = os.path.join(HERE, 'file_version_info.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


def main():
    version_file = _generate_version_file()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--uac-admin",
        "--name", APP_NAME,
        "--icon", os.path.join("icons", "cleaner.ico"),
        "--add-data", "icons;icons",
        "--version-file", version_file,
        "app_modern.py",
    ]

    print(f"打包版本: v{APP_VERSION}")
    print("执行打包命令：")
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
        print(f"\n打包完成 (v{APP_VERSION})：", exe)
        print("存在：", os.path.exists(exe))
    else:
        print("\n打包失败，返回码：", rc)

    # 清理临时版本文件
    try:
        os.remove(version_file)
    except OSError:
        pass

    sys.exit(rc)


if __name__ == '__main__':
    main()
