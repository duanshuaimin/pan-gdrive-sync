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
        items = []
        limit = 1000
        start = 0
        while True:
            params = {
                "method": "list",
                "app_id": self.cfg.data["baidu"]["app_id"],
                "dir": path,
                "start": start,
                # PCS expects its limit window as "start-end".
                "limit": f"{start}-{start + limit}",
            }
            resp = self.session.get(url, params=params, timeout=25)
            data = self._check(resp)
            page = data.get("list", [])
            for it in page:
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
            if len(page) < limit:
                break
            start += len(page)
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

    BAIDU_MAX_SINGLE_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GB (Baidu PCS API single upload limit)
    BAIDU_DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024        # 16 MB per slice
    BAIDU_MAX_BLOCKS = 1024                            # PCS createsuperfile allows up to 1024 blocks

    def upload_tmpfile(self, chunk: bytes, filename: str = "chunk") -> str:
        """Upload a single sliced block as a temporary file to Baidu PCS.

        Returns MD5 checksum of the uploaded slice.
        """
        url = (
            f"{self.PCS_API}/file"
            f"?method=upload"
            f"&app_id={self.cfg.data['baidu']['app_id']}"
            f"&type=tmpfile"
        )
        files = {"file": (filename, chunk)}
        resp = self.session.post(url, files=files, timeout=300)
        data = self._check(resp)
        return data["md5"]

    def create_superfile(
        self,
        remote_path: str,
        block_list: List[str],
        ondup: str = "overwrite",
    ) -> Dict[str, Any]:
        """Combine uploaded sliced blocks into a full superfile on Baidu PCS."""
        path = normalize_path(remote_path)
        url = (
            f"{self.PCS_API}/file"
            f"?method=createsuperfile"
            f"&app_id={self.cfg.data['baidu']['app_id']}"
            f"&ondup={ondup}"
            f"&path={urllib.parse.quote(path)}"
        )
        param_json = json.dumps({"block_list": block_list})
        resp = self.session.post(url, data={"param": param_json}, timeout=120)
        return self._check(resp)

    def upload_sliced_stream(
        self,
        stream: BinaryIO,
        remote_path: str,
        size: Optional[int] = None,
        chunk_size: int = BAIDU_DEFAULT_CHUNK_SIZE,
        ondup: str = "overwrite",
    ) -> Dict[str, Any]:
        """Upload a large stream to Baidu PCS using sliced block upload and createsuperfile."""
        path = normalize_path(remote_path)
        filename = os.path.basename(path)
        parent = os.path.dirname(path)
        if parent and parent != "/":
            self.mkdir(parent, parents=True)

        # Dynamic chunk size calculation if size exceeds standard max blocks (1024)
        if size and size > chunk_size * self.BAIDU_MAX_BLOCKS:
            chunk_size = max(chunk_size, ((size // 1000) // (1024 * 1024) + 1) * 1024 * 1024)

        block_list: List[str] = []
        part_idx = 0

        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            part_idx += 1
            part_name = f"{filename}.part{part_idx}"
            md5_hash = self.upload_tmpfile(chunk, filename=part_name)
            block_list.append(md5_hash)

        if not block_list:
            url = (
                f"{self.PCS_API}/file"
                f"?method=upload"
                f"&app_id={self.cfg.data['baidu']['app_id']}"
                f"&ondup={ondup}"
                f"&path={urllib.parse.quote(path)}"
            )
            files = {"file": (filename, b"")}
            resp = self.session.post(url, files=files, timeout=60)
            return self._check(resp)

        if len(block_list) == 1:
            # createsuperfile requires at least 2 blocks; upload an empty terminator part
            empty_md5 = self.upload_tmpfile(b"", filename=f"{filename}.part_end")
            block_list.append(empty_md5)

        return self.create_superfile(path, block_list, ondup=ondup)

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_path: str,
        size: Optional[int] = None,
        ondup: str = "overwrite",
        force_sliced: bool = False,
    ) -> Dict[str, Any]:
        """Stream upload to Baidu Netdisk.

        Automatically uses sliced chunked upload (createsuperfile) when size > 2GB.
        """
        path = normalize_path(remote_path)
        filename = os.path.basename(path)
        parent = os.path.dirname(path)
        if parent and parent != "/":
            self.mkdir(parent, parents=True)

        if ondup == "skip":
            try:
                existing = self.meta(path)
            except Exception:
                existing = []
            else:
                if existing:
                    try:
                        if (
                            size is not None
                            and existing[0].get("size") is not None
                            and int(existing[0]["size"]) == size
                        ):
                            return {"path": path, "status": "skipped"}
                    except (TypeError, ValueError):
                        pass
            # Baidu PCS can create a duplicate "newcopy" for ondup=skip.  Once
            # this call proceeds, overwrite makes the operation a replacement.
            ondup = "overwrite"

        # If size exceeds 2GB (or explicitly requested), use sliced chunk upload
        if (size is not None and size > self.BAIDU_MAX_SINGLE_UPLOAD) or force_sliced:
            return self.upload_sliced_stream(stream, path, size=size, ondup=ondup)

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


