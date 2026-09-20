import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cleaner_logic import CleanerLogic


class StorageScanTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cleaner-storage-scan-")
        self.cleaner = CleanerLogic()
        self.cleaner.set_options({
            "backup_dir": os.path.join(self.root, "CCleaner_Backup"),
        })

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative_path, content):
        path = os.path.join(self.root, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_scan_finds_large_files_and_content_identical_duplicates(self):
        large = self._write("media/movie.bin", b"L" * 4096)
        first = self._write("first/copy.bin", b"duplicate-content")
        second = self._write("second/copy.bin", b"duplicate-content")
        same_size = self._write("third/different.bin", b"different-content")

        result = self.cleaner.scan_storage(
            self.root,
            min_large_size=3000,
            min_duplicate_size=1,
        )

        self.assertFalse(result["aborted"])
        self.assertEqual(
            [os.path.normpath(large)],
            [os.path.normpath(item["path"]) for item in result["large_files"]],
        )
        self.assertEqual(1, len(result["duplicate_groups"]))
        group = result["duplicate_groups"][0]
        duplicate_paths = {
            os.path.normpath(item["path"]) for item in group["files"]
        }
        self.assertEqual(
            {os.path.normpath(first), os.path.normpath(second)},
            duplicate_paths,
        )
        self.assertNotIn(os.path.normpath(same_size), duplicate_paths)
        self.assertEqual(len(b"duplicate-content"), group["reclaimable_size"])

    def test_scan_excludes_backup_and_protected_root_directories(self):
        visible = self._write("user/visible.bin", b"visible")
        backup_file = self._write("CCleaner_Backup/run/files/0001.bin", b"visible")
        protected_file = self._write("System Volume Information/private.bin", b"visible")

        result = self.cleaner.scan_storage(
            self.root,
            min_large_size=1,
            min_duplicate_size=1,
        )
        scanned_paths = {
            os.path.normpath(item["path"]) for item in result["large_files"]
        }

        self.assertIn(os.path.normpath(visible), scanned_paths)
        self.assertNotIn(os.path.normpath(backup_file), scanned_paths)
        self.assertNotIn(os.path.normpath(protected_file), scanned_paths)

    def test_abort_callback_stops_scan(self):
        for index in range(10):
            self._write(f"files/{index}.bin", b"data")
        calls = 0

        def abort():
            nonlocal calls
            calls += 1
            return calls >= 2

        result = self.cleaner.scan_storage(
            self.root,
            min_large_size=1,
            min_duplicate_size=1,
            abort_callback=abort,
        )

        self.assertTrue(result["aborted"])

    def test_scan_reuses_directory_entry_metadata_for_regular_files(self):
        files = {
            os.path.normcase(self._write(f"files/{index}.bin", b"data"))
            for index in range(4)
        }
        original_isfile = os.path.isfile
        original_islink = os.path.islink

        with patch(
            "cleaner_logic.os.path.isfile", wraps=original_isfile
        ) as isfile:
            with patch(
                "cleaner_logic.os.path.islink", wraps=original_islink
            ) as islink:
                result = self.cleaner.scan_storage(
                    self.root,
                    find_large=False,
                    find_duplicates=False,
                )

        queried_as_file = {
            os.path.normcase(call.args[0]) for call in isfile.call_args_list
            if call.args
        }
        queried_as_link = {
            os.path.normcase(call.args[0]) for call in islink.call_args_list
            if call.args
        }
        self.assertEqual(4, result["scanned_files"])
        self.assertTrue(files.isdisjoint(queried_as_file))
        self.assertTrue(files.isdisjoint(queried_as_link))

    def test_scan_reports_duplicate_hashing_phase(self):
        self._write("first/copy.bin", b"duplicate-content")
        self._write("second/copy.bin", b"duplicate-content")
        phases = []

        result = self.cleaner.scan_storage(
            self.root,
            min_duplicate_size=1,
            find_large=False,
            phase_callback=phases.append,
        )

        self.assertEqual(1, len(result["duplicate_groups"]))
        self.assertTrue(any("校验重复文件" in phase for phase in phases))
        self.assertTrue(any("已确认 1 组" in phase for phase in phases))

    def test_abort_keeps_duplicate_groups_already_verified(self):
        self._write("first/a.bin", b"a" * 11)
        self._write("second/a-copy.bin", b"a" * 11)
        self._write("first/b.bin", b"b" * 13)
        self._write("second/b-copy.bin", b"b" * 13)
        completed_exact_hashes = 0
        lock = threading.Lock()

        def fake_hash(path, size, quick, abort_callback=None):
            nonlocal completed_exact_hashes
            if abort_callback and abort_callback():
                return None, True
            if not quick:
                with lock:
                    completed_exact_hashes += 1
            return f"digest-{size}", False

        def abort():
            with lock:
                return completed_exact_hashes >= 2

        with patch.object(self.cleaner, "_file_hash", side_effect=fake_hash):
            result = self.cleaner.scan_storage(
                self.root,
                min_duplicate_size=1,
                find_large=False,
                abort_callback=abort,
            )

        self.assertTrue(result["aborted"])
        self.assertEqual(1, len(result["duplicate_groups"]))

    def test_quick_hashing_uses_bounded_parallel_reads(self):
        for index in range(6):
            self._write(f"files/{index}.bin", bytes([index]) * 16)
        active = 0
        peak_active = 0
        lock = threading.Lock()

        def fake_hash(path, size, quick, abort_callback=None):
            nonlocal active, peak_active
            if quick:
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return os.path.basename(path), False
            return os.path.basename(path), False

        with patch.object(self.cleaner, "_file_hash", side_effect=fake_hash):
            self.cleaner.scan_storage(
                self.root,
                min_duplicate_size=1,
                find_large=False,
            )

        self.assertEqual(4, peak_active)

    def test_duplicate_scan_reports_files_below_threshold(self):
        self._write("first/copy.bin", b"same-content")
        self._write("second/copy.bin", b"same-content")

        result = self.cleaner.scan_storage(
            self.root,
            min_duplicate_size=1024,
            find_large=False,
        )

        self.assertEqual([], result["duplicate_groups"])
        self.assertEqual(0, result["duplicate_scan"]["eligible_files"])
        self.assertEqual(1024, result["duplicate_scan"]["min_size"])

    def test_duplicate_scan_reports_unreadable_hash_candidates(self):
        self._write("first/copy.bin", b"same-content")
        self._write("second/copy.bin", b"same-content")

        with patch.object(self.cleaner, "_file_hash", return_value=(None, False)):
            result = self.cleaner.scan_storage(
                self.root,
                min_duplicate_size=1,
                find_large=False,
            )

        self.assertEqual([], result["duplicate_groups"])
        self.assertEqual(2, result["duplicate_scan"]["unreadable_files"])
        self.assertEqual(2, len(result["errors"]))

    def test_quick_hash_honors_abort_before_opening_file(self):
        path = self._write("files/data.bin", b"data")

        with patch("builtins.open") as open_file:
            digest, aborted = self.cleaner._file_hash(
                path, 4, True, abort_callback=lambda: True)

        self.assertIsNone(digest)
        self.assertTrue(aborted)
        open_file.assert_not_called()


class StorageCleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cleaner-storage-clean-")
        self.cleaner = CleanerLogic()
        self.cleaner.set_options({"simulate": False, "backup": False})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name, content=b"payload"):
        path = os.path.join(self.root, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def _item(self, path, item_type="storage_large_file", **extra):
        item = {
            "path": path,
            "size": os.path.getsize(path),
            "type": item_type,
            "scan_root": self.root,
        }
        item.update(extra)
        return item

    def test_storage_cleanup_permanently_deletes_selected_file(self):
        victim = self._write("large.bin")

        with patch.dict(os.environ, {"SystemDrive": "Z:"}):
            result = self.cleaner.clean_selected([self._item(victim)])

        self.assertFalse(os.path.exists(victim))
        self.assertEqual(len(b"payload"), result["freed_space"])
        self.assertEqual([], result["errors"])

    def test_storage_cleanup_rejects_path_outside_scan_root(self):
        victim = self._write("outside.bin")
        other_root = os.path.join(self.root, "selected")
        os.makedirs(other_root)
        item = self._item(victim)
        item["scan_root"] = other_root

        with patch.dict(os.environ, {"SystemDrive": "Z:"}):
            result = self.cleaner.clean_selected([item])

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(0, result["freed_space"])
        self.assertIn("扫描范围", result["errors"][0]["error"])

    def test_duplicate_group_cannot_be_deleted_completely(self):
        first = self._write("first.bin", b"same")
        second = self._write("second.bin", b"same")
        common = {
            "duplicate_group": "group-1",
            "duplicate_count": 2,
        }
        items = [
            self._item(first, "storage_duplicate_file", **common),
            self._item(second, "storage_duplicate_file", **common),
        ]

        with patch.dict(os.environ, {"SystemDrive": "Z:"}):
            result = self.cleaner.clean_selected(items)

        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertEqual(0, result["freed_space"])
        self.assertIn("至少保留一个", result["errors"][0]["error"])

    def test_duplicate_group_allows_deleting_one_copy(self):
        first = self._write("first.bin", b"same")
        second = self._write("second.bin", b"same")
        item = self._item(
            second,
            "storage_duplicate_file",
            duplicate_group="group-1",
            duplicate_count=2,
        )

        with patch.dict(os.environ, {"SystemDrive": "Z:"}):
            result = self.cleaner.clean_selected([item])

        self.assertTrue(os.path.exists(first))
        self.assertFalse(os.path.exists(second))
        self.assertEqual(4, result["freed_space"])

    def test_same_volume_backup_blocks_storage_cleanup(self):
        victim = self._write("large.bin")
        self.cleaner.set_options({
            "backup": True,
            "backup_dir": os.path.join(self.root, "backups"),
        })

        with patch.dict(os.environ, {"SystemDrive": "Z:"}):
            result = self.cleaner.clean_selected([self._item(victim)])

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(0, result["freed_space"])
        self.assertIn("备份目录与目标磁盘相同", result["errors"][0]["error"])

    def test_non_system_drive_system_directories_are_protected(self):
        self.assertFalse(self.cleaner._is_safe_path("D:\\Windows\\System32\\kernel.dll"))
        self.assertFalse(self.cleaner._is_safe_path("D:\\System Volume Information\\data.bin"))
        self.assertFalse(self.cleaner._is_safe_path("D:\\$Recycle.Bin\\item.bin"))


if __name__ == "__main__":
    unittest.main()
