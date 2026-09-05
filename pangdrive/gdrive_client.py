"""Google Drive REST API v3 Client."""

import base64
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import config
from .utils import escape_drive_query_value, guess_mime_type, normalize_path


class GoogleDriveClient:
    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
    UPLOAD_API_BASE = "https://www.googleapis.com/upload/drive/v3"
    OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    SCOPES = "https://www.googleapis.com/auth/drive"
    FOLDER_MIME = "application/vnd.google-apps.folder"

    GOOGLE_DOCS_EXPORT_MAP = {
        "application/vnd.google-apps.document": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
        ),
        "application/vnd.google-apps.spreadsheet": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
        "application/vnd.google-apps.presentation": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
        ),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    }

    def is_google_doc(self, mime_type: Optional[str]) -> bool:
        """Check if a mime type represents a virtual Google Doc / Sheet / Slide."""
        if not mime_type:
            return False
        return mime_type.startswith("application/vnd.google-apps.") and mime_type != self.FOLDER_MIME

    def get_export_info(self, mime_type: str) -> Tuple[str, str]:
        """Returns (export_mime_type, file_extension) for a Google Doc format."""
        return self.GOOGLE_DOCS_EXPORT_MAP.get(mime_type, ("application/pdf", ".pdf"))

    def __init__(self, cfg=None):
        self.cfg = cfg or config
        self.session = requests.Session()

        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._path_cache: Dict[str, str] = {"/": "root"}

    def is_authenticated(self) -> bool:
        gcfg = self.cfg.data.get("gdrive", {})
        mode = gcfg.get("auth_mode", "oauth2")
        if mode == "service_account":
            sa_file = gcfg.get("service_account_file")
            return bool(sa_file and Path(sa_file).is_file())
        elif mode == "oauth2":
            return bool(gcfg.get("refresh_token") or gcfg.get("access_token"))
        elif mode == "token":
            return bool(gcfg.get("access_token"))
        return False

    def _get_access_token(self) -> str:
        gcfg = self.cfg.data.get("gdrive", {})
        mode = gcfg.get("auth_mode", "oauth2")
        now = int(time.time())

        # Check if cached access_token is still valid (buffer 60 seconds)
        if gcfg.get("access_token") and gcfg.get("token_expiry", 0) > now + 60:
            return gcfg["access_token"]

        if mode == "service_account":
            return self._refresh_service_account_token()
        elif mode == "oauth2" and gcfg.get("refresh_token"):
            return self._refresh_oauth2_token()
        elif gcfg.get("access_token"):
            return gcfg["access_token"]
        else:
            raise RuntimeError(
                "Google Drive not authenticated. Use 'pan-gdrive-sync auth gdrive' to configure service account or tokens."
            )

    def _refresh_service_account_token(self) -> str:
        """Generate JWT signed with service account RSA key and exchange for OAuth token."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        sa_path = self.cfg.data["gdrive"]["service_account_file"]
        with open(sa_path, "r", encoding="utf-8") as f:
            key_data = json.load(f)

        client_email = key_data["client_email"]
        private_key_pem = key_data["private_key"]
        private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss": client_email,
            "scope": self.SCOPES,
            "aud": self.OAUTH_TOKEN_URL,
            "exp": now + 3600,
            "iat": now,
        }

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        encoded_header = b64url(json.dumps(header).encode("utf-8"))
        encoded_payload = b64url(json.dumps(payload).encode("utf-8"))
        signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")

        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        encoded_sig = b64url(signature)
        assertion = f"{encoded_header}.{encoded_payload}.{encoded_sig}"

        resp = requests.post(
            self.OAUTH_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=15,
        )
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Google Service Account token exchange failed: {data}")

        token = data["access_token"]
        self.cfg.data["gdrive"]["access_token"] = token
        self.cfg.data["gdrive"]["token_expiry"] = now + data.get("expires_in", 3600)
        self.cfg.save()
        return token

    def _refresh_oauth2_token(self) -> str:
        gcfg = self.cfg.data["gdrive"]
        resp = requests.post(
            self.OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": gcfg["refresh_token"],
                "client_id": gcfg.get("client_id", ""),
                "client_secret": gcfg.get("client_secret", ""),
            },
            timeout=15,
        )
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"OAuth2 refresh failed: {data}")

        token = data["access_token"]
        now = int(time.time())
        self.cfg.data["gdrive"]["access_token"] = token
        self.cfg.data["gdrive"]["token_expiry"] = now + data.get("expires_in", 3600)
        self.cfg.save()
        return token

    def _get_headers(self) -> Dict[str, str]:
        token = self._get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _check(self, resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            return {}

        if "error" in data:
            err = data["error"]
            code = err.get("code", resp.status_code)
            msg = err.get("message", "Unknown error")
            raise RuntimeError(f"Google Drive Error [{code}]: {msg}")
        return data

    def get_about(self) -> dict:
        url = f"{self.DRIVE_API_BASE}/about"
        resp = self.session.get(url, headers=self._get_headers(), params={"fields": "storageQuota,user"}, timeout=10)
        data = self._check(resp)
        quota = data.get("storageQuota", {})
        limit = int(quota.get("limit", 0))
        usage = int(quota.get("usage", 0))
        free = max(0, limit - usage) if limit > 0 else 0
        percent = round((usage / limit * 100) if limit > 0 else 0, 2)
        return {
            "user": data.get("user", {}),
            "total": limit,
            "used": usage,
            "free": free,
            "percent": percent,
        }

    def resolve_path(self, path: str, create_missing: bool = False) -> str:
        """Resolve a POSIX path like '/Folder/Subfolder' into Google Drive folder ID.

        If create_missing is True, non-existent directories will be created.
        """
        path = normalize_path(path)
        cfg = getattr(self, "cfg", None)
        root_folder = (cfg.data.get("gdrive", {}).get("root_folder_id") if cfg and hasattr(cfg, "data") else None) or "root"
        if path == "/":
            return root_folder
        if path in self._path_cache:
            return self._path_cache[path]

        parts = [p for p in path.strip("/").split("/") if p]
        curr_id = root_folder
        curr_path = ""

        for part in parts:
            curr_path += "/" + part
            if curr_path in self._path_cache:
                curr_id = self._path_cache[curr_path]
                continue

            if curr_id in ("root", root_folder):
                query = (
                    f"name = '{escape_drive_query_value(part)}' and ('{curr_id}' in parents or sharedWithMe = true) "
                    f"and mimeType = '{self.FOLDER_MIME}' and trashed = false"
                )
            else:
                query = (
                    f"name = '{escape_drive_query_value(part)}' and '{curr_id}' in parents "
                    f"and mimeType = '{self.FOLDER_MIME}' and trashed = false"
                )
            url = f"{self.DRIVE_API_BASE}/files"
            resp = self.session.get(
                url,
                headers=self._get_headers(),
                params={
                    "q": query,
                    "fields": "files(id, name)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                },
                timeout=15,
            )
            data = self._check(resp)
            files = data.get("files", [])
            if files:
                curr_id = files[0]["id"]
            elif create_missing:
                # Create folder
                folder_meta = {
                    "name": part,
                    "mimeType": self.FOLDER_MIME,
                    "parents": [curr_id],
                }
                c_resp = self.session.post(
                    f"{self.DRIVE_API_BASE}/files?supportsAllDrives=true",
                    headers={**self._get_headers(), "Content-Type": "application/json"},
                    data=json.dumps(folder_meta),
                    timeout=15,
                )
                c_data = self._check(c_resp)
                curr_id = c_data["id"]
            else:
                raise FileNotFoundError(f"Google Drive directory not found: {curr_path}")

            self._path_cache[curr_path] = curr_id

        return curr_id

    def list_dir(self, remote_path: str = "/", folder_id: Optional[str] = None) -> List[Dict[str, Any]]:
        path = normalize_path(remote_path)
        target_id = folder_id or self.resolve_path(path)
        cfg = getattr(self, "cfg", None)
        root_folder = (cfg.data.get("gdrive", {}).get("root_folder_id") if cfg and hasattr(cfg, "data") else None) or "root"

        url = f"{self.DRIVE_API_BASE}/files"
        if target_id in ("root", root_folder):
            query = f"('{target_id}' in parents or sharedWithMe = true) and trashed = false"
        else:
            query = f"'{target_id}' in parents and trashed = false"
        items = []
        page_token = None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime, createdTime)",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=25)
            data = self._check(resp)
            for f in data.get("files", []):
                mime = f.get("mimeType", "")
                is_dir = mime == self.FOLDER_MIME
                is_doc = self.is_google_doc(mime)
                item_path = f"{path}/{f['name']}".replace("//", "/")
                item_dict = {
                    "id": f["id"],
                    "name": f["name"],
                    "path": item_path,
                    "isdir": is_dir,
                    "size": int(f.get("size") or 0),
                    "md5": f.get("md5Checksum", ""),
                    "mtime": f.get("modifiedTime", ""),
                    "mime_type": mime,
                }
                if is_doc:
                    _, ext = self.get_export_info(mime)
                    item_dict["is_google_doc"] = True
                    item_dict["export_ext"] = ext
                items.append(item_dict)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        # Sort: directories first, then alphabetically
        items.sort(key=lambda x: (0 if x["isdir"] else 1, x["name"].lower()))
        return items

    def mkdir(self, remote_path: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        path = normalize_path(remote_path)
        folder_id = self.resolve_path(path, create_missing=True)
        return {"id": folder_id, "path": path}

    def delete(self, file_id: Optional[str] = None, remote_path: Optional[str] = None) -> bool:
        if not file_id and remote_path:
            norm_p = normalize_path(remote_path)
            parent_p = os.path.dirname(norm_p) or "/"
            filename = os.path.basename(norm_p)
            parent_id = self.resolve_path(parent_p)
            q = f"name = '{escape_drive_query_value(filename)}' and '{parent_id}' in parents and trashed = false"
            resp = self.session.get(
                f"{self.DRIVE_API_BASE}/files",
                headers=self._get_headers(),
                params={
                    "q": q,
                    "fields": "files(id)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                },
                timeout=15,
            )
            files = self._check(resp).get("files", [])
            if not files:
                return False
            file_id = files[0]["id"]

        if not file_id:
            raise ValueError("Either file_id or remote_path must be provided to delete")

        url = f"{self.DRIVE_API_BASE}/files/{file_id}?supportsAllDrives=true"
        resp = self.session.delete(url, headers=self._get_headers(), timeout=15)
        if resp.status_code in (200, 204):
            return True
        self._check(resp)
        return True

    def download_stream(self, file_id: str) -> Tuple[requests.Response, int, str]:
        """Get streaming download response from Google Drive.

        Returns (response, size, md5). Automatically exports virtual Google Docs formats.
        """
        # Fetch file metadata first
        meta_url = f"{self.DRIVE_API_BASE}/files/{file_id}"
        meta_resp = self.session.get(
            meta_url,
            headers=self._get_headers(),
            params={
                "fields": "id, name, size, md5Checksum, mimeType",
                "supportsAllDrives": "true",
            },
            timeout=15,
        )
        meta = self._check(meta_resp)
        mime = meta.get("mimeType", "")
        if mime == self.FOLDER_MIME:
            raise IsADirectoryError(f"Google Drive item is a folder: {meta.get('name')}")

        if self.is_google_doc(mime):
            export_mime, _ext = self.get_export_info(mime)
            url = f"{self.DRIVE_API_BASE}/files/{file_id}/export?mimeType={urllib.parse.quote(export_mime)}&supportsAllDrives=true"
            resp = self.session.get(url, headers=self._get_headers(), stream=True, timeout=60)
            if resp.status_code != 200:
                self._check(resp)
            cl = resp.headers.get("Content-Length")
            size = int(cl) if cl and cl.isdigit() else 0
            return resp, size, ""

        size = int(meta.get("size", 0))
        md5 = meta.get("md5Checksum", "")

        url = f"{self.DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        resp = self.session.get(url, headers=self._get_headers(), stream=True, timeout=60)
        if resp.status_code != 200:
            self._check(resp)

        return resp, size, md5

    def upload_stream(
        self,
        stream: BinaryIO,
        remote_path: str,
        size: Optional[int] = None,
        mime_type: Optional[str] = None,
        ondup: str = "overwrite",
    ) -> Dict[str, Any]:
        """Upload a file or stream to Google Drive."""
        path = normalize_path(remote_path)
        filename = os.path.basename(path)
        parent_path = os.path.dirname(path) or "/"
        parent_id = self.resolve_path(parent_path, create_missing=True)

        if not mime_type:
            mime_type = guess_mime_type(filename)

        # Check existing file with same name
        query = f"name = '{escape_drive_query_value(filename)}' and '{parent_id}' in parents and trashed = false"
        chk_resp = self.session.get(
            f"{self.DRIVE_API_BASE}/files",
            headers=self._get_headers(),
            params={
                "q": query,
                "fields": "files(id, size, md5Checksum)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
            timeout=15,
        )
        existing = self._check(chk_resp).get("files", [])

        if existing:
            if ondup == "skip":
                try:
                    existing_size = existing[0].get("size")
                    if size is not None and existing_size is not None and int(existing_size) == size:
                        return {"id": existing[0]["id"], "name": filename, "status": "skipped"}
                except (TypeError, ValueError):
                    pass
            if ondup in ("skip", "overwrite"):
                # Delete existing file before re-upload
                for old in existing:
                    self.delete(file_id=old["id"])

        # Initiate Resumable Upload
        init_url = f"{self.UPLOAD_API_BASE}/files?uploadType=resumable&supportsAllDrives=true"
        file_metadata = {
            "name": filename,
            "parents": [parent_id],
        }
        init_headers = {
            **self._get_headers(),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
        }
        if size is not None:
            init_headers["X-Upload-Content-Length"] = str(size)

        init_resp = self.session.post(init_url, headers=init_headers, data=json.dumps(file_metadata), timeout=30)
        if init_resp.status_code != 200:
            self._check(init_resp)

        location_url = init_resp.headers.get("Location")
        if not location_url:
            raise RuntimeError("Google Drive did not return resumable upload Location URL")

        # Stream upload to location URL
        chunk_size = 4 * 1024 * 1024  # 4MB default
        if hasattr(self, "cfg") and hasattr(self.cfg, "data"):
            chunk_size = self.cfg.data.get("transfer", {}).get("chunk_size", chunk_size)
        # Google Drive resumable upload chunk size must be multiple of 256KB
        chunk_size = max(256 * 1024, (chunk_size // (256 * 1024)) * (256 * 1024))

        if size is not None and size > 0:
            offset = 0
            pending = b""
            pending_offset = 0
            while offset < size:
                if not pending:
                    bytes_to_read = min(chunk_size, size - offset)
                    pending = stream.read(bytes_to_read)
                    pending_offset = offset
                    if not pending:
                        break

                chunk = pending[offset - pending_offset :]
                chunk_len = len(chunk)
                end_offset = offset + chunk_len - 1
                headers = {
                    "Content-Range": f"bytes {offset}-{end_offset}/{size}",
                    "Content-Length": str(chunk_len),
                    "Content-Type": mime_type,
                }
                for retry_count in range(3):
                    put_resp = self.session.put(location_url, headers=headers, data=chunk, timeout=300)
                    if put_resp.status_code not in (429, 500, 502, 503):
                        break
                    if retry_count == 2:
                        return self._check(put_resp)
                    time.sleep(0.5 * (2**retry_count))

                if put_resp.status_code in (200, 201):
                    return self._check(put_resp)
                elif put_resp.status_code == 308:
                    range_hdr = put_resp.headers.get("Range") or put_resp.headers.get("range")
                    if range_hdr and range_hdr.startswith("bytes=") and "-" in range_hdr:
                        try:
                            committed_offset = int(range_hdr.rsplit("-", 1)[1]) + 1
                        except ValueError:
                            offset += chunk_len
                        else:
                            if not pending_offset <= committed_offset <= pending_offset + len(pending):
                                raise RuntimeError("Google Drive returned an invalid resumable upload Range")
                            offset = committed_offset
                    else:
                        offset += chunk_len
                    if offset >= pending_offset + len(pending):
                        pending = b""
                else:
                    return self._check(put_resp)
            raise RuntimeError("Google Drive upload finished without 200/201 confirmation")
        else:
            upload_headers = {"Content-Type": mime_type}
            if size is not None:
                upload_headers["Content-Length"] = str(size)
            put_resp = self.session.put(location_url, headers=upload_headers, data=stream, timeout=600)
            return self._check(put_resp)


