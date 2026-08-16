#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动更新模块：检查 GitHub Releases、下载新版（含 SHA-256 校验）、原地替换重启。

替换采用 Windows 通用的 rename-swap 方案（Squirrel/Electron 更新器同款思路）：
正在运行的 EXE 可以被重命名但不能被删除，因此
    1. 旧 EXE 改名为 <name>.exe.old
    2. 新 EXE 移动到原位置
    3. 启动新 EXE，当前进程退出
    4. 新版本下次启动时删除 .old 遗留文件
整个过程在 Python 内完成，不再依赖 cmd/bat 脚本（历史版本曾因 bat 中的
重定向语法问题静默失败，且无法向用户反馈错误）。
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from version import APP_VERSION, GITHUB_REPO

logger = logging.getLogger('CCleaner')

GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_DIR = os.path.join(tempfile.gettempdir(), 'CCleaner_update')


class UpdateError(Exception):
    """更新流程中的可预期错误。"""


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
    """判断远程版本是否比本地新。两侧都容忍 'v' 前缀。"""
    try:
        remote = _version_tuple(_parse_version(remote_tag))
        local = _version_tuple(_parse_version(local_version))
        return remote > local
    except Exception:
        return False


def check_update() -> dict | None:
    """检查是否有新版本。

    Returns:
        dict with keys: version, tag, download_url, sha256, changelog, published_at
        or None if no update available.

    Raises:
        UpdateError: 网络请求失败（与"无更新"区分开，便于 UI 分别提示）。
    """
    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': f'CCleaner-Pro/{APP_VERSION}',
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        logger.debug(f'检查更新失败: {e}')
        raise UpdateError(f'无法访问 GitHub API: {e}') from e

    tag = data.get('tag_name', '')
    if not tag or not _is_newer(tag, APP_VERSION):
        return None

    # 查找 .exe 资产
    download_url = None
    sha256 = None
    for asset in data.get('assets', []):
        name = asset.get('name', '').lower()
        if name.endswith('.exe'):
            download_url = asset.get('browser_download_url')
            # GitHub API 的 digest 形如 "sha256:abcdef..."
            digest = asset.get('digest') or ''
            if digest.lower().startswith('sha256:'):
                sha256 = digest.split(':', 1)[1].strip().lower()
            break

    if not download_url:
        logger.debug('新版本无 EXE 资产')
        return None

    return {
        'version': _parse_version(tag),
        'tag': tag,
        'download_url': download_url,
        'sha256': sha256,
        'changelog': data.get('body', ''),
        'published_at': data.get('published_at', ''),
    }


def download_update(url: str, progress_callback=None, expected_sha256: str = None) -> str:
    """下载更新文件，可选做 SHA-256 校验。

    Args:
        url: 下载地址
        progress_callback: callback(downloaded_bytes, total_bytes)
        expected_sha256: 期望的 SHA-256（来自 GitHub API digest），提供则校验

    Returns:
        下载后的本地文件路径

    Raises:
        UpdateError: 下载内容校验失败
    """
    os.makedirs(UPDATE_DIR, exist_ok=True)
    filename = url.rsplit('/', 1)[-1] or 'update.exe'
    dest = os.path.join(UPDATE_DIR, filename)

    req = urllib.request.Request(
        url,
        headers={'User-Agent': f'CCleaner-Pro/{APP_VERSION}'},
    )
    hasher = hashlib.sha256()
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
                hasher.update(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total)

    if expected_sha256:
        actual = hasher.hexdigest()
        if actual != expected_sha256.lower():
            try:
                os.remove(dest)
            except OSError:
                pass
            raise UpdateError(
                f'下载文件校验失败（SHA-256 不匹配），已删除。\n'
                f'期望: {expected_sha256}\n实际: {actual}')

    return dest


def _force_delete(path: str, retries: int = 3, delay: float = 0.5):
    """删除文件，遇到占用（多为杀毒软件短暂锁定）时重试。"""
    for i in range(retries):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(delay)


def _swap_files(new_exe_path: str, current_exe: str):
    """把新 EXE 换到当前 EXE 的位置（rename-swap）。

    旧文件改名为 <current>.old；新文件移动到 current 位置。
    任一步失败都会尝试回滚，保证当前 EXE 仍可运行。
    """
    backup_path = current_exe + '.old'
    if os.path.exists(backup_path):
        # 上次更新成功后进程退出、但新版本还没来得及清理的遗留
        try:
            _force_delete(backup_path)
        except OSError as e:
            raise UpdateError(f'无法清理旧备份文件 {backup_path}: {e}')

    try:
        # Windows 允许重命名正在运行的 EXE（不允许直接删除）
        os.replace(current_exe, backup_path)
    except OSError as e:
        raise UpdateError(f'无法重命名当前程序（可能被杀毒软件锁定）: {e}')

    try:
        # os.replace 支持跨盘移动并覆盖目标
        os.replace(new_exe_path, current_exe)
    except OSError as e:
        # 回滚：把旧文件放回去，保持程序可用
        try:
            os.replace(backup_path, current_exe)
        except OSError:
            logger.error(f'回滚失败！请手动将 {backup_path} 改回 {current_exe}')
        raise UpdateError(f'无法放置新版程序: {e}')


def apply_update(new_exe_path: str) -> bool:
    """替换当前 EXE 并重启应用（rename-swap 方案，无需 bat 脚本）。

    成功路径：换文件 → 启动新版本 → os._exit(0) 结束当前进程。
    失败路径：回滚并返回 False，由调用方（UI）向用户展示错误。

    Returns:
        True 表示已启动新版本、当前进程即将退出；
        False 表示失败（开发模式或文件操作失败），当前进程继续运行。
    """
    if not getattr(sys, 'frozen', False):
        logger.info('开发模式下不执行自动替换')
        return False

    current_exe = sys.executable

    if not os.path.isfile(new_exe_path):
        logger.error(f'更新文件不存在: {new_exe_path}')
        return False

    try:
        _swap_files(new_exe_path, current_exe)
    except UpdateError as e:
        logger.error(f'应用更新失败: {e}')
        return False

    # 启动新版本。当前进程已提权，子进程继承权限，不会再弹 UAC。
    try:
        subprocess.Popen(
            [current_exe],
            close_fds=True,
            cwd=os.path.dirname(current_exe),
        )
    except OSError as e:
        # 新 EXE 启动失败：此时 current 位置是新版本、.old 是旧版本。
        # 回滚 = 删掉新版、把旧版改回原名，避免用户失去可用程序。
        logger.error(f'启动新版本失败: {e}，尝试回滚')
        backup_path = current_exe + '.old'
        try:
            _force_delete(current_exe)
            os.replace(backup_path, current_exe)
        except OSError as rollback_err:
            logger.error(f'回滚失败: {rollback_err}')
        return False

    time.sleep(0.5)
    os._exit(0)
    return True  # os._exit 不返回；此行保证（_exit 被打桩的）测试与类型签名正确


def cleanup_after_update() -> bool:
    """新版启动时调用：删除上次更新遗留的 .old 文件。

    Returns:
        True 表示删除了遗留文件（说明刚完成一次自动更新）。
    """
    if not getattr(sys, 'frozen', False):
        return False
    backup_path = sys.executable + '.old'
    if os.path.exists(backup_path):
        try:
            _force_delete(backup_path)
            logger.info(f'已清理更新遗留文件: {backup_path}')
            return True
        except OSError as e:
            logger.warning(f'清理更新遗留文件失败: {e}')
    return False


def get_current_exe_path() -> str:
    """获取当前运行的 EXE 路径（仅打包后有效）。"""
    if getattr(sys, 'frozen', False):
        return sys.executable
    return ''
