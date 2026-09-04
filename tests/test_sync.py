"""Test suite for pan-gdrive-sync."""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pangdrive.baidu_client import BaiduClient
from pangdrive.config import config
from pangdrive.gdrive_client import GoogleDriveClient
from pangdrive.transfer import ProgressStreamWrapper, TransferDirection, TransferEngine
from pangdrive.utils import format_size, guess_mime_type, normalize_path, split_storage_uri


class TestPanGDriveSync(unittest.TestCase):
    def test_01_utils(self):
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024 * 1024 * 5), "5.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 1024 * 2.5), "2.5 GB")

        self.assertEqual(normalize_path("a/b/c"), "/a/b/c")
        self.assertEqual(normalize_path("/a//b///c/"), "/a/b/c")
        self.assertEqual(normalize_path(""), "/")

        prov, path = split_storage_uri("baidu:/my/folder")
        self.assertEqual(prov, "baidu")
        self.assertEqual(path, "/my/folder")

        prov, path = split_storage_uri("gdrive:/backup/doc.pdf")
        self.assertEqual(prov, "gdrive")
        self.assertEqual(path, "/backup/doc.pdf")

        self.assertEqual(
            guess_mime_type("test.docx"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(guess_mime_type("test.pdf"), "application/pdf")
        print("Test 01 passed: Utils validation")

    def test_02_baidu_live_connection(self):
        baidu = BaiduClient()
        self.assertTrue(baidu.is_authenticated())

        info = baidu.get_user_info()
        self.assertIn("uname", info)
        print(f"Test 02 passed: Baidu User {info.get('uname')}")

        quota = baidu.get_quota()
        self.assertGreater(quota["total"], 0)
        self.assertGreaterEqual(quota["used"], 0)
        print(f"Test 02 passed: Baidu Quota {format_size(quota['used'])} / {format_size(quota['total'])}")

        items = baidu.list_dir("/2015-2026语文中考真题")
        self.assertGreater(len(items), 0)
        print(f"Test 02 passed: Baidu list_dir returned {len(items)} items")

    def test_03_progress_stream_wrapper(self):
        payload = b"Hello Pan-GDrive Streaming Pipe 2026"
        raw_io = io.BytesIO(payload)
        wrapper = ProgressStreamWrapper(raw_io, total=len(payload))

        read_back = bytearray()
        while True:
            chunk = wrapper.read(8)
            if not chunk:
                break
            read_back.extend(chunk)

        self.assertEqual(bytes(read_back), payload)
        self.assertEqual(wrapper.read_bytes, len(payload))
        print("Test 03 passed: ProgressStreamWrapper streaming pipe")

    def test_04_gdrive_structure(self):
        gdrive = GoogleDriveClient()
        self.assertIn("/", gdrive._path_cache)
        self.assertEqual(gdrive._path_cache["/"], "root")
        print("Test 04 passed: Google Drive client initialized properly")

    def test_05_web_endpoints(self):
        from pangdrive.web.app import create_app
        from pangdrive.web.task_manager import TaskManager

        app = create_app()
        client = app.test_client()

        # 1. Test index page
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"PanGDrive Sync", res.data)

        # 2. Test static assets
        res_css = client.get("/static/style.css")
        self.assertEqual(res_css.status_code, 200)
        res_js = client.get("/static/app.js")
        self.assertEqual(res_js.status_code, 200)

        # 3. Test /api/status
        res_status = client.get("/api/status")
        self.assertEqual(res_status.status_code, 200)
        status_data = res_status.get_json()
        self.assertIn("baidu", status_data)
        self.assertIn("gdrive", status_data)
        self.assertTrue(status_data["baidu"]["authenticated"])

        # 4. Test /api/files listing for baidu
        res_files = client.get("/api/files?drive=baidu&path=/")
        self.assertEqual(res_files.status_code, 200)
        files_data = res_files.get_json()
        self.assertTrue(files_data["ok"])
        self.assertIsInstance(files_data["items"], list)

        # 5. Test /api/tasks
        res_tasks = client.get("/api/tasks")
        self.assertEqual(res_tasks.status_code, 200)
        tasks_data = res_tasks.get_json()
        self.assertTrue(tasks_data["ok"])

        # 6. Test TaskManager functionality
        tm = TaskManager.get_instance()
        all_tasks = tm.get_all_tasks()
        self.assertIsInstance(all_tasks, list)

        print("Test 05 passed: Web UI and REST API endpoints functional")


if __name__ == "__main__":
    unittest.main()
