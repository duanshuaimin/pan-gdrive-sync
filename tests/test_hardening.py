import base64
import io
import json
import os
import sqlite3
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

    def test_migrate_service_account_repoints_legacy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_root = Path(tmp) / "pan-gdrive-sync"
            old_root = Path(tmp) / "pangdrive"
            new_root.mkdir()
            old_root.mkdir()
            old_service_account = old_root / "service_account.json"
            old_service_account.write_text("{}", encoding="utf-8")
            cfg = mock.MagicMock()
            cfg.data = {
                "gdrive": {"service_account_file": str(old_service_account)}
            }
            with mock.patch.object(paths, "CONFIG_DIR", new_root), mock.patch.object(
                paths, "LEGACY_CONFIG_DIR", old_root
            ), mock.patch("pangdrive.config.config", cfg):
                paths.migrate_legacy_artifacts()

            expected_path = new_root / "service_account.json"
            self.assertTrue(expected_path.is_file())
            self.assertEqual(
                cfg.data["gdrive"]["service_account_file"], str(expected_path)
            )
            cfg.save.assert_called_once()

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

    def test_storage_reset_closes_singleton_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            Storage.reset_instance_for_tests()
            storage = Storage.get_instance(db_path=str(Path(tmp) / "tasks.db"))
            connection = storage._get_connection()

            Storage.reset_instance_for_tests()

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


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
            self.assertEqual(response.status_code, 200)

    def test_transfer_and_job_apis_reject_invalid_mode(self):
        from pangdrive.web.app import create_app

        cfg = self._config_with_web_auth()
        task_manager = mock.MagicMock()
        token = base64.b64encode(b"admin:secret").decode()
        headers = {"Authorization": f"Basic {token}"}
        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.app.TaskManager.get_instance", return_value=task_manager
        ):
            client = create_app().test_client()
            for endpoint, payload in (
                (
                    "/api/transfer/start",
                    {
                        "source": "baidu:/source",
                        "dest": "gdrive:/dest",
                        "mode": "unexpected",
                    },
                ),
                (
                    "/api/jobs",
                    {
                        "name": "job",
                        "source": "baidu:/source",
                        "dest": "gdrive:/dest",
                        "mode": "unexpected",
                    },
                ),
                ("/api/jobs/job-id", {"mode": "unexpected"}),
            ):
                response = client.open(endpoint, method="PUT" if "job-id" in endpoint else "POST",
                                       json=payload, headers=headers)
                self.assertEqual(response.status_code, 400)
        task_manager.create_task.assert_not_called()
        task_manager.create_job.assert_not_called()
        task_manager.update_job.assert_not_called()

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

        self.assertEqual(result.exit_code, 1, result.output)
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


class TestCliSchedulingHelp(unittest.TestCase):
    def test_job_add_interval_help_explains_web_scheduler_requirement(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        result = CliRunner().invoke(cli, ["job", "add", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertRegex(
            result.output,
            r"only while the web\s+server is running",
        )


class TestTaskManagerIsolation(unittest.TestCase):
    def test_task_manager_exposes_test_reset_helper(self):
        from pangdrive.web.task_manager import TaskManager

        self.assertTrue(hasattr(TaskManager, "reset_instance_for_tests"))


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


class TestDriveEscape(unittest.TestCase):
    def test_escapes_backslashes_and_quotes(self):
        from pangdrive.utils import escape_drive_query_value

        self.assertEqual(
            escape_drive_query_value(r"folder\O'Brien"),
            r"folder\\O\'Brien",
        )


class TestDirectoryPagination(unittest.TestCase):
    def test_gdrive_list_dir_follows_next_page_token(self):
        from pangdrive.gdrive_client import GoogleDriveClient

        client = GoogleDriveClient.__new__(GoogleDriveClient)
        client.session = mock.MagicMock()
        client._get_headers = mock.MagicMock(return_value={})
        client._check = mock.MagicMock(
            side_effect=[
                {
                    "files": [
                        {
                            "id": "one",
                            "name": "one.txt",
                            "mimeType": "text/plain",
                            "size": "1",
                        }
                    ],
                    "nextPageToken": "next-page",
                },
                {
                    "files": [
                        {
                            "id": "two",
                            "name": "two.txt",
                            "mimeType": "text/plain",
                            "size": "2",
                        }
                    ]
                },
            ]
        )

        items = client.list_dir("/source", folder_id="folder-id")

        self.assertEqual([item["name"] for item in items], ["one.txt", "two.txt"])
        self.assertEqual(client.session.get.call_count, 2)
        self.assertNotIn("pageToken", client.session.get.call_args_list[0].kwargs["params"])
        self.assertEqual(
            client.session.get.call_args_list[1].kwargs["params"]["pageToken"],
            "next-page",
        )

    def test_baidu_list_dir_follows_start_and_limit(self):
        from pangdrive.baidu_client import BaiduClient

        client = BaiduClient.__new__(BaiduClient)
        client.cfg = mock.MagicMock()
        client.cfg.data = {"baidu": {"app_id": "app-id"}}
        client.session = mock.MagicMock()
        client._check = mock.MagicMock(
            side_effect=[
                {"list": [{"path": "/source/one.txt", "size": 1}] * 1000},
                {"list": [{"path": "/source/two.txt", "size": 2}]},
            ]
        )

        items = client.list_dir("/source")

        self.assertEqual(len(items), 1001)
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(client.session.get.call_args_list[0].kwargs["params"]["start"], 0)
        self.assertEqual(client.session.get.call_args_list[1].kwargs["params"]["start"], 1000)


class TestSkipBySize(unittest.TestCase):
    def test_gdrive_upload_skip_requires_known_equal_size(self):
        from pangdrive.gdrive_client import GoogleDriveClient

        client = GoogleDriveClient.__new__(GoogleDriveClient)
        client.session = mock.MagicMock()
        client.resolve_path = mock.MagicMock(return_value="parent-id")
        client._get_headers = mock.MagicMock(return_value={})
        client.delete = mock.MagicMock()
        client._check = mock.MagicMock(
            side_effect=[
                {"files": [{"id": "old-id", "size": "9"}]},
                {"id": "new-id"},
            ]
        )
        client.session.post.return_value.status_code = 200
        client.session.post.return_value.headers = {"Location": "https://upload.example"}

        result = client.upload_stream(
            io.BytesIO(b"0123456789"),
            "/dest/file.txt",
            size=10,
            ondup="skip",
        )

        self.assertEqual(result, {"id": "new-id"})
        client.delete.assert_called_once_with(file_id="old-id")
        client.session.post.assert_called_once()

    def test_gdrive_upload_skip_returns_skipped_for_equal_size(self):
        from pangdrive.gdrive_client import GoogleDriveClient

        client = GoogleDriveClient.__new__(GoogleDriveClient)
        client.session = mock.MagicMock()
        client.resolve_path = mock.MagicMock(return_value="parent-id")
        client._get_headers = mock.MagicMock(return_value={})
        client.delete = mock.MagicMock()
        client._check = mock.MagicMock(
            return_value={"files": [{"id": "old-id", "size": "10"}]}
        )

        result = client.upload_stream(
            io.BytesIO(b"0123456789"),
            "/dest/file.txt",
            size=10,
            ondup="skip",
        )

        self.assertEqual(
            result, {"id": "old-id", "name": "file.txt", "status": "skipped"}
        )
        client.delete.assert_not_called()
        client.session.post.assert_not_called()

    def test_baidu_upload_skip_replaces_size_mismatch(self):
        from pangdrive.baidu_client import BaiduClient

        client = BaiduClient.__new__(BaiduClient)
        client.cfg = mock.MagicMock()
        client.cfg.data = {"baidu": {"app_id": "app-id"}}
        client.session = mock.MagicMock()
        client.meta = mock.MagicMock(return_value=[{"size": 9, "isdir": 0}])
        client._check = mock.MagicMock(return_value={"path": "/dest/file.txt"})

        client.upload_stream(
            io.BytesIO(b"0123456789"),
            "/dest/file.txt",
            size=10,
            ondup="skip",
        )

        self.assertIn("ondup=overwrite", client.session.post.call_args.args[0])

    def test_transfer_skip_checks_baidu_source_size_before_destination(self):
        engine = TransferEngine.__new__(TransferEngine)
        engine.baidu = mock.MagicMock()
        engine.gdrive = mock.MagicMock()
        engine.baidu.meta.return_value = [{"size": 10, "isdir": False}]
        engine.gdrive.resolve_path.return_value = "parent-id"
        engine.gdrive.session.get.return_value.json.return_value = {
            "files": [{"id": "old-id", "size": "9"}]
        }
        response = mock.MagicMock()
        response.raw = io.BytesIO(b"0123456789")
        engine.baidu.download_stream.return_value = (response, 10, "")

        engine.transfer_file(
            "baidu", "/source/file.txt", "gdrive", "/dest/file.txt", ondup="skip"
        )

        engine.baidu.meta.assert_called_once_with("/source/file.txt")
        engine.gdrive.upload_stream.assert_called_once()
        self.assertEqual(
            engine.gdrive.upload_stream.call_args.kwargs["ondup"], "overwrite"
        )

    def test_transfer_skip_meta_failure_still_copies(self):
        engine = TransferEngine.__new__(TransferEngine)
        engine.baidu = mock.MagicMock()
        engine.gdrive = mock.MagicMock()
        engine.baidu.meta.side_effect = RuntimeError("Baidu metadata unavailable")
        response = mock.MagicMock()
        response.raw = io.BytesIO(b"0123456789")
        engine.baidu.download_stream.return_value = (response, 10, "")

        result = engine.transfer_file(
            "baidu", "/source/file.txt", "gdrive", "/dest/file.txt", ondup="skip"
        )

        self.assertEqual(result["status"], "success")
        engine.gdrive.upload_stream.assert_called_once()


class TestSyncDiskCacheAndHistory(unittest.TestCase):
    def test_sync_passes_disk_cache_to_each_file_transfer(self):
        engine = TransferEngine.__new__(TransferEngine)
        engine.baidu = mock.MagicMock()
        engine.gdrive = mock.MagicMock()
        engine.baidu.list_dir.return_value = [
            {"path": "/source/file.txt", "name": "file.txt", "isdir": False, "size": 1}
        ]
        engine.transfer_file = mock.MagicMock(return_value={"status": "success"})

        engine.sync_directory(
            "baidu",
            "/source",
            "gdrive",
            "/dest",
            use_disk_cache=True,
            show_console_progress=False,
        )

        self.assertTrue(engine.transfer_file.call_args.kwargs["use_disk_cache"])

    def test_sync_cli_accepts_disk_cache_flag(self):
        from click.testing import CliRunner
        from pangdrive.cli import cli

        engine = mock.MagicMock()
        with mock.patch("pangdrive.cli.TransferEngine", return_value=engine):
            result = CliRunner().invoke(
                cli, ["sync", "baidu:/source", "gdrive:/dest", "--disk-cache"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(engine.sync_directory.call_args.kwargs["use_disk_cache"])

    def test_history_clear_removes_only_finished_tasks(self):
        from pangdrive.web.app import create_app

        cfg = TestWebBasicAuth()._config_with_web_auth()
        task_manager = mock.MagicMock()
        token = base64.b64encode(b"admin:secret").decode()
        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.app.TaskManager.get_instance", return_value=task_manager
        ):
            response = create_app().test_client().post(
                "/api/history/clear", headers={"Authorization": f"Basic {token}"}
            )

        self.assertEqual(response.status_code, 200)
        task_manager.storage.clear_tasks.assert_called_with(only_finished=True)
