import base64
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pangdrive import paths
from pangdrive.config import Config
from pangdrive.storage import Storage
from pangdrive.transfer import TransferCancelledError, TransferEngine
from pangdrive.utils import escape_html


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

    def test_status_uses_auth_mode(self):
        from pangdrive.web.app import create_app

        cfg = self._config_with_web_auth()
        cfg.data["gdrive"]["auth_mode"] = "token"
        gdrive = mock.MagicMock()
        gdrive.is_authenticated.return_value = True
        gdrive.get_about.return_value = {
            "user": {"emailAddress": "user@example.test", "displayName": "User"},
            "total": 1,
            "used": 0,
            "free": 1,
            "percent": 0,
        }
        baidu = mock.MagicMock()
        baidu.is_authenticated.return_value = False
        token = base64.b64encode(b"admin:secret").decode()

        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.app.TaskManager.get_instance", return_value=mock.MagicMock()
        ), mock.patch("pangdrive.web.app.BaiduClient", return_value=baidu), mock.patch(
            "pangdrive.web.app.GoogleDriveClient", return_value=gdrive
        ):
            response = create_app().test_client().get(
                "/api/status", headers={"Authorization": f"Basic {token}"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["gdrive"]["type"], "token")

    def test_baidu_web_auth_restores_credentials_on_verify_failure(self):
        from pangdrive.web.app import create_app

        cfg = self._config_with_web_auth()
        cfg.data["baidu"] = {"bduss": "previous", "stoken": "old"}
        baidu = mock.MagicMock()
        baidu.get_user_info.side_effect = RuntimeError("invalid BDUSS")
        token = base64.b64encode(b"admin:secret").decode()

        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.app.TaskManager.get_instance", return_value=mock.MagicMock()
        ), mock.patch("pangdrive.web.app.BaiduClient", return_value=baidu):
            response = create_app().test_client().post(
                "/api/auth/baidu",
                json={"bduss": "invalid", "stoken": "new"},
                headers={"Authorization": f"Basic {token}"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(cfg.data["baidu"], {"bduss": "previous", "stoken": "old"})

    def test_baidu_cli_auth_does_not_persist_on_verify_failure(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        cfg = mock.MagicMock()
        cfg.data = {"baidu": {"bduss": "previous", "stoken": "old"}}
        baidu = mock.MagicMock()
        baidu.get_user_info.side_effect = RuntimeError("invalid BDUSS")

        with mock.patch("pangdrive.cli.config", cfg), mock.patch(
            "pangdrive.cli.BaiduClient", return_value=baidu
        ):
            result = CliRunner().invoke(cli, ["auth", "baidu", "--bduss", "invalid"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cfg.data["baidu"], {"bduss": "previous", "stoken": "old"})
        cfg.set_baidu.assert_not_called()

    def test_web_service_account_uses_central_path_and_restrictive_mode(self):
        import pangdrive.web.app as web_app

        cfg = self._config_with_web_auth()
        expected_path = Path(tempfile.mkdtemp()) / "service_account.json"
        gdrive = mock.MagicMock()
        gdrive.get_about.return_value = {"user": {"emailAddress": "sa@example.test"}}
        token = base64.b64encode(b"admin:secret").decode()

        with mock.patch.object(web_app, "config", cfg), mock.patch.object(
            web_app.TaskManager, "get_instance", return_value=mock.MagicMock()
        ), mock.patch.object(
            web_app, "GoogleDriveClient", return_value=gdrive
        ), mock.patch.object(
            web_app, "service_account_path", create=True, return_value=expected_path
        ), mock.patch.object(web_app.os, "chmod") as chmod:
            response = web_app.create_app().test_client().post(
                "/api/auth/gdrive",
                json={
                    "auth_type": "service_account",
                    "service_account_json": json.dumps(
                        {"client_email": "sa@example.test", "private_key": "private"}
                    ),
                },
                headers={"Authorization": f"Basic {token}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(cfg.data["gdrive"]["service_account_file"], str(expected_path))
        chmod.assert_any_call(expected_path, 0o600)


class TestEscapeHtml(unittest.TestCase):
    def test_escapes_all_special(self):
        self.assertEqual(
            escape_html("<script>alert('x')</script>\"&"),
            "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;&quot;&amp;",
        )


class TestDiskCacheCleanup(unittest.TestCase):
    def test_baidu_to_gdrive_cancel_during_disk_cache_download_removes_tmp(self):
        engine = TransferEngine.__new__(TransferEngine)
        engine.baidu = mock.MagicMock()
        engine.gdrive = mock.MagicMock()
        cancel = threading.Event()
        created = []
        real_named_temporary_file = tempfile.NamedTemporaryFile

        class FakeResponse:
            def iter_content(self, chunk_size=65536):
                yield b"abc"
                cancel.set()
                yield b"def"

        def create_tmp(*args, **kwargs):
            tmp_file = real_named_temporary_file(*args, **kwargs)
            created.append(tmp_file.name)
            return tmp_file

        engine.baidu.download_stream.return_value = (FakeResponse(), 6, "md5")

        with mock.patch("pangdrive.transfer.tempfile.NamedTemporaryFile", create_tmp):
            with self.assertRaises(TransferCancelledError):
                engine.transfer_file(
                    "baidu", "/source.bin", "gdrive", "/target.bin",
                    use_disk_cache=True, cancel_event=cancel,
                )

        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    def test_gdrive_to_baidu_cancel_during_disk_cache_download_removes_tmp(self):
        engine = TransferEngine.__new__(TransferEngine)
        engine.baidu = mock.MagicMock()
        engine.gdrive = mock.MagicMock()
        cancel = threading.Event()
        created = []
        real_named_temporary_file = tempfile.NamedTemporaryFile

        class FakeResponse:
            def iter_content(self, chunk_size=65536):
                yield b"abc"
                cancel.set()
                yield b"def"

        def create_tmp(*args, **kwargs):
            tmp_file = real_named_temporary_file(*args, **kwargs)
            created.append(tmp_file.name)
            return tmp_file

        engine.gdrive.resolve_path.return_value = "parent-id"
        engine.gdrive.session.get.return_value.json.return_value = {
            "files": [{"id": "file-id"}]
        }
        engine.gdrive.download_stream.return_value = (FakeResponse(), 6, "md5")

        with mock.patch("pangdrive.transfer.tempfile.NamedTemporaryFile", create_tmp):
            with self.assertRaises(TransferCancelledError):
                engine.transfer_file(
                    "gdrive", "/source.bin", "baidu", "/target.bin",
                    use_disk_cache=True, cancel_event=cancel,
                )

        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))
