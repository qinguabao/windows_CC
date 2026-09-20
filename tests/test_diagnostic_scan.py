# -*- coding: utf-8 -*-
"""深度诊断扫描的只读性与正确性测试。"""

import builtins
import os
import shutil
import tempfile
import unittest
from unittest import mock

from cleaner_logic import (
    ANALYSIS_ONLY_CATEGORIES,
    DIAGNOSTIC_GROUPS,
    DIAGNOSTIC_ITEM_TYPE,
    CleanerLogic,
    build_diagnostic_probes,
)


def _snapshot(root):
    """记录目录树中每个文件的大小与修改时间，用于证明扫描没有改动文件。"""
    snapshot = {}
    for current, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(current, name)
            stat_result = os.stat(path)
            snapshot[os.path.relpath(path, root)] = (
                stat_result.st_size, stat_result.st_mtime_ns)
    return snapshot


class DiagnosticProbeTests(unittest.TestCase):
    """探针表本身的结构约束。"""

    def test_default_probes_cover_every_group(self):
        probes = build_diagnostic_probes()
        groups = {probe['group'] for probe in probes}
        self.assertEqual(groups, set(DIAGNOSTIC_GROUPS))
        for probe in probes:
            self.assertTrue(probe.get('name'))
            self.assertTrue(probe.get('suggestion'))
            self.assertIn(probe['kind'],
                          {'dir', 'glob', 'files', 'dynamic_top', 'note_only'})

    def test_probes_are_built_from_env_not_user_profile_guessing(self):
        fake_env = {
            'APPDATA': r'X:\roaming',
            'LOCALAPPDATA': r'X:\local',
            'USERPROFILE': r'X:\home',
            'ProgramData': r'X:\pd',
            'ProgramFiles': r'X:\pf',
            'SystemRoot': r'X:\win',
        }
        probes = build_diagnostic_probes(fake_env)
        paths = ' | '.join(probe.get('path', '') for probe in probes)
        self.assertIn(r'X:\roaming', paths)
        self.assertIn(r'X:\local', paths)


class DiagnosticScanTests(unittest.TestCase):

    def setUp(self):
        self.cleaner = CleanerLogic()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _write(self, relative, size):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'x' * size)
        return path

    def test_dir_probe_sums_nested_files(self):
        self._write('a.bin', 1024)
        self._write(os.path.join('sub', 'b.bin'), 2048)
        probes = [{'group': 'db', 'name': 'root', 'kind': 'dir',
                   'path': self.root, 'suggestion': 's'}]
        [item] = self.cleaner.scan_diagnostics(probes=probes)
        self.assertTrue(item['exists'])
        self.assertTrue(item['accessible'])
        self.assertEqual(item['size'], 3072)
        self.assertEqual(item['file_count'], 2)

    def test_glob_probe_only_counts_matching_files(self):
        self._write('MARIO-bin.000001', 1000)
        self._write('MARIO-bin.000002', 2000)
        self._write('MARIO.err', 9999)
        probes = [{'group': 'db', 'name': 'binlog', 'kind': 'glob',
                   'path': self.root, 'pattern': '*-bin.[0-9]*',
                   'suggestion': 's'}]
        [item] = self.cleaner.scan_diagnostics(probes=probes)
        self.assertEqual(item['size'], 3000)
        self.assertEqual(item['file_count'], 2)

    def test_missing_path_is_reported_not_exists(self):
        probes = [{'group': 'db', 'name': 'gone', 'kind': 'dir',
                   'path': os.path.join(self.root, 'nope'), 'suggestion': 's'}]
        [item] = self.cleaner.scan_diagnostics(probes=probes)
        self.assertFalse(item['exists'])
        self.assertEqual(item['size'], 0)

    def test_dynamic_top_filters_by_threshold_and_sorts(self):
        self._write(os.path.join('small', 'f.bin'), 10)
        self._write(os.path.join('mid', 'f.bin'), 5000)
        self._write(os.path.join('big', 'f.bin'), 9000)
        probes = [{'group': 'appdata', 'name': 'top', 'kind': 'dynamic_top',
                   'base': self.root, 'threshold': 1000, 'limit': 5,
                   'suggestion': 's'}]
        items = self.cleaner.scan_diagnostics(probes=probes)
        names = [item['path'] for item in items]
        self.assertEqual(names, [os.path.join(self.root, 'big'),
                                 os.path.join(self.root, 'mid')])
        self.assertEqual([item['size'] for item in items], [9000, 5000])

    def test_dynamic_top_reports_overflow_bucket(self):
        for index in range(4):
            self._write(os.path.join(f'd{index}', 'f.bin'), 5000)
        probes = [{'group': 'appdata', 'name': 'top', 'kind': 'dynamic_top',
                   'base': self.root, 'threshold': 1000, 'limit': 2,
                   'suggestion': 's'}]
        items = self.cleaner.scan_diagnostics(probes=probes)
        self.assertEqual(len(items), 3)
        self.assertIn('其余 2 个子目录', items[-1]['name'])
        self.assertEqual(items[-1]['size'], 10000)

    def test_note_only_probe_has_no_size(self):
        probes = [{'group': 'system', 'name': '还原点', 'kind': 'note_only',
                   'suggestion': 's', 'note': '需管理员权限查看'}]
        [item] = self.cleaner.scan_diagnostics(probes=probes)
        self.assertTrue(item['exists'])
        self.assertEqual(item['size'], 0)
        self.assertEqual(item['note'], '需管理员权限查看')

    def test_inaccessible_directory_is_flagged(self):
        probes = [{'group': 'system', 'name': 'WinSxS', 'kind': 'dir',
                   'path': self.root, 'suggestion': 's'}]
        with mock.patch('os.scandir', side_effect=PermissionError('denied')):
            [item] = self.cleaner.scan_diagnostics(probes=probes)
        self.assertTrue(item['exists'])
        self.assertFalse(item['accessible'])
        self.assertEqual(item['size'], 0)

    def test_probe_failure_does_not_abort_whole_scan(self):
        good = self._write('good.bin', 777)
        probes = [
            {'group': 'db', 'name': 'before', 'kind': 'files',
             'paths': [good], 'suggestion': 's'},
            {'group': 'db', 'name': 'broken', 'kind': 'dir',
             'path': object(), 'suggestion': 's'},
            {'group': 'db', 'name': 'after', 'kind': 'files',
             'paths': [good], 'suggestion': 's'},
        ]
        items = self.cleaner.scan_diagnostics(probes=probes)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]['size'], 777)
        self.assertTrue(items[1]['failed'])
        self.assertIn('诊断失败', items[1]['note'])
        self.assertEqual(items[2]['size'], 777)

    def test_abort_callback_stops_scan(self):
        self._write('a.bin', 1024)
        probes = [{'group': 'db', 'name': f'p{i}', 'kind': 'dir',
                   'path': self.root, 'suggestion': 's'} for i in range(5)]
        items = self.cleaner.scan_diagnostics(
            probes=probes, abort_callback=lambda: True)
        self.assertEqual(items, [])

    def test_deadline_marks_probe_incomplete(self):
        self._write(os.path.join('sub', 'a.bin'), 10)
        probes = [{'group': 'system', 'name': 'huge', 'kind': 'dir',
                   'path': self.root, 'suggestion': 's'}]
        with mock.patch('time.monotonic', side_effect=[0.0, 1000.0, 1000.0, 1000.0]):
            [item] = self.cleaner.scan_diagnostics(
                probes=probes, deadline_seconds=1)
        self.assertTrue(item['incomplete'])


class DiagnosticMySqlNoteTests(unittest.TestCase):
    """my.ini 的 log-bin 状态会体现在备注里。"""

    def _note_for(self, ini_body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server_dir = os.path.join(tmp.name, 'MySQL Server 8.0')
        data_dir = os.path.join(server_dir, 'Data')
        os.makedirs(data_dir)
        if ini_body is not None:
            with open(os.path.join(server_dir, 'my.ini'), 'w', encoding='utf-8') as handle:
                handle.write(ini_body)
        probes = [{'group': 'db', 'name': 'binlog', 'kind': 'glob',
                   'path': data_dir, 'pattern': '*-bin.[0-9]*',
                   'suggestion': 's', 'inspect': 'mysql_log_bin',
                   'server_dir': server_dir}]
        [item] = CleanerLogic().scan_diagnostics(probes=probes)
        return item['note']

    def test_active_log_bin_is_flagged(self):
        self.assertIn('仍开启', self._note_for('log-bin="X-bin"\n'))

    def test_commented_log_bin_is_reported_off(self):
        self.assertIn('不会再增长', self._note_for('#log-bin="X-bin"\n'))

    def test_missing_my_ini_is_tolerated(self):
        note = self._note_for(None)
        self.assertNotIn('仍开启', note)
        self.assertNotIn('不会再增长', note)


class DiagnosticReadOnlyTests(unittest.TestCase):
    """证明诊断功能不会改动文件系统，也无法经由删除核心删文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.cleaner = CleanerLogic()
        os.makedirs(os.path.join(self.root, 'sub'))
        self.target = os.path.join(self.root, 'keep.bin')
        with open(self.target, 'wb') as handle:
            handle.write(b'y' * 4096)
        with open(os.path.join(self.root, 'sub', 'nested.bin'), 'wb') as handle:
            handle.write(b'z' * 2048)

    def _probes(self):
        return [
            {'group': 'db', 'name': 'dir', 'kind': 'dir',
             'path': self.root, 'suggestion': 's'},
            {'group': 'db', 'name': 'glob', 'kind': 'glob',
             'path': self.root, 'pattern': '*.bin', 'suggestion': 's'},
            {'group': 'appdata', 'name': 'top', 'kind': 'dynamic_top',
             'base': self.root, 'threshold': 0, 'limit': 10, 'suggestion': 's'},
            {'group': 'system', 'name': 'files', 'kind': 'files',
             'paths': [self.target], 'suggestion': 's'},
        ]

    def test_scan_leaves_tree_byte_identical(self):
        before = _snapshot(self.root)
        self.cleaner.scan_diagnostics(probes=self._probes())
        self.assertEqual(_snapshot(self.root), before)

    def test_diagnostic_items_carry_the_analysis_only_type(self):
        items = self.cleaner.scan_diagnostics(probes=self._probes())
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item['type'], DIAGNOSTIC_ITEM_TYPE)
        self.assertIn(DIAGNOSTIC_ITEM_TYPE, ANALYSIS_ONLY_CATEGORIES)

    def test_clean_selected_refuses_diagnostic_items(self):
        # 真实删除模式：如果类型过滤失效，下面的文件就会被删掉。
        self.cleaner.set_options({'simulate': False, 'backup': False})
        items = self.cleaner.scan_diagnostics(probes=self._probes())
        result = self.cleaner.clean_selected(items)
        self.assertEqual(result['cleaned_items'], [])
        self.assertTrue(result['errors'])
        for error in result['errors']:
            self.assertIn('仅供查看', error['error'])
        self.assertTrue(os.path.exists(self.target))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'sub', 'nested.bin')))

    def test_scan_survives_all_write_operations_being_blocked(self):
        before = _snapshot(self.root)
        real_open = builtins.open

        def guarded_open(file, mode='r', *args, **kwargs):
            if any(flag in mode for flag in ('w', 'a', 'x', '+')):
                raise AssertionError(f'诊断过程尝试写入文件：{file} ({mode})')
            return real_open(file, mode, *args, **kwargs)

        def blocked(*_args, **_kwargs):
            raise AssertionError('诊断过程尝试修改文件系统')

        with mock.patch('builtins.open', guarded_open), \
                mock.patch('os.remove', blocked), \
                mock.patch('os.rename', blocked), \
                mock.patch('os.replace', blocked), \
                mock.patch('os.mkdir', blocked), \
                mock.patch('os.makedirs', blocked), \
                mock.patch('os.rmdir', blocked), \
                mock.patch('shutil.rmtree', blocked), \
                mock.patch('shutil.move', blocked):
            items = self.cleaner.scan_diagnostics(probes=self._probes())
        self.assertTrue(items)
        self.assertEqual(_snapshot(self.root), before)

    def test_scan_does_not_touch_cleaner_configuration(self):
        backup_dir = self.cleaner.backup_dir
        max_backups = self.cleaner.max_backups
        max_backup_size = self.cleaner.max_backup_size
        self.cleaner.scan_diagnostics(probes=self._probes())
        self.assertEqual(self.cleaner.backup_dir, backup_dir)
        self.assertEqual(self.cleaner.max_backups, max_backups)
        self.assertEqual(self.cleaner.max_backup_size, max_backup_size)


if __name__ == '__main__':
    unittest.main()
