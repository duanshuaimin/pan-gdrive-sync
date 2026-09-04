import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pangdrive import paths
from pangdrive.storage import Storage


class TestPathsAndStorage(unittest.TestCase):
    def tearDown(self):
        Storage.reset_instance_for_tests()

    def test_migrate_copies_tasks_db_when_new_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_root = Path(tmp) / "pan-gdrive-sync"
            old_root = Path(tmp) / "pangdrive"
            old_root.mkdir()
            new_root.mkdir()
            old_db = old_root / "tasks.db"
            old_db.write_bytes(b"sqlite-fake")
            with mock.patch.object(paths, "CONFIG_DIR", new_root), mock.patch.object(
                paths, "LEGACY_CONFIG_DIR", old_root
            ):
                paths.migrate_legacy_artifacts()
                self.assertTrue((new_root / "tasks.db").is_file())
                self.assertEqual((new_root / "tasks.db").read_bytes(), b"sqlite-fake")
                self.assertTrue(old_db.is_file())  # not deleted

    def test_storage_default_db_under_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "pan-gdrive-sync"
            cfg.mkdir()
            with mock.patch("pangdrive.storage.CONFIG_DIR", cfg), mock.patch(
                "pangdrive.paths.CONFIG_DIR", cfg
            ), mock.patch("pangdrive.paths.LEGACY_CONFIG_DIR", Path(tmp) / "missing"):
                Storage.reset_instance_for_tests()
                s = Storage.get_instance()
                self.assertEqual(Path(s.db_path), cfg / "tasks.db")
