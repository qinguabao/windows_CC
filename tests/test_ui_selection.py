import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app_modern import ModernCleanerWindow


class SelectionSizeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_checked_size_updates_when_items_are_selected_or_cleared(self):
        with patch("app_modern.CleanerLogic") as cleaner_class:
            mock_cleaner = cleaner_class.return_value
            mock_cleaner.get_disk_info.return_value = {
                "total": 100,
                "used": 50,
                "free": 50,
                "percent": 50,
            }
            mock_cleaner.backup_dir = "D:\\CCleaner_Backup"
            mock_cleaner.max_backups = 5
            mock_cleaner.max_backup_size = 20 * 1024 * 1024 * 1024
            window = ModernCleanerWindow()

        try:
            window._populate({
                "temp": [
                    {"path": "C:\\Temp\\old.tmp", "size": 100, "type": "temp"},
                ],
                "downloads": [
                    {
                        "path": "C:\\Users\\me\\Downloads\\archive.zip",
                        "size": 900,
                        "type": "downloads",
                    },
                ],
            })

            temp_item = window.tree.topLevelItem(0).child(0)
            download_item = window.tree.topLevelItem(1).child(0)
            self.assertEqual("100 B", window.big_num.text())
            self.assertEqual(Qt.Unchecked, download_item.checkState(0))

            download_item.setCheckState(0, Qt.Checked)
            self.app.processEvents()
            self.assertEqual("1000 B", window.big_num.text())

            temp_item.setCheckState(0, Qt.Unchecked)
            self.app.processEvents()
            self.assertEqual("900 B", window.big_num.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
