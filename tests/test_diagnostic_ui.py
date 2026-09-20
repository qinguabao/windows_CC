# -*- coding: utf-8 -*-
"""深度诊断对话框的渲染与关闭行为测试（offscreen，不启动工作线程）。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest  # noqa: E402
from unittest.mock import Mock  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

from cleaner_logic import DIAGNOSTIC_ITEM_TYPE  # noqa: E402
from diagnostic import (  # noqa: E402
    DiagnosticDialog,
    _deduped_total,
    _size_text,
    format_size,
)


def _item(group, name, size, **overrides):
    item = {
        'type': DIAGNOSTIC_ITEM_TYPE,
        'group': group,
        'name': name,
        'path': f'C:/{name}',
        'size': size,
        'file_count': 1,
        'exists': True,
        'accessible': True,
        'incomplete': False,
        'failed': False,
        'suggestion': f'建议-{name}',
        'note': '',
    }
    item.update(overrides)
    return item


class DiagnosticDialogTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DiagnosticDialog(Mock())

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_dialog_has_expected_columns_and_no_delete_entry(self):
        headers = [self.dialog.tree.headerItem().text(i) for i in range(5)]
        self.assertEqual(headers, ["项目", "大小", "备注", "路径", "处理建议"])
        labels = [self.dialog.scan_button.text(), self.dialog.stop_button.text()]
        self.assertEqual(labels, ["开始诊断", "停止"])
        # 只读：对话框里不存在任何清理/删除按钮
        self.assertFalse(hasattr(self.dialog, 'clean_button'))

    def test_populate_builds_groups_in_defined_order(self):
        self.dialog._on_scan_finished([
            _item('ide', 'JetBrains', 100),
            _item('db', 'MySQL binlog', 500),
        ])
        titles = [self.dialog.tree.topLevelItem(i).text(0)
                  for i in range(self.dialog.tree.topLevelItemCount())]
        self.assertEqual(titles, ['数据库日志与数据', 'IDE / 编辑器缓存'])

    def test_populate_sorts_children_by_size_descending(self):
        self.dialog._on_scan_finished([
            _item('db', 'small', 10),
            _item('db', 'big', 900),
            _item('db', 'mid', 500),
        ])
        group = self.dialog.tree.topLevelItem(0)
        names = [group.child(i).text(0).split('（')[0] for i in range(group.childCount())]
        self.assertEqual(names, ['big', 'mid', 'small'])
        self.assertEqual(group.text(1), format_size(1410))

    def test_missing_items_are_skipped(self):
        self.dialog._on_scan_finished([
            _item('db', 'present', 100),
            _item('db', 'absent', 0, exists=False),
        ])
        group = self.dialog.tree.topLevelItem(0)
        self.assertEqual(group.childCount(), 1)
        self.assertIn('跳过 1 项', self.dialog.summary_label.text())

    def test_inaccessible_item_is_not_shown_as_zero(self):
        self.dialog._on_scan_finished([
            _item('system', 'WinSxS', 0, accessible=False),
        ])
        group = self.dialog.tree.topLevelItem(0)
        self.assertEqual(group.child(0).text(1), '无权限读取')

    def test_incomplete_item_notes_it_may_be_larger(self):
        self.dialog._on_scan_finished([
            _item('system', 'WinSxS', 4096, incomplete=True),
        ])
        group = self.dialog.tree.topLevelItem(0)
        self.assertIn('可能更大', group.child(0).text(1))
        self.assertIn('未统计完整', self.dialog.summary_label.text())

    def test_suggestion_and_note_are_rendered(self):
        self.dialog._on_scan_finished([
            _item('db', 'binlog', 100, note='log-bin 仍开启'),
        ])
        child = self.dialog.tree.topLevelItem(0).child(0)
        self.assertEqual(child.text(2), 'log-bin 仍开启')
        self.assertEqual(child.text(4), '建议-binlog')

    def test_unknown_group_is_appended_after_known_groups(self):
        self.dialog._on_scan_finished([
            _item('zzz_custom', 'custom', 10),
            _item('db', 'known', 20),
        ])
        titles = [self.dialog.tree.topLevelItem(i).text(0)
                  for i in range(self.dialog.tree.topLevelItemCount())]
        self.assertEqual(titles[0], '数据库日志与数据')
        self.assertEqual(titles[-1], 'zzz_custom')

    def test_close_without_scan_is_immediate(self):
        self.dialog.close()
        self.assertFalse(self.dialog.isVisible())

    def test_size_text_states(self):
        self.assertEqual(_size_text({'exists': False}), '—')
        self.assertEqual(_size_text({'exists': True, 'accessible': False}), '无权限读取')
        self.assertEqual(_size_text({'exists': True, 'accessible': False, 'failed': True}),
                         '诊断失败')
        self.assertEqual(_size_text({'exists': True, 'accessible': True, 'size': 1024}), '1.00 KB')
        self.assertEqual(
            _size_text({'exists': True, 'accessible': True, 'size': 1024, 'incomplete': True}),
            '1.00 KB（可能更大）')


class DiagnosticMainWindowWiringTests(unittest.TestCase):
    """主界面的「深度诊断」按钮与忙碌闸门。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        from unittest.mock import patch
        from app_modern import ModernCleanerWindow
        with patch("app_modern.CleanerLogic") as cleaner_class:
            mock_cleaner = cleaner_class.return_value
            mock_cleaner.get_disk_info.return_value = {
                "total": 100, "used": 50, "free": 50, "percent": 50}
            mock_cleaner.backup_dir = "D:\\CCleaner_Backup"
            mock_cleaner.max_backups = 5
            mock_cleaner.max_backup_size = 20 * 1024 * 1024 * 1024
            return ModernCleanerWindow()

    def test_button_exists_and_is_disabled_while_busy(self):
        window = self._make_window()
        try:
            self.assertEqual(window.diag_btn.text(), "深度诊断")
            self.assertTrue(window.diag_btn.isEnabled())
            window._set_busy(True)
            self.assertFalse(window.diag_btn.isEnabled())
            window._set_busy(False)
            self.assertTrue(window.diag_btn.isEnabled())
        finally:
            window.close()

    def test_open_diagnostics_shows_the_dialog(self):
        from unittest.mock import patch
        window = self._make_window()
        try:
            with patch("app_modern.DiagnosticDialog") as dialog_class:
                window.open_diagnostics()
            dialog_class.assert_called_once()
            dialog_class.return_value.exec.assert_called_once()
        finally:
            window.close()


class DedupedTotalTests(unittest.TestCase):
    """应用数据 Top 榜与具体探针会嵌套，合计必须去重。"""

    def test_nested_paths_are_counted_once(self):
        items = [
            _item('appdata', 'top', 100, path='C:/Users/x/AppData/Local'),
            _item('appdata', 'ms-playwright', 60,
                  path='C:/Users/x/AppData/Local/ms-playwright'),
            _item('system', 'WinSxS', 40, path='C:/Windows/WinSxS'),
        ]
        self.assertEqual(_deduped_total(items), 140)

    def test_child_is_counted_when_parent_is_absent(self):
        items = [_item('appdata', 'ms-playwright', 60,
                       path='C:/Users/x/AppData/Local/ms-playwright')]
        self.assertEqual(_deduped_total(items), 60)

    def test_multi_path_items_are_summed_verbatim(self):
        items = [_item('system', 'pagefile', 25, path='C:/pagefile.sys | C:/swapfile.sys')]
        self.assertEqual(_deduped_total(items), 25)

    def test_similar_prefix_is_not_treated_as_nested(self):
        items = [
            _item('appdata', 'a', 10, path='C:/data'),
            _item('appdata', 'b', 20, path='C:/data-other'),
        ]
        self.assertEqual(_deduped_total(items), 30)


class DedupedTotalSummaryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_summary_total_excludes_nested_duplicates(self):
        dialog = DiagnosticDialog(Mock())
        try:
            dialog._on_scan_finished([
                _item('appdata', 'LocalAppData · ms-playwright', 60,
                      path='C:/Users/x/AppData/Local/ms-playwright'),
                _item('appdata', 'ms-playwright 浏览器', 60,
                      path='C:/Users/x/AppData/Local/ms-playwright'),
                _item('system', 'WinSxS', 40, path='C:/Windows/WinSxS'),
            ])
            self.assertIn('100 B', dialog.summary_label.text())
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
