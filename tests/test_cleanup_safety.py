import os
import tempfile
import unittest

from cleaner_logic import CleanerLogic


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cleaner = CleanerLogic()
        self.cleaner.set_options({"simulate": False, "backup": False})

    def test_broad_windows_roots_are_never_safe_cleanup_targets(self):
        unsafe_paths = [
            "C:\\",
            "C:\\Users",
            "C:\\Windows\\WinSxS",
            "C:\\Windows\\SoftwareDistribution",
            "C:\\ProgramData\\Microsoft\\Windows Defender\\Quarantine",
        ]

        for path in unsafe_paths:
            with self.subTest(path=path):
                self.assertFalse(self.cleaner._is_safe_path(path))

    def test_analysis_only_large_file_cannot_be_deleted_by_core_api(self):
        with tempfile.TemporaryDirectory(prefix="cleaner-large-file-") as root:
            victim = os.path.join(root, "personal-video.mp4")
            with open(victim, "wb") as handle:
                handle.write(b"personal")

            result = self.cleaner.clean_selected([
                {"path": victim, "size": 8, "type": "large_files"},
            ])

            self.assertTrue(os.path.exists(victim))
            self.assertEqual(0, result["freed_space"])
            self.assertEqual(1, len(result["errors"]))
            self.assertIn("仅供查看", result["errors"][0]["error"])

    def test_disabled_system_category_cannot_be_deleted_by_core_api(self):
        with tempfile.TemporaryDirectory(prefix="cleaner-system-category-") as root:
            victim = os.path.join(root, "update.dat")
            with open(victim, "wb") as handle:
                handle.write(b"system")

            result = self.cleaner.clean_selected([
                {"path": victim, "size": 6, "type": "updates"},
            ])

            self.assertTrue(os.path.exists(victim))
            self.assertEqual(0, result["freed_space"])
            self.assertEqual(1, len(result["errors"]))
            self.assertIn("已停用", result["errors"][0]["error"])

    def test_scan_results_deduplicate_the_same_path_across_categories(self):
        duplicate = os.path.join(tempfile.gettempdir(), "duplicate.tmp")
        results = {
            "temp": [{"path": duplicate, "size": 10, "type": "temp"}],
            "logs": [{"path": duplicate, "size": 10, "type": "logs"}],
            "large_files": [
                {"path": duplicate, "size": 10, "type": "large_files"},
            ],
        }

        self.cleaner._deduplicate_scan_results(results)

        self.assertEqual(1, len(results["temp"]))
        self.assertEqual([], results["logs"])
        # 分析视图独立保留，但不会进入任何清理集合。
        self.assertEqual(1, len(results["large_files"]))


    def test_network_config_files_not_in_scan_results(self):
        """Verify that dangerous network config files are never included in scan results."""
        results = self.cleaner.scan_system(skip_categories=None)
        dangerous_suffixes = ('hosts.ics', 'networks', 'INDEX.BTR')
        for category, items in results.items():
            for item in items:
                path = item.get('path', '')
                basename = os.path.basename(path)
                with self.subTest(path=path, category=category):
                    self.assertNotIn(
                        basename, dangerous_suffixes,
                        f"Dangerous network config file found in scan results: {path}"
                    )

    def test_printer_icc_profiles_not_scanned(self):
        """Verify that ICC color profiles directory is not scanned as printer temp."""
        icc_path = os.path.join(
            'C:', os.sep, 'Windows', 'System32', 'spool', 'drivers', 'color'
        )
        # The path should not appear in printer_temp results
        results = self.cleaner.scan_system(skip_categories=None)
        printer_paths = [
            item.get('path', '') for item in results.get('printer_temp', [])
        ]
        self.assertNotIn(
            os.path.normcase(os.path.normpath(icc_path)),
            [os.path.normcase(os.path.normpath(p)) for p in printer_paths],
            "ICC color profiles directory should not be in printer_temp scan results"
        )


class CleanupSelectionPolicyTests(unittest.TestCase):
    def test_large_files_are_display_only_and_excluded_from_one_click(self):
        from app_modern import (
            category_default_checked,
            collect_one_click_items,
            is_category_cleanable,
        )

        large = {"path": "C:\\Users\\me\\video.mp4", "size": 123, "type": "large_files"}
        temp = {"path": "C:\\Temp\\old.tmp", "size": 10, "type": "temp"}

        self.assertFalse(is_category_cleanable("large_files"))
        self.assertFalse(category_default_checked("large_files"))
        self.assertEqual([temp], collect_one_click_items({
            "large_files": [large],
            "temp": [temp],
        }))

    def test_downloads_start_unchecked_but_remain_available_to_one_click(self):
        from app_modern import (
            category_default_checked,
            collect_one_click_items,
            is_category_cleanable,
        )

        download = {
            "path": "C:\\Users\\me\\Downloads\\archive.zip",
            "size": 100,
            "type": "downloads",
        }

        self.assertTrue(is_category_cleanable("downloads"))
        self.assertFalse(category_default_checked("downloads"))
        self.assertEqual([download], collect_one_click_items({"downloads": [download]}))


if __name__ == "__main__":
    unittest.main()
