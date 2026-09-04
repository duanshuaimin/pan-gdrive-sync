"""Flask Web Application for pan-gdrive-sync."""

import datetime
import json
import os
import queue
import time
from pathlib import Path
from typing import Any, Dict

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from ..baidu_client import BaiduClient
from ..config import config
from ..gdrive_client import GoogleDriveClient
from ..utils import format_size, normalize_path, split_storage_uri
from .task_manager import TaskManager


def format_timestamp(ts: Any) -> str:
    if not ts:
        return "-"
    try:
        if isinstance(ts, str) and "T" in ts:
            # ISO timestamp from Google Drive (e.g. 2026-09-04T10:00:00.000Z)
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        val = int(ts)
        return datetime.datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def create_app() -> Flask:
    static_folder = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, static_folder=static_folder, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False

    task_mgr = TaskManager.get_instance()

    @app.route("/")
    def index():
        return send_from_directory(static_folder, "index.html")

    # ==========================================
    # System & Authentication APIs
    # ==========================================

    @app.route("/api/status", methods=["GET"])
    def get_status():
        res = {
            "baidu": {"authenticated": False, "username": "-", "quota": None, "error": None},
            "gdrive": {"authenticated": False, "type": "-", "email": "-", "quota": None, "error": None},
        }

        # Check Baidu status
        try:
            baidu = BaiduClient(config)
            if baidu.is_authenticated():
                uinfo = baidu.get_user_info()
                quota = baidu.get_quota()
                res["baidu"] = {
                    "authenticated": True,
                    "username": uinfo.get("uname", "Baidu User"),
                    "uk": uinfo.get("uk"),
                    "vip_type": uinfo.get("vip_type", 0),
                    "vip_name": "超级会员" if uinfo.get("vip_type") == 2 else ("普通会员" if uinfo.get("vip_type") == 1 else "普通用户"),
                    "quota": {
                        "total": quota["total"],
                        "used": quota["used"],
                        "free": quota["free"],
                        "percent": quota["percent"],
                        "total_str": format_size(quota["total"]),
                        "used_str": format_size(quota["used"]),
                        "free_str": format_size(quota["free"]),
                    },
                }
        except Exception as e:
            res["baidu"]["error"] = str(e)

        # Check Google Drive status
        try:
            gdrive = GoogleDriveClient(config)
            if gdrive.is_authenticated():
                about = gdrive.get_about()
                user = about.get("user", {})
                res["gdrive"] = {
                    "authenticated": True,
                    "type": config.data.get("gdrive", {}).get("auth_type", "service_account"),
                    "email": user.get("emailAddress", "Connected"),
                    "display_name": user.get("displayName", "Google Account"),
                    "quota": {
                        "total": about["total"],
                        "used": about["used"],
                        "free": about["free"],
                        "percent": about["percent"],
                        "total_str": format_size(about["total"]) if about["total"] > 0 else "无限 (Unlimited)",
                        "used_str": format_size(about["used"]),
                        "free_str": format_size(about["free"]) if about["total"] > 0 else "-",
                    },
                }
        except Exception as e:
            res["gdrive"]["error"] = str(e)

        return jsonify(res)

    @app.route("/api/auth/baidu", methods=["POST"])
    def auth_baidu():
        data = request.json or {}
        bduss = data.get("bduss", "").strip()
        stoken = data.get("stoken", "").strip()
        cookies = data.get("cookies", "").strip()

        if cookies and not bduss:
            for item in cookies.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    if k.strip() == "BDUSS":
                        bduss = v.strip()
                    elif k.strip() == "STOKEN":
                        stoken = v.strip()

        if not bduss:
            return jsonify({"ok": False, "error": "未提供 BDUSS 或 Cookie 字符串"}), 400

        config.set_baidu(bduss=bduss, stoken=stoken, cookies=cookies)
        try:
            client = BaiduClient(config)
            info = client.get_user_info()
            return jsonify({"ok": True, "user": info.get("uname", "Baidu User")})
        except Exception as e:
            return jsonify({"ok": False, "error": f"百度网盘验证失败: {e}"}), 400

    @app.route("/api/auth/gdrive", methods=["POST"])
    def auth_gdrive():
        data = request.json or {}
        auth_type = data.get("auth_type", "service_account")

        try:
            if auth_type == "service_account":
                json_raw = data.get("service_account_json", "").strip()
                if not json_raw:
                    return jsonify({"ok": False, "error": "未提供 Service Account JSON 密钥"}), 400

                # Validate JSON format
                parsed = json.loads(json_raw)
                if "client_email" not in parsed or "private_key" not in parsed:
                    return jsonify({"ok": False, "error": "无效的 Google 服务账号 JSON 格式 (缺少 client_email 或 private_key)"}), 400

                # Save file to config directory
                key_path = os.path.expanduser("~/.config/pangdrive/service_account.json")
                os.makedirs(os.path.dirname(key_path), exist_ok=True)
                with open(key_path, "w", encoding="utf-8") as f:
                    f.write(json_raw)

                config.set_gdrive_service_account(key_path)

            elif auth_type == "token":
                token = data.get("token", "").strip()
                if not token:
                    return jsonify({"ok": False, "error": "未提供 Access Token"}), 400
                config.set_gdrive_token(token)
            else:
                return jsonify({"ok": False, "error": f"未知认证类型: {auth_type}"}), 400

            # Verify with client
            client = GoogleDriveClient(config)
            about = client.get_about()
            return jsonify({"ok": True, "email": about.get("user", {}).get("emailAddress", "Connected")})

        except Exception as e:
            return jsonify({"ok": False, "error": f"Google Drive 认证验证失败: {e}"}), 400

    # ==========================================
    # File Explorer APIs
    # ==========================================

    @app.route("/api/files", methods=["GET"])
    def list_files():
        drive = request.args.get("drive", "baidu").lower()
        path = normalize_path(request.args.get("path", "/"))

        try:
            if drive == "baidu":
                baidu = BaiduClient(config)
                raw_items = baidu.list_dir(path)
            elif drive == "gdrive":
                gdrive = GoogleDriveClient(config)
                raw_items = gdrive.list_dir(path)
            else:
                return jsonify({"ok": False, "error": f"不支持的网盘类型: {drive}"}), 400

            items = []
            for it in raw_items:
                is_dir = bool(it.get("isdir"))
                size = it.get("size", 0)
                items.append({
                    "name": it.get("name"),
                    "path": it.get("path"),
                    "isdir": is_dir,
                    "size": size,
                    "size_str": "-" if is_dir else format_size(size),
                    "mtime": it.get("mtime"),
                    "mtime_str": format_timestamp(it.get("mtime")),
                    "mime_type": it.get("mime_type", ""),
                    "id": it.get("id") or it.get("fs_id"),
                })

            # Sort: folders first, then alphabetical
            items.sort(key=lambda x: (0 if x["isdir"] else 1, x["name"].lower()))

            return jsonify({
                "ok": True,
                "drive": drive,
                "path": path,
                "items": items,
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e), "drive": drive, "path": path, "items": []}), 500

    @app.route("/api/files/mkdir", methods=["POST"])
    def make_directory():
        data = request.json or {}
        drive = data.get("drive", "baidu").lower()
        path = normalize_path(data.get("path", ""))

        if not path or path == "/":
            return jsonify({"ok": False, "error": "无效的目录路径"}), 400

        try:
            if drive == "baidu":
                baidu = BaiduClient(config)
                baidu.mkdir(path)
            elif drive == "gdrive":
                gdrive = GoogleDriveClient(config)
                gdrive.mkdir(path)
            else:
                return jsonify({"ok": False, "error": f"不支持的网盘类型: {drive}"}), 400

            return jsonify({"ok": True, "path": path})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/files/delete", methods=["POST"])
    def delete_file():
        data = request.json or {}
        drive = data.get("drive", "baidu").lower()
        path = normalize_path(data.get("path", ""))

        if not path or path == "/":
            return jsonify({"ok": False, "error": "不能删除根目录"}), 400

        try:
            if drive == "baidu":
                baidu = BaiduClient(config)
                baidu.delete(path)
            elif drive == "gdrive":
                gdrive = GoogleDriveClient(config)
                gdrive.delete(remote_path=path)
            else:
                return jsonify({"ok": False, "error": f"不支持的网盘类型: {drive}"}), 400

            return jsonify({"ok": True, "path": path})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ==========================================
    # Transfer & Tasks APIs
    # ==========================================

    @app.route("/api/transfer/start", methods=["POST"])
    def start_transfer():
        data = request.json or {}
        source = data.get("source", "").strip()
        dest = data.get("dest", "").strip()
        mode = data.get("mode", "copy").lower()  # "copy" or "sync"
        skip_existing = bool(data.get("skip_existing", True))
        recursive = bool(data.get("recursive", True))

        if not source or not dest:
            return jsonify({"ok": False, "error": "源地址与目的地址不能为空"}), 400

        try:
            split_storage_uri(source)
            split_storage_uri(dest)
        except Exception as e:
            return jsonify({"ok": False, "error": f"地址格式错误: {e}"}), 400

        task = task_mgr.create_task(
            source=source,
            dest=dest,
            mode=mode,
            skip_existing=skip_existing,
            recursive=recursive,
        )

        return jsonify({"ok": True, "task": task.to_dict()})

    @app.route("/api/tasks", methods=["GET"])
    def get_tasks():
        return jsonify({"ok": True, "tasks": task_mgr.get_all_tasks()})

    @app.route("/api/tasks/events", methods=["GET"])
    def task_events():
        def generate():
            q = task_mgr.subscribe()
            try:
                # Send immediate initial state
                initial_tasks = task_mgr.get_all_tasks()
                yield f"data: {json.dumps(initial_tasks)}\n\n"

                while True:
                    try:
                        tasks_data = q.get(timeout=15)
                        yield f"data: {json.dumps(tasks_data)}\n\n"
                    except queue.Empty:
                        # Keep-alive ping
                        yield ": keepalive\n\n"
            finally:
                task_mgr.unsubscribe(q)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
    def cancel_task(task_id: str):
        success = task_mgr.cancel_task(task_id)
        return jsonify({"ok": success})

    @app.route("/api/tasks/clear", methods=["POST"])
    def clear_tasks():
        task_mgr.clear_completed()
        return jsonify({"ok": True})

    return app
