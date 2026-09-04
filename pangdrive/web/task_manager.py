"""Background task manager for cross-cloud transfers and sync."""

import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..transfer import TransferCancelledError, TransferEngine
from ..utils import format_size


class Task:
    def __init__(
        self,
        task_id: str,
        source: str,
        dest: str,
        mode: str = "copy",
        skip_existing: bool = True,
        recursive: bool = True,
    ):
        self.id = task_id
        self.source = source
        self.dest = dest
        self.mode = mode  # "copy" or "sync"
        self.skip_existing = skip_existing
        self.recursive = recursive

        self.status = "pending"  # pending, running, completed, failed, cancelled
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

        self.total_bytes = 0
        self.transferred_bytes = 0
        self.current_file = ""
        self.file_index = 0
        self.total_files = 1
        self.error: Optional[str] = None

        self._last_bytes = 0
        self._last_time = time.time()
        self.speed_bytes_sec = 0.0

        self.cancel_event = threading.Event()

    def update_bytes(self, chunk_len: int, current_transferred: int, total: int):
        now = time.time()
        self.transferred_bytes = current_transferred
        if total > 0:
            self.total_bytes = total

        dt = now - self._last_time
        if dt >= 0.5:
            delta = self.transferred_bytes - self._last_bytes
            self.speed_bytes_sec = max(0.0, delta / dt)
            self._last_time = now
            self._last_bytes = self.transferred_bytes

    def cancel(self):
        self.cancel_event.set()
        if self.status in ("pending", "running"):
            self.status = "cancelling"

    def to_dict(self) -> Dict[str, Any]:
        percent = 0.0
        if self.total_bytes > 0:
            percent = min(100.0, round((self.transferred_bytes / self.total_bytes) * 100, 1))
        elif self.status == "completed":
            percent = 100.0

        speed_str = f"{format_size(int(self.speed_bytes_sec))}/s" if self.status == "running" and self.speed_bytes_sec > 0 else "-"

        eta_sec = None
        if self.status == "running" and self.speed_bytes_sec > 1024 and self.total_bytes > self.transferred_bytes:
            eta_sec = int((self.total_bytes - self.transferred_bytes) / self.speed_bytes_sec)

        return {
            "id": self.id,
            "source": self.source,
            "dest": self.dest,
            "mode": self.mode,
            "status": self.status,
            "percent": percent,
            "total_bytes": self.total_bytes,
            "total_bytes_str": format_size(self.total_bytes),
            "transferred_bytes": self.transferred_bytes,
            "transferred_bytes_str": format_size(self.transferred_bytes),
            "current_file": self.current_file,
            "file_index": self.file_index,
            "total_files": self.total_files,
            "speed_str": speed_str,
            "eta_seconds": eta_sec,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = TaskManager()
        return cls._instance

    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.lock = threading.Lock()
        self.listeners: List[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def broadcast(self):
        tasks_data = self.get_all_tasks()
        with self.lock:
            dead_listeners = []
            for q in self.listeners:
                try:
                    q.put_nowait(tasks_data)
                except queue.Full:
                    pass
                except Exception:
                    dead_listeners.append(q)
            for d in dead_listeners:
                if d in self.listeners:
                    self.listeners.remove(d)

    def create_task(
        self,
        source: str,
        dest: str,
        mode: str = "copy",
        skip_existing: bool = True,
        recursive: bool = True,
    ) -> Task:
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        task = Task(
            task_id=task_id,
            source=source,
            dest=dest,
            mode=mode,
            skip_existing=skip_existing,
            recursive=recursive,
        )
        with self.lock:
            self.tasks[task_id] = task

        # Launch background runner
        thread = threading.Thread(target=self._run_task, args=(task,), daemon=True)
        thread.start()
        self.broadcast()
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self.lock:
            return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        with self.lock:
            # Sort newest first
            sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
            return [t.to_dict() for t in sorted_tasks]

    def cancel_task(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task.cancel()
        self.broadcast()
        return True

    def clear_completed(self):
        with self.lock:
            removable = [
                tid for tid, t in self.tasks.items()
                if t.status in ("completed", "failed", "cancelled")
            ]
            for tid in removable:
                del self.tasks[tid]
        self.broadcast()

    def _run_task(self, task: Task):
        task.status = "running"
        task.started_at = time.time()
        task._last_time = time.time()
        self.broadcast()

        try:
            from ..utils import split_storage_uri
            src_p, src_path = split_storage_uri(task.source)
            dst_p, dst_path = split_storage_uri(task.dest)

            engine = TransferEngine()
            ondup = "skip" if task.skip_existing else "overwrite"

            if task.mode == "sync":
                def sync_cb(ev: dict):
                    if ev.get("event") == "file_start":
                        task.current_file = ev.get("current_file", "")
                        task.file_index = ev.get("file_index", 0)
                        task.total_files = ev.get("total_files", 1)
                    elif ev.get("event") == "chunk":
                        task.update_bytes(
                            ev.get("chunk_len", 0),
                            ev.get("transferred_bytes", 0),
                            ev.get("total_bytes", 0),
                        )
                    elif ev.get("event") == "file_complete":
                        task.transferred_bytes = ev.get("transferred_bytes", 0)
                    self.broadcast()

                res = engine.sync_directory(
                    src_provider=src_p,
                    src_dir=src_path,
                    dst_provider=dst_p,
                    dst_dir=dst_path,
                    ondup=ondup,
                    recursive=task.recursive,
                    progress_callback=sync_cb,
                    cancel_event=task.cancel_event,
                    show_console_progress=False,
                )
                task.total_bytes = res.get("total_bytes", task.total_bytes)
                task.transferred_bytes = task.total_bytes

            else:
                # Single file copy
                task.current_file = src_path.split("/")[-1]
                task.total_files = 1
                task.file_index = 1

                def file_cb(chunk_len, read_bytes, total_bytes):
                    task.update_bytes(chunk_len, read_bytes, total_bytes)
                    self.broadcast()

                engine.transfer_file(
                    src_provider=src_p,
                    src_path=src_path,
                    dst_provider=dst_p,
                    dst_path=dst_path,
                    ondup=ondup,
                    progress=None,
                    task_id=None,
                    callback=file_cb,
                    cancel_event=task.cancel_event,
                )
                if task.total_bytes > 0:
                    task.transferred_bytes = task.total_bytes

            task.status = "completed"
            task.finished_at = time.time()

        except TransferCancelledError:
            task.status = "cancelled"
            task.finished_at = time.time()
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.finished_at = time.time()

        self.broadcast()
