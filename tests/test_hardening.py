import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pangdrive import paths
from pangdrive.config import Config
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


class TestWebBasicAuth(unittest.TestCase):
    def _config_with_web_auth(self):
        cfg = Config.__new__(Config)
        cfg.config_dir = Path(tempfile.mkdtemp())
        cfg.config_file = cfg.config_dir / "config.json"
        cfg.data = {
            "baidu": {},
            "gdrive": {"auth_mode": "oauth2"},
            "transfer": {},
            "web": {"username": "admin", "password_hash": ""},
        }
        cfg.set_web_auth("admin", "secret")
        return cfg

    def test_set_web_auth_hashes_password_and_marks_configured(self):
        cfg = self._config_with_web_auth()

        self.assertEqual(cfg.data["web"]["username"], "admin")
        self.assertNotEqual(cfg.data["web"]["password_hash"], "secret")
        self.assertTrue(cfg.has_web_auth())

    def test_has_web_auth_false_when_empty(self):
        cfg = Config.__new__(Config)
        cfg.data = {"web": {"username": "", "password_hash": ""}}

        self.assertFalse(cfg.has_web_auth())

    def test_api_requires_valid_basic_auth(self):
        from pangdrive.web.app import create_app

        cfg = self._config_with_web_auth()
        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.app.TaskManager.get_instance", return_value=mock.MagicMock()
        ):
            client = create_app().test_client()
            response = client.get("/api/status")
            self.assertEqual(response.status_code, 401)
            self.assertIn("Basic", response.headers.get("WWW-Authenticate", ""))

            token = base64.b64encode(b"admin:secret").decode()
            response = client.get(
                "/api/status", headers={"Authorization": f"Basic {token}"}
            )
            self.assertNotEqual(response.status_code, 401)

    def test_web_cmd_requires_auth_config(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        with mock.patch("pangdrive.cli.config") as cfg, mock.patch(
            "pangdrive.web.create_app"
        ):
            cfg.has_web_auth.return_value = False
            result = CliRunner().invoke(cli, ["web"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Web auth not configured.", result.output)

    def test_web_cmd_refuses_debug_on_non_loopback_bind(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        with mock.patch("pangdrive.cli.config") as cfg, mock.patch(
            "pangdrive.web.create_app"
        ):
            cfg.has_web_auth.return_value = True
            result = CliRunner().invoke(
                cli, ["web", "--host", "0.0.0.0", "--debug"]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Refusing --debug on non-loopback bind.", result.output)

    def test_auth_web_cmd_saves_prompted_credentials(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        with mock.patch("pangdrive.cli.config") as cfg:
            result = CliRunner().invoke(
                cli, ["auth", "web"], input="admin\nsecret\nsecret\n"
            )

        self.assertEqual(result.exit_code, 0, result.output)
        cfg.set_web_auth.assert_called_once_with("admin", "secret")
