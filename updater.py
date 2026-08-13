#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动更新模块：检查 GitHub Releases、下载新版、替换重启。"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from version import APP_VERSION, GITHUB_REPO

logger = logging.getLogger('CCleaner')

GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_DIR = os.path.join(tempfile.gettempdir(), 'CCleaner_update')


def _parse_version(tag: str) -> str:
    """从 tag 中提取版本号，去掉 'v' 前缀。"""
    return tag.lstrip('vV').strip()


def _version_tuple(ver: str) -> tuple:
    """将版本字符串转为可比较的元组。"""
    parts = []
    for p in ver.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_newer(remote_tag: str, local_version: str) -> bool:
    """判断远程版本是否比本地新。"""
    try:
        remote = _version_tuple(_parse_version(remote_tag))
        local = _version_tuple(local_version)
        return remote > local
    except Exception:
        return False


def check_update() -> dict | None:
    """检查是否有新版本。

    Returns:
        dict with keys: version, download_url, changelog, published_at
        or None if no update or error.
    """
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': f'CCleaner-Pro/{APP_VERSION}',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        logger.debug(f'检查更新失败: {e}')
        return None

    tag = data.get('tag_name', '')
    if not tag or not _is_newer(tag, APP_VERSION):
        return None

    # 查找 .exe 资产
    download_url = None
    for asset in data.get('assets', []):
        name = asset.get('name', '').lower()
        if name.endswith('.exe'):
            download_url = asset.get('browser_download_url')
            break

    if not download_url:
        logger.debug('新版本无 EXE 资产')
        return None

    return {
        'version': _parse_version(tag),
        'tag': tag,
        'download_url': download_url,
        'changelog': data.get('body', ''),
        'published_at': data.get('published_at', ''),
    }


def download_update(url: str, progress_callback=None) -> str:
    """下载更新文件。

    Args:
        url: 下载地址
        progress_callback: callback(downloaded_bytes, total_bytes)

    Returns:
        下载后的本地文件路径
    """
    os.makedirs(UPDATE_DIR, exist_ok=True)
    filename = url.rsplit('/', 1)[-1] or 'update.exe'
    dest = os.path.join(UPDATE_DIR, filename)

    req = urllib.request.Request(
        url,
        headers={'User-Agent': f'CCleaner-Pro/{APP_VERSION}'},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get('Content-Length', 0))
        downloaded = 0
        block_size = 64 * 1024

        with open(dest, 'wb') as f:
            while True:
                chunk = resp.read(block_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)

    return dest


def apply_update(new_exe_path: str):
    """替换当前 EXE 并重启应用。

    生成一个 bat 脚本：等待当前进程退出后替换文件并重启。
    """
    if getattr(sys, 'frozen', False):
        current_exe = sys.executable
    else:
        # 开发模式下不执行实际替换
        logger.info('开发模式下不执行自动替换')
        return

    bat_path = os.path.join(UPDATE_DIR, '_updater.bat')
    bat_content = f'''@echo off
timeout /t 2 /nobreak >nul
del /f /q "{current_exe}"
move /y "{new_exe_path}" "{current_exe}"
start "" "{current_exe}"
del /f /q "%~f0"
'''

    with open(bat_path, 'w', encoding='gbk') as f:
        f.write(bat_content)

    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ['cmd', '/c', bat_path],
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )
    sys.exit(0)


def get_current_exe_path() -> str:
    """获取当前运行的 EXE 路径（仅打包后有效）。"""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return ''
