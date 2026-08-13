import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cleaner_logic import DEFAULT_MAX_BACKUP_SIZE, CleanerLogic


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cleaner-backup-test-")
        self.backup_dir = os.path.join(self.root, "backups")
        self.cleaner = CleanerLogic()
        self.cleaner.set_options({
            "simulate": False,
            "backup": True,
            "backup_dir": self.backup_dir,
            "max_backups": 20,
            "max_backup_size": 100 * 1024 * 1024,
        })

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative_path, content):
        path = os.path.join(self.root, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def _only_backup(self):
        backups = self.cleaner.get_backup_info()["backups"]
        self.assertEqual(1, len(backups))
        return backups[0]

    def test_same_named_files_keep_distinct_payloads_and_original_paths(self):
        first = self._write(os.path.join("first", "same.txt"), b"first")
        second = self._write(os.path.join("second", "same.txt"), b"second")

        result = self.cleaner.clean_selected([
            {"path": first, "size": 5, "type": "temp"},
            {"path": second, "size": 6, "type": "temp"},
        ])

        self.assertEqual([], result["errors"])
        backup = self._only_backup()
        manifest_path = os.path.join(backup["path"], "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)

        self.assertEqual({os.path.abspath(first), os.path.abspath(second)}, {
            entry["original_path"] for entry in manifest["entries"]
        })
        payload_paths = [
            os.path.join(backup["path"], entry["backup_path"])
            for entry in manifest["entries"]
        ]
        self.assertEqual(2, len(set(payload_paths)))
        with open(payload_paths[0], "rb") as handle:
            first_payload = handle.read()
        with open(payload_paths[1], "rb") as handle:
            second_payload = handle.read()
        self.assertEqual({b"first", b"second"}, {first_payload, second_payload})

    def test_restore_uses_manifest_original_paths(self):
        first = self._write(os.path.join("restore-a", "same.txt"), b"alpha")
        second = self._write(os.path.join("restore-b", "same.txt"), b"beta")
        self.cleaner.clean_selected([
            {"path": first, "size": 5, "type": "temp"},
            {"path": second, "size": 4, "type": "temp"},
        ])
        backup = self._only_backup()

        result = self.cleaner.restore_backup_detailed(backup["path"])

        self.assertTrue(result["success"])
        self.assertEqual(2, result["restored_count"])
        with open(first, "rb") as handle:
            self.assertEqual(b"alpha", handle.read())
        with open(second, "rb") as handle:
            self.assertEqual(b"beta", handle.read())

    def test_backup_failure_keeps_source_file_and_reports_error(self):
        victim = self._write(os.path.join("failure", "victim.txt"), b"keep-me")

        with patch("cleaner_logic.shutil.copy2", side_effect=OSError("forced failure")):
            result = self.cleaner.clean_selected([
                {"path": victim, "size": 7, "type": "temp"},
            ])

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(0, result["freed_space"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn("备份失败", result["errors"][0]["error"])

    def test_simulation_does_not_create_empty_backup(self):
        victim = self._write(os.path.join("simulation", "victim.txt"), b"preview")
        self.cleaner.set_options({"simulate": True, "backup": True})

        result = self.cleaner.clean_selected([
            {"path": victim, "size": 7, "type": "temp"},
        ])

        self.assertEqual(7, result["freed_space"])
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(0, self.cleaner.get_backup_info()["backup_count"])

    def test_backup_session_creation_failure_aborts_without_deleting(self):
        victim = self._write(os.path.join("session-failure", "victim.txt"), b"keep")

        with patch.object(
                self.cleaner, "_create_backup_session",
                side_effect=OSError("backup directory is not writable")):
            result = self.cleaner.clean_selected([
                {"path": victim, "size": 4, "type": "temp"},
            ])

        self.assertTrue(os.path.exists(victim))
        self.assertEqual(0, result["freed_space"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn("无法创建备份", result["errors"][0]["error"])

    def test_backup_directory_is_protected_and_parent_cleanup_is_detected(self):
        payload = os.path.join(self.backup_dir, "run", "files", "00000001.bin")
        self.assertFalse(self.cleaner._is_safe_path(self.backup_dir))
        self.assertFalse(self.cleaner._is_safe_path(payload))
        self.assertTrue(self.cleaner._is_safe_path(self.root))
        self.assertTrue(self.cleaner._path_contains_backup_dir(self.root))

    def test_delete_backup_rejects_root_and_unmanaged_paths(self):
        unmanaged = os.path.join(self.root, "unmanaged")
        os.makedirs(unmanaged)

        self.assertFalse(self.cleaner.delete_backup(self.backup_dir))
        self.assertFalse(self.cleaner.delete_backup(unmanaged))
        self.assertTrue(os.path.isdir(unmanaged))

    def test_old_backup_cleanup_keeps_newest_count(self):
        for index in range(3):
            victim = self._write(
                os.path.join(f"retention-{index}", "victim.txt"),
                str(index).encode("ascii"),
            )
            result = self.cleaner.clean_selected([
                {"path": victim, "size": 1, "type": "temp"},
            ])
            self.assertEqual([], result["errors"])

        before = self.cleaner.get_backup_info()["backups"]
        self.assertEqual(3, len(before))
        newest_names = {backup["name"] for backup in before[:2]}
        self.cleaner.set_options({"max_backups": 2})

        self.assertTrue(self.cleaner.clean_old_backups())
        after = self.cleaner.get_backup_info()["backups"]
        self.assertEqual(2, len(after))
        self.assertEqual(newest_names, {backup["name"] for backup in after})

    def test_retention_is_applied_after_a_new_backup_is_created(self):
        self.cleaner.set_options({"max_backups": 2})

        for index in range(3):
            victim = self._write(
                os.path.join(f"automatic-retention-{index}", "victim.txt"),
                str(index).encode("ascii"),
            )
            result = self.cleaner.clean_selected([
                {"path": victim, "size": 1, "type": "temp"},
            ])
            self.assertEqual([], result["errors"])

        self.assertEqual(2, self.cleaner.get_backup_info()["backup_count"])

    def test_size_retention_never_deletes_the_only_latest_backup(self):
        victim = self._write(os.path.join("oversized", "victim.txt"), b"latest")
        result = self.cleaner.clean_selected([
            {"path": victim, "size": 6, "type": "temp"},
        ])
        self.assertEqual([], result["errors"])

        self.cleaner.set_options({"max_backup_size": 1})
        self.assertTrue(self.cleaner.clean_old_backups())
        self.assertEqual(1, self.cleaner.get_backup_info()["backup_count"])

    def test_default_backup_retention_target_is_twenty_gibibytes(self):
        self.assertEqual(20 * 1024 * 1024 * 1024, DEFAULT_MAX_BACKUP_SIZE)


if __name__ == "__main__":
    unittest.main()
