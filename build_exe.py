"""将统一后的 PySide6 应用打包为单文件 EXE。"""

import os
import subprocess
import sys

def build_exe():
    """使用PyInstaller打包应用为EXE文件"""
    print("开始打包C盘清理工具为EXE文件...")
    
    data_separator = ';' if sys.platform.startswith('win') else ':'
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--name=C盘清理工具',
        '--onefile',
        '--windowed',
        '--uac-admin',
        '--icon=icons/cleaner.ico',
        f'--add-data=icons{data_separator}icons',
        '--noconfirm',
        '--clean',
        'app_modern.py',
    ]
    # 执行打包命令
    rc = subprocess.call(cmd)
    if rc:
        raise SystemExit(rc)
    print("打包完成：", os.path.abspath('dist/C盘清理工具.exe'))

if __name__ == '__main__':
    build_exe()
