"""Baidu Netdisk API Client for cross-cloud transfers."""

import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO, Dict, Generator, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import config
from .utils import normalize_path


class BaiduClient:
    PCS_API = "https://pcs.baidu.com/rest/2.0/pcs"
    PAN_API = "https://pan.baidu.com/api"

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self.session = requests.Session()

        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._init_session()

    def _init_session(self):
        baidu_cfg = self.cfg.data.get("baidu", {})
        bduss = baidu_cfg.get("bduss", "").strip()
        stoken = baidu_cfg.get("stoken", "").strip()
        cookies = baidu_cfg.get("cookies", "").strip()

        cookie_items = {}
        if cookies:
            for item in cookies.split(";"):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    cookie_items[k.strip()] = v.strip()
        if bduss:
            cookie_items["BDUSS"] = bduss
        if stoken:
            cookie_items["STOKEN"] = stoken

        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_items.items())
        self.session.headers.update({
            "User-Agent": baidu_cfg.get("user_agent", config.data["baidu"]["user_agent"]),
            "Cookie": cookie_str,
        })

    def is_authenticated(self) -> bool:
        return bool(self.cfg.data.get("baidu", {}).get("bduss"))

    def _check(self, resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            return {}

        err_code = data.get("error_code") or data.get("errno", 0)
        if err_code != 0:
            msg = data.get("error_msg") or data.get("errmsg") or f"Baidu API error {err_code}"
            raise RuntimeError(f"Baidu Netdisk Error [{err_code}]: {msg}")
        return data

    def get_user_info(self) -> dict:
        url = f"{self.PAN_API}/user/getinfo"
        resp = self.session.get(url, params={"need_selfinfo": 1}, timeout=10)
        data = self._check(resp)
        records = data.get("records", [])
        return records[0] if records else {}

    def get_quota(self) -> dict:
        url = f"{self.PCS_API}/quota"
        params = {"method": "info", "app_id": self.cfg.data["baidu"]["app_id"]}
        resp = self.session.get(url, params=params, timeout=10)
        data = self._check(resp)
        total = data.get("quota", 0)
        used = data.get("used", 0)
        return {
            "total": total,
            "used": used,
            "free": max(0, total - used),
            "percent": round((used / total * 100) if total > 0 else 0, 2),
        }

    def list_dir(self, remote_path: str = "/") -> List[Dict[str, Any]]:
        path = normalize_path(remote_path)
        url = f"{self.PCS_API}/file"
        params = {
            "method": "list",
            "app_id": self.cfg.data["baidu"]["app_id"],
            "dir": path,
        }
        resp = self.session.get(url, params=params, timeout=25)
        data = self._check(resp)
        items = []
        for it in data.get("list", []):
            raw_p = it.get("path", "")
            items.append({
                "path": raw_p,
                "name": it.get("server_filename") or os.path.basename(raw_p),
                "isdir": bool(it.get("isdir", 0)),
                "size": it.get("size", 0),
                "mtime": it.get("mtime", 0),
                "md5": it.get("md5", ""),
                "fs_id": it.get("fs_id", 0),
            })
        return items

    def meta(self, paths: Union[str, List[str]]) -> List[Dict[str, Any]]:
        if isinstance(paths, str):
            paths = [paths]
        paths = [normalize_path(p) for p in paths]
        url = f"{self.PCS_API}/file"
        param_json = json.dumps({"list": [{"path": p} for p in paths]})
        resp = self.session.post(
            url,
            params={"method": "meta", "app_id": self.cfg.data["baidu"]["app_id"]},
            data={"param": param_json},
            timeout=15,
        )
        data = self._check(resp)
        return data.get("list", [])

    def mkdir(self, remote_path: str, parents: bool = True) -> Dict[str, Any]:
        path = normalize_path(remote_path)
        if path == "/":
            return {}
        url = f"{self.PCS_API}/file"
        params = {
            "method": "mkdir",
            "app_id": self.cfg.data["baidu"]["app_id"],
            "path": path,
        }
        resp = self.session.post(url, params=params, timeout=15)
        try:
            return self._check(resp)
        except RuntimeError as e:
            if "31061" in str(e):  # Already exists
                return {"path": path, "status": "already_exists"}
            if parents and ("31066" in str(e) or "-9" in str(e)):
                parent = os.path.dirname(path)
                if parent and parent != "/":
                    self.mkdir(parent, parents=True)
                    return self.mkdir(path, parents=False)
            raise

    def delete(self, paths: Union[str, List[str]]) -> Dict[str, Any]:
        if isinstance(paths, str):
            paths = [paths]
        paths = [normalize_path(p) for p in paths]
        url = f"{self.PCS_API}/file"
        param_json = json.dumps({"list": [{"path": p} for p in paths]})
        resp = self.session.post(
            url,
            params={"method": "delete", "app_id": self.cfg.data["baidu"]["app_id"]},
            data={"param": param_json},
            timeout=20,
        )
        return self._check(resp)

    def download_stream(self, remote_path: str) -> Tuple[requests.Response, int, str]:
        """Get streaming download response from Baidu Netdisk.

        Returns (response, size, md5).
        """
        path = normalize_path(remote_path)
        meta_list = self.meta(path)
        if not meta_list:
            raise FileNotFoundError(f"Remote Baidu file not found: {path}")

        m = meta_list[0]
        if m.get("isdir"):
            raise IsADirectoryError(f"Remote path is a directory: {path}")

        size = m.get("size", 0)
        md5 = m.get("md5", "")

        url = f"{self.PCS_API}/file?method=download&app_id={self.cfg.data['baidu']['app_id']}&path={urllib.parse.quote(path)}"
        resp = self.session.get(url, stream=True, timeout=60)
        if resp.status_code != 200:
            self._check(resp)

        return resp, size, md5

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_path: str,
        size: Optional[int] = None,
        ondup: str = "overwrite",
    ) -> Dict[str, Any]:
        """Stream upload to Baidu Netdisk."""
        path = normalize_path(remote_path)
        filename = os.path.basename(path)
        parent = os.path.dirname(path)
        if parent and parent != "/":
            self.mkdir(parent, parents=True)

        url = (
            f"{self.PCS_API}/file"
            f"?method=upload"
            f"&app_id={self.cfg.data['baidu']['app_id']}"
            f"&ondup={ondup}"
            f"&path={urllib.parse.quote(path)}"
        )
        files = {"file": (filename, stream)}
        resp = self.session.post(url, files=files, timeout=600)
        return self._check(resp)


