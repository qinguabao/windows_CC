import json
import os
import tempfile
import unittest
from unittest.mock import patch

import settings


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'settings.json')

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)
        os.rmdir(self.tmp)

    def test_load_defaults_when_missing(self):
        with patch.object(settings, 'SETTINGS_PATH', self.path):
            result = settings.load_settings()
        self.assertTrue(result['simulate'])
        self.assertTrue(result['backup'])
        self.assertEqual(result['max_backups'], 5)

    def test_save_and_load_roundtrip(self):
        with patch.object(settings, 'SETTINGS_PATH', self.path):
            settings.save_settings({'simulate': False, 'backup': True, 'backup_dir': 'D:\\test', 'max_backups': 3, 'max_backup_size_mb': 1024})
            result = settings.load_settings()
        self.assertFalse(result['simulate'])
        self.assertEqual(result['backup_dir'], 'D:\\test')
        self.assertEqual(result['max_backups'], 3)

    def test_load_handles_corrupt_file(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w') as f:
            f.write('not json{{')
        with patch.object(settings, 'SETTINGS_PATH', self.path):
            result = settings.load_settings()
        self.assertTrue(result['simulate'])  # falls back to defaults
