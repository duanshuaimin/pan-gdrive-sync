"""Utility helper functions."""

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Union


def escape_html(s: str) -> str:
    """Escape text for safe insertion into HTML; mirrors app.js escapeHtml."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def format_size(num_bytes: Union[int, float]) -> str:
    """Format bytes into readable human format (KB, MB, GB, TB)."""
    if num_bytes is None:
        return "N/A"
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def guess_mime_type(filename: str) -> str:
    """Guess MIME type from filename with sensible defaults."""
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    ext = Path(filename).suffix.lower()
    common_types = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".zip": "application/zip",
        ".7z": "application/x-7z-compressed",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
    }
    return common_types.get(ext, "application/octet-stream")


def normalize_path(path: str) -> str:
    """Normalize remote path ensuring single leading slash."""
    if not path:
        return "/"
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    # Remove redundant slashes
    path = re.sub(r"/+", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def split_storage_uri(uri: str) -> tuple[str, str]:
    """Split uri like 'baidu:/path' or 'gdrive:/folder' into (provider, path)."""
    uri = uri.strip()
    if ":" in uri:
        provider, path = uri.split(":", 1)
        provider = provider.lower().strip()
        if provider in ("baidu", "pan", "bdpan", "baidupan"):
            return "baidu", normalize_path(path)
        elif provider in ("gdrive", "google", "drive", "googledrive"):
            return "gdrive", normalize_path(path)
        else:
            raise ValueError(f"Unknown storage provider prefix: {provider}. Must be 'baidu:' or 'gdrive:'")
    else:
        raise ValueError(f"Invalid URI format '{uri}'. Must include provider prefix like 'baidu:/path' or 'gdrive:/path'")
