"""Bidirectional cross-cloud streaming transfer and synchronization engine."""

import os
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .baidu_client import BaiduClient
from .config import config
from .gdrive_client import GoogleDriveClient
from .utils import format_size, guess_mime_type, normalize_path, split_storage_uri

console = Console()


class TransferDirection(Enum):
    BAIDU_TO_GDRIVE = "baidu_to_gdrive"
    GDRIVE_TO_BAIDU = "gdrive_to_baidu"


class ProgressStreamWrapper:
    """Wraps an HTTP response raw stream to update a progress bar as bytes flow."""

    def __init__(self, raw_stream, progress: Optional[Progress] = None, task_id=None, total: int = 0):
        self.raw = raw_stream
        self.progress = progress
        self.task_id = task_id
        self.total = total
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.raw.read(size)
        if chunk:
            self.read_bytes += len(chunk)
            if self.progress and self.task_id is not None:
                self.progress.update(self.task_id, advance=len(chunk))
        return chunk

    def __len__(self) -> int:
        return self.total

    def close(self):
        if hasattr(self.raw, "close"):
            self.raw.close()


class TransferEngine:
    def __init__(self, baidu: Optional[BaiduClient] = None, gdrive: Optional[GoogleDriveClient] = None):
        self.baidu = baidu or BaiduClient()
        self.gdrive = gdrive or GoogleDriveClient()
        self.cfg = config

    def transfer_file(
        self,
        src_provider: str,
        src_path: str,
        dst_provider: str,
        dst_path: str,
        ondup: str = "overwrite",
        progress: Optional[Progress] = None,
        task_id=None,
        use_disk_cache: bool = False,
    ) -> Dict[str, Any]:
        """Transfer a single file between Baidu Netdisk and Google Drive."""
        src_p = normalize_path(src_path)
        dst_p = normalize_path(dst_path)
        filename = os.path.basename(src_p)

        # Ensure destination path includes filename
        if dst_p.endswith("/") or dst_p == "/":
            dst_p = f"{dst_p}/{filename}".replace("//", "/")

        if src_provider == "baidu" and dst_provider == "gdrive":
            direction = TransferDirection.BAIDU_TO_GDRIVE
        elif src_provider == "gdrive" and dst_provider == "baidu":
            direction = TransferDirection.GDRIVE_TO_BAIDU
        else:
            raise ValueError(f"Unsupported transfer combination: {src_provider} -> {dst_provider}")

        # Check skip policy
        if ondup == "skip":
            if direction == TransferDirection.BAIDU_TO_GDRIVE:
                try:
                    parent_p = os.path.dirname(dst_p) or "/"
                    parent_id = self.gdrive.resolve_path(parent_p)
                    q = f"name = '{filename}' and '{parent_id}' in parents and trashed = false"
                    res = self.gdrive.session.get(
                        f"{self.gdrive.DRIVE_API_BASE}/files",
                        headers=self.gdrive._get_headers(),
                        params={"q": q, "fields": "files(id, size)"},
                    ).json()
                    files = res.get("files", [])
                    if files:
                        if progress and task_id is not None:
                            progress.update(task_id, description=f"[yellow]Skipped (exists): {filename}")
                        return {"status": "skipped", "file": filename}
                except Exception:
                    pass
            else:
                try:
                    m = self.baidu.meta(dst_p)
                    if m and len(m) > 0 and not m[0].get("isdir"):
                        if progress and task_id is not None:
                            progress.update(task_id, description=f"[yellow]Skipped (exists): {filename}")
                        return {"status": "skipped", "file": filename}
                except Exception:
                    pass

        # Execute Transfer
        if direction == TransferDirection.BAIDU_TO_GDRIVE:
            # 1. Open Baidu download stream
            resp, total_size, md5 = self.baidu.download_stream(src_p)
            if progress and task_id is not None:
                progress.update(task_id, total=total_size)

            if use_disk_cache:
                # Cache to temporary file on disk first
                with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            tmp_f.write(chunk)
                            if progress and task_id is not None:
                                progress.update(task_id, advance=len(chunk))
                    tmp_path = tmp_f.name
                try:
                    with open(tmp_path, "rb") as f_in:
                        res = self.gdrive.upload_stream(
                            f_in,
                            dst_p,
                            size=total_size,
                            mime_type=guess_mime_type(filename),
                            ondup=ondup,
                        )
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            else:
                # Direct streaming pipe
                stream_wrapper = ProgressStreamWrapper(resp.raw, progress=progress, task_id=task_id, total=total_size)
                res = self.gdrive.upload_stream(
                    stream_wrapper,
                    dst_p,
                    size=total_size,
                    mime_type=guess_mime_type(filename),
                    ondup=ondup,
                )

            return {"status": "success", "direction": "baidu->gdrive", "result": res}

        else:
            # 2. Open GDrive download stream
            # Resolve GDrive file ID
            parent_p = os.path.dirname(src_p) or "/"
            parent_id = self.gdrive.resolve_path(parent_p)
            q = f"name = '{filename}' and '{parent_id}' in parents and trashed = false"
            f_resp = self.gdrive.session.get(
                f"{self.gdrive.DRIVE_API_BASE}/files",
                headers=self.gdrive._get_headers(),
                params={"q": q, "fields": "files(id, size, mimeType)"},
            ).json()
            files = f_resp.get("files", [])
            if not files:
                raise FileNotFoundError(f"Google Drive source file not found: {src_p}")

            file_id = files[0]["id"]
            resp, total_size, md5 = self.gdrive.download_stream(file_id)

            if progress and task_id is not None:
                progress.update(task_id, total=total_size)

            if use_disk_cache:
                with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            tmp_f.write(chunk)
                            if progress and task_id is not None:
                                progress.update(task_id, advance=len(chunk))
                    tmp_path = tmp_f.name
                try:
                    with open(tmp_path, "rb") as f_in:
                        res = self.baidu.upload_stream(f_in, dst_p, size=total_size, ondup=ondup)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            else:
                stream_wrapper = ProgressStreamWrapper(resp.raw, progress=progress, task_id=task_id, total=total_size)
                res = self.baidu.upload_stream(stream_wrapper, dst_p, size=total_size, ondup=ondup)

            return {"status": "success", "direction": "gdrive->baidu", "result": res}

    def sync_directory(
        self,
        src_provider: str,
        src_dir: str,
        dst_provider: str,
        dst_dir: str,
        ondup: str = "overwrite",
        recursive: bool = True,
    ) -> Dict[str, Any]:
        """Synchronize an entire folder between Baidu Netdisk and Google Drive."""
        src_dir = normalize_path(src_dir)
        dst_dir = normalize_path(dst_dir)

        console.print(
            f"[bold cyan]Scanning {src_provider.upper()} directory: {src_dir}...[/bold cyan]"
        )

        all_files: List[Dict[str, Any]] = []

        if src_provider == "baidu":
            dirs_to_visit = [src_dir]
            while dirs_to_visit:
                curr = dirs_to_visit.pop(0)
                items = self.baidu.list_dir(curr)
                for it in items:
                    if it["isdir"]:
                        if recursive:
                            dirs_to_visit.append(it["path"])
                    else:
                        all_files.append(it)
        else:
            # GDrive recursive scan
            dirs_to_visit = [(src_dir, self.gdrive.resolve_path(src_dir))]
            while dirs_to_visit:
                curr_path, curr_id = dirs_to_visit.pop(0)
                items = self.gdrive.list_dir(curr_path, folder_id=curr_id)
                for it in items:
                    if it["isdir"]:
                        if recursive:
                            dirs_to_visit.append((it["path"], it["id"]))
                    else:
                        all_files.append(it)

        if not all_files:
            console.print(f"[yellow]No files found in {src_provider}:{src_dir}[/yellow]")
            return {"transferred": 0, "skipped": 0, "failed": 0, "total_bytes": 0}

        total_bytes = sum(f["size"] for f in all_files)
        console.print(
            f"[bold blue]Found {len(all_files)} files ({format_size(total_bytes)}) to transfer from {src_provider}:{src_dir} to {dst_provider}:{dst_dir}[/bold blue]"
        )

        transferred_count = 0
        skipped_count = 0
        failed_count = 0
        transferred_bytes = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            overall_task = progress.add_task(f"Total Progress ({len(all_files)} files)", total=total_bytes)

            for it in all_files:
                f_path = it["path"]
                rel_path = f_path[len(src_dir):].lstrip("/")
                target_dest = f"{dst_dir}/{rel_path}".replace("//", "/")
                f_size = it["size"]
                f_name = it["name"]

                file_task = progress.add_task(f"Syncing {f_name}", total=f_size)
                try:
                    res = self.transfer_file(
                        src_provider,
                        f_path,
                        dst_provider,
                        target_dest,
                        ondup=ondup,
                        progress=progress,
                        task_id=file_task,
                    )
                    if res.get("status") == "skipped":
                        skipped_count += 1
                    else:
                        transferred_count += 1
                        transferred_bytes += f_size
                    progress.update(overall_task, advance=f_size)
                except Exception as e:
                    failed_count += 1
                    console.print(f"[bold red]Failed transferring {f_name}: {e}[/bold red]")
                finally:
                    progress.remove_task(file_task)

        console.print(
            f"[bold green]Sync Completed: {transferred_count} transferred, {skipped_count} skipped, {failed_count} failed. Total {format_size(transferred_bytes)}[/bold green]"
        )

        return {
            "transferred": transferred_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "total_bytes": transferred_bytes,
        }


