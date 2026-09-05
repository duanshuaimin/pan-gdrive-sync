"""Test suite for pan-gdrive-sync 3 key improvements:
1. Baidu PCS multi-chunk sliced upload & createsuperfile (>2GB support)
2. Google Docs / Sheets / Slides virtual format detection & export
3. Scheduler run-due, headless daemon, and systemd service generation
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from click.testing import CliRunner

from pangdrive.baidu_client import BaiduClient
from pangdrive.config import Config
from pangdrive.gdrive_client import GoogleDriveClient
from pangdrive.storage import Storage
from pangdrive.transfer import TransferEngine
from pangdrive.web.task_manager import TaskManager
from pangdrive.cli import cli


class TestBaiduSlicedUpload(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.__new__(Config)
        self.cfg.data = {
            "baidu": {
                "bduss": "fake_bduss",
                "stoken": "fake_stoken",
                "app_id": "250528",
                "user_agent": "pan.baidu.com",
            }
        }
        self.client = BaiduClient(self.cfg)

    def test_upload_tmpfile_calls_api_and_returns_md5(self):
        with mock.patch.object(self.client.session, "post") as mock_post:
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"md5": "abc123md5", "request_id": 999}
            mock_post.return_value = mock_resp

            md5 = self.client.upload_tmpfile(b"test-chunk-data", filename="chunk1")

            self.assertEqual(md5, "abc123md5")
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertIn("method=upload", args[0])
            self.assertIn("type=tmpfile", args[0])
            self.assertIn("app_id=250528", args[0])
            self.assertEqual(kwargs["files"]["file"][0], "chunk1")
            self.assertEqual(kwargs["files"]["file"][1], b"test-chunk-data")

    def test_create_superfile_calls_api_with_block_list(self):
        with mock.patch.object(self.client.session, "post") as mock_post:
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "path": "/data/big.iso",
                "size": 3000000000,
                "md5": "supermd5",
            }
            mock_post.return_value = mock_resp

            res = self.client.create_superfile(
                "/data/big.iso", ["md5_1", "md5_2"], ondup="overwrite"
            )

            self.assertEqual(res["path"], "/data/big.iso")
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertIn("method=createsuperfile", args[0])
            self.assertIn("path=/data/big.iso", args[0])
            self.assertIn("ondup=overwrite", args[0])
            param = json.loads(kwargs["data"]["param"])
            self.assertEqual(param["block_list"], ["md5_1", "md5_2"])

    def test_upload_sliced_stream_slices_and_combines(self):
        data = b"A" * 100 + b"B" * 100 + b"C" * 50
        stream = io.BytesIO(data)

        with mock.patch.object(self.client, "mkdir"), \
             mock.patch.object(self.client, "upload_tmpfile") as mock_tmp, \
             mock.patch.object(self.client, "create_superfile") as mock_super:

            mock_tmp.side_effect = ["md5_part1", "md5_part2", "md5_part3"]
            mock_super.return_value = {"path": "/big.bin", "size": len(data), "status": "ok"}

            res = self.client.upload_sliced_stream(
                stream, "/big.bin", size=len(data), chunk_size=100, ondup="overwrite"
            )

            self.assertEqual(res["status"], "ok")
            self.assertEqual(mock_tmp.call_count, 3)
            mock_super.assert_called_once_with(
                "/big.bin", ["md5_part1", "md5_part2", "md5_part3"], ondup="overwrite"
            )

    def test_upload_stream_auto_delegates_to_sliced_when_size_exceeds_2gb(self):
        stream = io.BytesIO(b"fake")
        large_size = 3 * 1024 * 1024 * 1024  # 3 GB

        with mock.patch.object(self.client, "mkdir"), \
             mock.patch.object(self.client, "upload_sliced_stream") as mock_sliced:
            mock_sliced.return_value = {"path": "/huge.iso", "status": "ok"}

            res = self.client.upload_stream(stream, "/huge.iso", size=large_size)

            self.assertEqual(res["status"], "ok")
            mock_sliced.assert_called_once_with(
                stream, "/huge.iso", size=large_size, ondup="overwrite"
            )

    def test_upload_stream_force_sliced_flag(self):
        stream = io.BytesIO(b"small")
        with mock.patch.object(self.client, "mkdir"), \
             mock.patch.object(self.client, "upload_sliced_stream") as mock_sliced:
            mock_sliced.return_value = {"path": "/forced.iso", "status": "ok"}

            res = self.client.upload_stream(stream, "/forced.iso", size=1024, force_sliced=True)

            self.assertEqual(res["status"], "ok")
            mock_sliced.assert_called_once_with(
                stream, "/forced.iso", size=1024, ondup="overwrite"
            )

    def test_upload_tmpfile_retries_transient_5xx_then_succeeds(self):
        fail_resp = mock.MagicMock()
        fail_resp.status_code = 503
        ok_resp = mock.MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"md5": "recovered_md5", "request_id": 1}

        with mock.patch.object(self.client.session, "post") as mock_post, \
             mock.patch("pangdrive.baidu_client.time.sleep") as sleep:
            mock_post.side_effect = [fail_resp, ok_resp]

            md5 = self.client.upload_tmpfile(b"chunk-data", filename="part1")

        self.assertEqual(md5, "recovered_md5")
        self.assertEqual(mock_post.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_upload_tmpfile_raises_after_max_transient_retries(self):
        fail_resp = mock.MagicMock()
        fail_resp.status_code = 503
        fail_resp.raise_for_status.side_effect = requests.HTTPError("503 Server Error")

        with mock.patch.object(self.client.session, "post", return_value=fail_resp) as mock_post, \
             mock.patch("pangdrive.baidu_client.time.sleep"), \
             self.assertRaises(requests.HTTPError):
            self.client.upload_tmpfile(b"chunk-data", filename="part1")

        self.assertEqual(mock_post.call_count, 3)

    def test_upload_tmpfile_does_not_retry_auth_errors(self):
        auth_resp = mock.MagicMock()
        auth_resp.status_code = 401
        auth_resp.json.side_effect = ValueError("no json")
        auth_resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")

        with mock.patch.object(self.client.session, "post", return_value=auth_resp) as mock_post, \
             mock.patch("pangdrive.baidu_client.time.sleep") as sleep, \
             self.assertRaises(requests.HTTPError):
            self.client.upload_tmpfile(b"chunk-data", filename="part1")

        self.assertEqual(mock_post.call_count, 1)
        sleep.assert_not_called()

    def test_upload_sliced_stream_succeeds_when_block_upload_fails_once(self):
        data = b"A" * 100 + b"B" * 100
        stream = io.BytesIO(data)

        fail_resp = mock.MagicMock()
        fail_resp.status_code = 503
        ok_responses = []
        for md5 in ("md5_part1", "md5_part2"):
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"md5": md5, "request_id": 1}
            ok_responses.append(resp)
        super_resp = mock.MagicMock()
        super_resp.status_code = 200
        super_resp.json.return_value = {"path": "/big.bin", "size": len(data), "status": "ok"}

        with mock.patch.object(self.client, "mkdir"), \
             mock.patch.object(self.client.session, "post") as mock_post, \
             mock.patch("pangdrive.baidu_client.time.sleep"):
            mock_post.side_effect = [fail_resp, ok_responses[0], ok_responses[1], super_resp]

            res = self.client.upload_sliced_stream(
                stream, "/big.bin", size=len(data), chunk_size=100, ondup="overwrite"
            )

        self.assertEqual(res["status"], "ok")
        self.assertEqual(mock_post.call_count, 4)


class TestGoogleDocsExport(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.__new__(Config)
        self.cfg.data = {
            "gdrive": {
                "auth_mode": "oauth2",
                "access_token": "fake_access_token",
                "token_expiry": 9999999999,
            }
        }
        self.client = GoogleDriveClient(self.cfg)

    def test_is_google_doc_identification(self):
        self.assertTrue(self.client.is_google_doc("application/vnd.google-apps.document"))
        self.assertTrue(self.client.is_google_doc("application/vnd.google-apps.spreadsheet"))
        self.assertTrue(self.client.is_google_doc("application/vnd.google-apps.presentation"))
        self.assertTrue(self.client.is_google_doc("application/vnd.google-apps.drawing"))

        # Folder is not an exportable document
        self.assertFalse(self.client.is_google_doc("application/vnd.google-apps.folder"))
        # Standard binary files are not virtual google docs
        self.assertFalse(self.client.is_google_doc("application/pdf"))
        self.assertFalse(self.client.is_google_doc("image/jpeg"))
        self.assertFalse(self.client.is_google_doc(""))
        self.assertFalse(self.client.is_google_doc(None))

    def test_get_export_info_mapping(self):
        mime, ext = self.client.get_export_info("application/vnd.google-apps.document")
        self.assertEqual(mime, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(ext, ".docx")

        mime, ext = self.client.get_export_info("application/vnd.google-apps.spreadsheet")
        self.assertEqual(mime, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(ext, ".xlsx")

        mime, ext = self.client.get_export_info("application/vnd.google-apps.presentation")
        self.assertEqual(mime, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertEqual(ext, ".pptx")

        mime, ext = self.client.get_export_info("application/vnd.google-apps.drawing")
        self.assertEqual(mime, "image/png")
        self.assertEqual(ext, ".png")

    def test_download_stream_exports_virtual_google_doc(self):
        # 1. Mock file metadata response
        meta_resp = mock.MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "id": "doc123",
            "name": "Meeting Notes",
            "mimeType": "application/vnd.google-apps.document",
        }

        # 2. Mock export stream response
        export_resp = mock.MagicMock()
        export_resp.status_code = 200
        export_resp.headers = {"Content-Length": "4096"}
        export_resp.raw = io.BytesIO(b"docx-binary-content")

        with mock.patch.object(self.client.session, "get") as mock_get:
            mock_get.side_effect = [meta_resp, export_resp]

            resp, size, md5 = self.client.download_stream("doc123")

            self.assertEqual(resp, export_resp)
            self.assertEqual(size, 4096)
            self.assertEqual(md5, "")
            self.assertEqual(mock_get.call_count, 2)

            # Check that export endpoint was called
            export_call_url = mock_get.call_args_list[1][0][0]
            self.assertIn("/files/doc123/export", export_call_url)
            self.assertIn("mimeType=", export_call_url)

    def test_list_dir_flags_google_docs(self):
        list_resp = mock.MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "files": [
                {
                    "id": "id_doc",
                    "name": "Plan",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "id_sheet",
                    "name": "Budget",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                },
                {
                    "id": "id_file",
                    "name": "photo.jpg",
                    "mimeType": "image/jpeg",
                    "size": "5000",
                },
            ]
        }

        with mock.patch.object(self.client, "resolve_path", return_value="root_id"), \
             mock.patch.object(self.client.session, "get", return_value=list_resp):

            items = self.client.list_dir("/")

            self.assertEqual(len(items), 3)
            doc_item = next(it for it in items if it["id"] == "id_doc")
            sheet_item = next(it for it in items if it["id"] == "id_sheet")
            file_item = next(it for it in items if it["id"] == "id_file")

            self.assertTrue(doc_item.get("is_google_doc"))
            self.assertEqual(doc_item.get("export_ext"), ".docx")

            self.assertTrue(sheet_item.get("is_google_doc"))
            self.assertEqual(sheet_item.get("export_ext"), ".xlsx")

            self.assertFalse(file_item.get("is_google_doc", False))

    def test_transfer_engine_appends_extension_for_google_docs(self):
        engine = TransferEngine(baidu=mock.MagicMock(), gdrive=self.client)

        # Mock GDrive resolve_path and file lookup
        meta_search_resp = mock.MagicMock()
        meta_search_resp.status_code = 200
        meta_search_resp.json.return_value = {
            "files": [
                {
                    "id": "doc456",
                    "name": "Summary",
                    "mimeType": "application/vnd.google-apps.document",
                }
            ]
        }

        export_resp = mock.MagicMock()
        export_resp.status_code = 200
        export_resp.headers = {"Content-Length": "1234"}
        export_resp.raw = io.BytesIO(b"fake docx")

        with mock.patch.object(self.client, "resolve_path", return_value="parent_id"), \
             mock.patch.object(self.client.session, "get", return_value=meta_search_resp), \
             mock.patch.object(self.client, "download_stream", return_value=(export_resp, 1234, "")), \
             mock.patch.object(engine.baidu, "upload_stream") as mock_baidu_upload:

            mock_baidu_upload.return_value = {"path": "/backup/Summary.docx", "status": "ok"}

            res = engine.transfer_file(
                src_provider="gdrive",
                src_path="/Summary",
                dst_provider="baidu",
                dst_path="/backup/",
            )

            self.assertEqual(res["status"], "success")
            mock_baidu_upload.assert_called_once()
            call_dst_path = mock_baidu_upload.call_args[0][1]
            self.assertEqual(call_dst_path, "/backup/Summary.docx")


class TestGoogleDriveSharedNamespace(unittest.TestCase):
    def setUp(self):
        self.client = GoogleDriveClient.__new__(GoogleDriveClient)
        self.client.session = mock.MagicMock()
        self.client._get_headers = mock.MagicMock(return_value={})
        self.client._path_cache = {"/": "root"}

    def test_root_listing_excludes_shared_items(self):
        self.client._check = mock.MagicMock(return_value={"files": []})

        self.client.list_dir("/")

        query = self.client.session.get.call_args.kwargs["params"]["q"]
        self.assertEqual(query, "'root' in parents and trashed = false")
        self.assertNotIn("sharedWithMe", query)

    def test_shared_namespace_listing_queries_shared_items(self):
        self.client._check = mock.MagicMock(return_value={"files": []})

        self.client.list_dir("/__shared__")

        query = self.client.session.get.call_args.kwargs["params"]["q"]
        self.assertEqual(query, "sharedWithMe = true and trashed = false")

    def test_shared_namespace_resolution_uses_shared_query_for_first_component(self):
        self.client._check = mock.MagicMock(return_value={"files": [{"id": "shared-folder"}]})

        folder_id = self.client.resolve_path("/__shared__/Foo")

        self.assertEqual(folder_id, "shared-folder")
        query = self.client.session.get.call_args.kwargs["params"]["q"]
        self.assertEqual(
            query,
            "name = 'Foo' and sharedWithMe = true and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        )


class TestDaemonAndScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test_tasks.db")
        TaskManager.reset_instance_for_tests()
        Storage.reset_instance_for_tests()
        self.storage = Storage.get_instance(db_path=self.db_path)
        self.task_mgr = TaskManager.get_instance(db_path=self.db_path)

    def tearDown(self):
        TaskManager.reset_instance_for_tests()
        Storage.reset_instance_for_tests()
        self.tmp.cleanup()

    def test_run_due_jobs_executes_due_schedules(self):
        import time
        now = time.time()
        # Create a job that is already due (next_run_at in past)
        self.storage.create_job(
            job_id="job_due",
            name="Due Job",
            source="baidu:/src",
            dest="gdrive:/dst",
            interval_seconds=60,
        )
        self.storage.update_job("job_due", next_run_at=now - 10)

        # Create a job that is NOT due yet
        self.storage.create_job(
            job_id="job_future",
            name="Future Job",
            source="baidu:/src2",
            dest="gdrive:/dst2",
            interval_seconds=3600,
        )
        self.storage.update_job("job_future", next_run_at=now + 1800)

        with mock.patch.object(self.task_mgr, "trigger_job") as mock_trigger:
            mock_trigger.return_value = mock.MagicMock(id="task_123", source="baidu:/src", dest="gdrive:/dst")

            triggered = self.task_mgr.run_due_jobs()

            self.assertEqual(len(triggered), 1)
            mock_trigger.assert_called_once_with("job_due")

    def test_run_due_jobs_waits_for_transfer_and_advances_schedule_at_trigger(self):
        import time

        self.storage.create_job(
            job_id="job_wait",
            name="Wait for Job",
            source="baidu:/src",
            dest="gdrive:/dst",
            interval_seconds=60,
        )
        self.storage.update_job("job_wait", next_run_at=time.time() - 1)

        def slow_sync_directory(**kwargs):
            time.sleep(0.1)
            return {"total_bytes": 0}

        trigger_time = time.time()
        with mock.patch.object(TransferEngine, "sync_directory", side_effect=slow_sync_directory), \
             mock.patch.object(
                 self.storage,
                 "close_thread_connection",
                 wraps=self.storage.close_thread_connection,
             ) as close_connection:
            triggered = self.task_mgr.run_due_jobs()
            self.assertEqual(len(triggered), 1)
            self.task_mgr.wait_for_tasks(triggered)

        task = triggered[0]
        job = self.storage.get_job("job_wait")
        self.assertEqual(task.status, "completed")
        self.assertGreaterEqual(task.finished_at - task.started_at, 0.1)
        self.assertGreaterEqual(job["next_run_at"], trigger_time + 59.9)
        self.assertEqual(job["last_status"], "completed")
        close_connection.assert_called_once_with()

    def test_run_due_jobs_skips_job_with_running_task(self):
        import time

        self.storage.create_job(
            job_id="job_running",
            name="Already Running",
            source="baidu:/src",
            dest="gdrive:/dst",
            interval_seconds=60,
        )
        self.storage.update_job("job_running", next_run_at=time.time() - 1)
        running_task = mock.MagicMock(job_id="job_running", status="running")

        with mock.patch.object(self.task_mgr, "trigger_job") as mock_trigger, \
             mock.patch.object(self.task_mgr, "tasks", {"task_running": running_task}):
            self.assertEqual(self.task_mgr.run_due_jobs(), [])
            mock_trigger.assert_not_called()

    def test_cli_job_run_due(self):
        runner = CliRunner()
        with mock.patch("pangdrive.web.task_manager.TaskManager.get_instance") as mock_mgr_inst:
            mgr = mock.MagicMock()
            mock_mgr_inst.return_value = mgr
            mgr.run_due_jobs.return_value = [
                mock.MagicMock(id="task_001", source="baidu:/a", dest="gdrive:/b")
            ]

            result = runner.invoke(cli, ["job", "run-due"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Triggered job task", result.output)
            mgr.wait_for_tasks.assert_called_once_with(mgr.run_due_jobs.return_value)

    def test_cli_daemon_once_mode(self):
        runner = CliRunner()
        with mock.patch("pangdrive.web.task_manager.TaskManager.get_instance") as mock_mgr_inst:
            mgr = mock.MagicMock()
            mock_mgr_inst.return_value = mgr
            mgr.run_due_jobs.return_value = []

            result = runner.invoke(cli, ["daemon", "--once"])
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Checking scheduled sync jobs", result.output)
            self.assertIn("No scheduled jobs currently due", result.output)
            mgr.wait_for_tasks.assert_called_once_with([])

    def test_cli_daemon_once_exits_when_another_run_holds_lock(self):
        import fcntl

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp_home, \
             mock.patch("pathlib.Path.home", return_value=Path(tmp_home)), \
             mock.patch("fcntl.flock", side_effect=BlockingIOError), \
             mock.patch("pangdrive.web.task_manager.TaskManager.get_instance") as mock_mgr_inst:
            result = runner.invoke(cli, ["daemon", "--once"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("already running", result.output)
        mock_mgr_inst.assert_not_called()

    def test_cli_job_systemd_daemon_template(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["job", "systemd", "--mode", "daemon"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Description=Pan-GDrive-Sync Scheduler Daemon", result.output)
        self.assertIn("daemon", result.output)
        self.assertIn("[Install]", result.output)

    def test_cli_job_systemd_web_template(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["job", "systemd", "--mode", "web"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Description=Pan-GDrive-Sync Web UI Service", result.output)
        self.assertIn("web", result.output)

    def test_cli_job_systemd_write_mode(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp_home:
            with mock.patch("pathlib.Path.home", return_value=Path(tmp_home)):
                result = runner.invoke(cli, ["job", "systemd", "--mode", "daemon", "--write"])
                self.assertEqual(result.exit_code, 0)
                self.assertIn("Wrote systemd service unit to", result.output)
                unit_file = Path(tmp_home) / ".config" / "systemd" / "user" / "pgsync-daemon.service"
                self.assertTrue(unit_file.is_file())
                self.assertIn("Pan-GDrive-Sync Scheduler Daemon", unit_file.read_text())


if __name__ == "__main__":
    unittest.main()
