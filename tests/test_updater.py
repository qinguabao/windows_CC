# -*- coding: utf-8 -*-
"""自动更新模块的回归测试。

历史背景：旧版 apply_update 依赖 cmd/bat 脚本替换 EXE，曾因 bat 中混入
Unix 重定向语法（>/dev/null）静默失败且无任何用户反馈。现改为纯 Python
的 rename-swap 方案，本文件覆盖版本比较、更新检查、下载校验与文件交换。
"""

import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import updater


class FakeResponse:
    """模拟 urlopen 返回的响应对象。"""

    def __init__(self, payload: bytes, headers=None):
        self._stream = io.BytesIO(payload)
        self.headers = headers or {}

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class VersionCompareTests(unittest.TestCase):
    def test_parse_version_strips_v_prefix(self):
        self.assertEqual(updater._parse_version('v1.2.3'), '1.2.3')
        self.assertEqual(updater._parse_version('V2.0.0'), '2.0.0')
        self.assertEqual(updater._parse_version('1.1.0 '), '1.1.0')

    def test_numeric_not_decimal_compare(self):
        # 1.10.0 必须大于 1.9.0（字符串比较会判错）
        self.assertTrue(updater._is_newer('v1.10.0', '1.9.0'))
        self.assertFalse(updater._is_newer('v1.9.0', '1.10.0'))

    def test_same_version_is_not_newer(self):
        self.assertFalse(updater._is_newer('v1.2.0', '1.2.0'))
        self.assertFalse(updater._is_newer('1.2.0', 'v1.2.0'))

    def test_major_minor_patch_ordering(self):
        self.assertTrue(updater._is_newer('v1.2.1', '1.2.0'))
        self.assertTrue(updater._is_newer('v2.0.0', '1.9.9'))
        self.assertFalse(updater._is_newer('v1.2.0', '1.3.0'))

    def test_garbage_version_tolerated(self):
        self.assertFalse(updater._is_newer('garbage', '1.0.0'))
        # 垃圾段按 0 处理，不抛异常
        self.assertEqual(updater._version_tuple('1.x.2'), (1, 0, 2))


class CheckUpdateTests(unittest.TestCase):
    def _api_payload(self, tag='v1.9.9', assets=None):
        return json.dumps({
            'tag_name': tag,
            'body': 'changelog',
            'published_at': '2026-01-01T00:00:00Z',
            'assets': assets if assets is not None else [{
                'name': 'CCleaner-Pro-v1.9.9.exe',
                'browser_download_url': 'https://example.com/dl.exe',
                'digest': 'sha256:' + 'a' * 64,
            }],
        }).encode('utf-8')

    @patch.object(updater, 'APP_VERSION', '1.2.0')
    @patch('urllib.request.urlopen')
    def test_update_available_with_digest(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(self._api_payload())
        info = updater.check_update()
        self.assertIsNotNone(info)
        self.assertEqual(info['version'], '1.9.9')
        self.assertEqual(info['sha256'], 'a' * 64)
        self.assertEqual(info['download_url'], 'https://example.com/dl.exe')

    @patch.object(updater, 'APP_VERSION', '1.2.0')
    @patch('urllib.request.urlopen')
    def test_no_update_when_remote_not_newer(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(self._api_payload(tag='v1.2.0'))
        self.assertIsNone(updater.check_update())

    @patch.object(updater, 'APP_VERSION', '1.2.0')
    @patch('urllib.request.urlopen')
    def test_no_exe_asset_returns_none(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(
            self._api_payload(assets=[{'name': 'source.zip'}]))
        self.assertIsNone(updater.check_update())

    @patch.object(updater, 'APP_VERSION', '1.2.0')
    @patch('urllib.request.urlopen')
    def test_network_error_raises_update_error(self, mock_urlopen):
        # 网络失败必须抛异常而不是返回 None，
        # 否则 UI 会把"连不上服务器"误报成"已是最新版本"
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError('no route')
        with self.assertRaises(updater.UpdateError):
            updater.check_update()


class DownloadUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_dir = updater.UPDATE_DIR
        updater.UPDATE_DIR = self.tmp

    def tearDown(self):
        updater.UPDATE_DIR = self.orig_dir
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def _download(self, payload: bytes, expected_sha256=None):
        digest = hashlib.sha256(payload).hexdigest()
        resp = FakeResponse(payload, {'Content-Length': str(len(payload))})
        with patch('urllib.request.urlopen', return_value=resp):
            path = updater.download_update(
                'https://example.com/pkg.exe',
                expected_sha256=expected_sha256 or digest,
            )
        return path

    def test_download_with_matching_hash(self):
        payload = b'fake-exe-content' * 100
        path = self._download(payload)
        self.assertTrue(os.path.isfile(path))
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), payload)

    def test_download_with_bad_hash_deleted_and_raises(self):
        payload = b'corrupted-exe'
        with self.assertRaises(updater.UpdateError):
            self._download(payload, expected_sha256='0' * 64)
        dest = os.path.join(self.tmp, 'pkg.exe')
        self.assertFalse(os.path.exists(dest),
                         '校验失败的下载文件必须删除，避免被误安装')

    def test_download_without_hash_skips_verification(self):
        payload = b'no-digest-asset'
        resp = FakeResponse(payload, {'Content-Length': str(len(payload))})
        with patch('urllib.request.urlopen', return_value=resp):
            path = updater.download_update(
                'https://example.com/pkg.exe', expected_sha256=None)
        self.assertTrue(os.path.isfile(path))


class SwapFilesTests(unittest.TestCase):
    """rename-swap 是自动更新的核心，失败路径必须回滚。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def _make(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def test_swap_success(self):
        current = self._make('app.exe', b'old-version')
        new_exe = self._make('new.exe', b'new-version')
        updater._swap_files(new_exe, current)
        with open(current, 'rb') as f:
            self.assertEqual(f.read(), b'new-version')
        self.assertFalse(os.path.exists(new_exe), '新文件应被移动而非复制')
        with open(current + '.old', 'rb') as f:
            self.assertEqual(f.read(), b'old-version')

    def test_swap_removes_stale_old_first(self):
        current = self._make('app.exe', b'old-version')
        new_exe = self._make('new.exe', b'new-version')
        stale = self._make('app.exe.old', b'ancient')
        updater._swap_files(new_exe, current)
        with open(current, 'rb') as f:
            self.assertEqual(f.read(), b'new-version')
        with open(current + '.old', 'rb') as f:
            self.assertEqual(f.read(), b'old-version')

    def test_swap_missing_new_rolls_back(self):
        current = self._make('app.exe', b'old-version')
        missing = os.path.join(self.tmp, 'does-not-exist.exe')
        with self.assertRaises(updater.UpdateError):
            updater._swap_files(missing, current)
        # 当前 EXE 必须仍然可运行（内容未变）
        with open(current, 'rb') as f:
            self.assertEqual(f.read(), b'old-version')

    def test_swap_missing_current_raises(self):
        new_exe = self._make('new.exe', b'new-version')
        missing = os.path.join(self.tmp, 'does-not-exist.exe')
        with self.assertRaises(updater.UpdateError):
            updater._swap_files(new_exe, missing)
        self.assertTrue(os.path.isfile(new_exe), '新文件应保留在原地')


class ApplyUpdateTests(unittest.TestCase):
    """apply_update 的进程编排（Popen/os._exit 打桩，不真正退出）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    def test_dev_mode_returns_false_without_touching_files(self):
        fake_new = os.path.join(self.tmp, 'new.exe')
        with open(fake_new, 'wb') as f:
            f.write(b'x')
        with patch.object(updater.sys, 'frozen', False, create=True):
            self.assertFalse(updater.apply_update(fake_new))

    def test_frozen_missing_file_returns_false(self):
        fake_exe = os.path.join(self.tmp, 'app.exe')
        with open(fake_exe, 'wb') as f:
            f.write(b'old')
        with patch.object(updater.sys, 'frozen', True, create=True), \
                patch.object(updater.sys, 'executable', fake_exe):
            self.assertFalse(
                updater.apply_update(os.path.join(self.tmp, 'missing.exe')))

    def test_frozen_success_swaps_and_exits(self):
        fake_exe = os.path.join(self.tmp, 'app.exe')
        fake_new = os.path.join(self.tmp, 'new.exe')
        with open(fake_exe, 'wb') as f:
            f.write(b'old')
        with open(fake_new, 'wb') as f:
            f.write(b'new')
        exits = []
        popens = []
        with patch.object(updater.sys, 'frozen', True, create=True), \
                patch.object(updater.sys, 'executable', fake_exe), \
                patch('subprocess.Popen', side_effect=lambda *a, **k: popens.append(a)), \
                patch.object(updater.os, '_exit', side_effect=lambda code: exits.append(code)):
            self.assertTrue(updater.apply_update(fake_new))
        self.assertEqual(popens, [([fake_exe],)])
        self.assertEqual(exits, [0])
        with open(fake_exe, 'rb') as f:
            self.assertEqual(f.read(), b'new')
        with open(fake_exe + '.old', 'rb') as f:
            self.assertEqual(f.read(), b'old')

    def test_frozen_popen_failure_rolls_back(self):
        fake_exe = os.path.join(self.tmp, 'app.exe')
        fake_new = os.path.join(self.tmp, 'new.exe')
        with open(fake_exe, 'wb') as f:
            f.write(b'old')
        with open(fake_new, 'wb') as f:
            f.write(b'new')
        with patch.object(updater.sys, 'frozen', True, create=True), \
                patch.object(updater.sys, 'executable', fake_exe), \
                patch('subprocess.Popen', side_effect=OSError('blocked by AV')):
            self.assertFalse(updater.apply_update(fake_new))
        # 回滚后：当前位置仍是旧版本，新版本被删除
        with open(fake_exe, 'rb') as f:
            self.assertEqual(f.read(), b'old')
        self.assertFalse(os.path.exists(fake_exe + '.old'))

    def test_cleanup_after_update_removes_leftover(self):
        fake_exe = os.path.join(self.tmp, 'app.exe')
        leftover = fake_exe + '.old'
        with open(fake_exe, 'wb') as f:
            f.write(b'cur')
        with open(leftover, 'wb') as f:
            f.write(b'old')
        with patch.object(updater.sys, 'frozen', True, create=True), \
                patch.object(updater.sys, 'executable', fake_exe):
            self.assertTrue(updater.cleanup_after_update())
            self.assertFalse(os.path.exists(leftover))
            # 没有遗留时返回 False
            self.assertFalse(updater.cleanup_after_update())


if __name__ == '__main__':
    unittest.main()
