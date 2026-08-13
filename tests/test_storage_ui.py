import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from storage_cleaner import StorageCleanerDialog


class StorageCleanerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cleaner = Mock()
        self.cleaner.backup_dir = "E:\\CCleaner_Backup"
        self.cleaner.get_available_drives.return_value = [{
            "path": "D:\\",
            "name": "D:",
            "total": 1000,
            "used": 600,
            "free": 400,
            "percent": 60,
            "removable": False,
        }]
        self.dialog = StorageCleanerDialog(self.cleaner)

    def tearDown(self):
        self.dialog.close()

    def _results(self):
        first = {
            "path": "D:\\media\\first.bin",
            "size": 100,
            "modified": "2026-01-01 10:00:00",
            "type": "storage_duplicate_file",
            "scan_root": "D:\\",
            "duplicate_group": "group-1",
            "duplicate_count": 2,
            "recommended_keep": True,
        }
        second = dict(
            first,
            path="D:\\backup\\second.bin",
            recommended_keep=False,
        )
        large_first = dict(
            first,
            type="storage_large_file",
        )
        return {
            "scan_root": "D:\\",
            "large_files": [large_first],
            "duplicate_groups": [{
                "id": "group-1",
                "size": 100,
                "count": 2,
                "reclaimable_size": 100,
                "files": [first, second],
            }],
            "scanned_files": 2,
            "scanned_size": 200,
            "errors": [],
            "aborted": False,
        }

    def test_results_start_unchecked(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)

        self.assertEqual(Qt.Unchecked, self.dialog.large_tree.topLevelItem(0).checkState(0))
        group = self.dialog.duplicate_tree.topLevelItem(0)
        self.assertTrue(group.isExpanded())
        self.assertEqual(Qt.Unchecked, group.child(0).checkState(0))
        self.assertFalse(self.dialog.clean_button.isEnabled())
        self.assertIn("已选 0 个文件", self.dialog.selected_summary.text())

    def test_same_path_selected_in_both_views_is_counted_once(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)
        self.dialog.large_tree.topLevelItem(0).setCheckState(0, Qt.Checked)
        self.dialog.duplicate_tree.topLevelItem(0).child(0).setCheckState(
            0, Qt.Checked)
        self.app.processEvents()

        selected = self.dialog._collect_selected()
        self.assertEqual(1, len(selected))
        self.assertIn("已选 1 个文件", self.dialog.selected_summary.text())
        self.assertIn("100 B", self.dialog.selected_summary.text())

    def test_selecting_every_copy_is_rejected(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)
        group = self.dialog.duplicate_tree.topLevelItem(0)
        group.child(0).setCheckState(0, Qt.Checked)
        group.child(1).setCheckState(0, Qt.Checked)

        self.assertTrue(
            self.dialog._duplicate_violation(self.dialog._collect_selected()))

    def test_actual_cleanup_marks_deleted_without_rescanning(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)
        large_node = self.dialog.large_tree.topLevelItem(0)
        duplicate_node = self.dialog.duplicate_tree.topLevelItem(0).child(0)
        cleaned_path = large_node.data(0, Qt.UserRole)["path"]
        self.dialog._clean_items = [large_node.data(0, Qt.UserRole)]
        self.dialog.simulate_checkbox.setChecked(False)

        with patch("storage_cleaner.QMessageBox.information"), patch.object(
                self.dialog, "start_scan") as start_scan:
            self.dialog._clean_finished({
                "freed_space": 100,
                "cleaned_items": [cleaned_path],
                "errors": [],
            })
            self.app.processEvents()

        start_scan.assert_not_called()
        self.assertEqual("已删除", large_node.text(2))
        self.assertEqual("已删除", duplicate_node.text(2))
        self.assertFalse(large_node.flags() & Qt.ItemIsUserCheckable)
        self.assertIn("已选 0 个文件", self.dialog.selected_summary.text())

    def test_duplicate_group_recalculates_and_protects_last_file(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)
        group = self.dialog.duplicate_tree.topLevelItem(0)
        deleted_node = group.child(1)
        remaining_node = group.child(0)
        deleted_path = deleted_node.data(0, Qt.UserRole)["path"]
        self.dialog._clean_items = [deleted_node.data(0, Qt.UserRole)]
        self.dialog.simulate_checkbox.setChecked(False)

        with patch("storage_cleaner.QMessageBox.information"):
            self.dialog._clean_finished({
                "freed_space": 100,
                "cleaned_items": [deleted_path],
                "errors": [],
            })

        self.assertEqual("已删除", deleted_node.text(2))
        self.assertEqual("唯一保留文件", remaining_node.text(2))
        self.assertFalse(remaining_node.flags() & Qt.ItemIsUserCheckable)
        self.assertIn("剩余 1 份", group.text(0))

    def test_failed_cleanup_keeps_item_available_for_retry(self):
        self.dialog.results = self._results()
        self.dialog._populate_results(self.dialog.results)
        node = self.dialog.large_tree.topLevelItem(0)
        item = node.data(0, Qt.UserRole)
        self.dialog._clean_items = [item]
        self.dialog.simulate_checkbox.setChecked(False)

        with patch("storage_cleaner.QMessageBox.warning"):
            self.dialog._clean_finished({
                "freed_space": 0,
                "cleaned_items": [],
                "errors": [{"path": item["path"], "error": "文件正在使用中"}],
            })

        self.assertEqual("删除失败", node.text(2))
        self.assertTrue(node.flags() & Qt.ItemIsUserCheckable)
        self.assertIn("文件正在使用中", node.toolTip(2))


if __name__ == "__main__":
    unittest.main()
