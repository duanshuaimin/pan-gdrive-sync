"""Persistent background task manager and job scheduler for cross-cloud sync."""

import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..config import config
from ..storage import Storage
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
        job_id: Optional[str] = None,
        status: str = "pending",
        total_bytes: int = 0,
        transferred_bytes: int = 0,
        current_file: str = "",
        error: Optional[str] = None,
        created_at: Optional[float] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ):
        self.id = task_id
        self.job_id = job_id
        self.source = source
        self.dest = dest
        self.mode = mode  # "copy" or "sync"
        self.skip_existing = skip_existing
        self.recursive = recursive

        self.status = status  # pending, running, completed, failed, cancelled, interrupted
        self.created_at = created_at or time.time()
        self.started_at = started_at
        self.finished_at = finished_at

        self.total_bytes = total_bytes
        self.transferred_bytes = transferred_bytes
        self.current_file = current_file
        self.file_index = 0
        self.total_files = 1
        self.error = error

        self._last_bytes = 0
        self._last_time = time.time()
        self._last_db_save = 0.0
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

        speed_str = (
            f"{format_size(int(self.speed_bytes_sec))}/s"
            if self.status == "running" and self.speed_bytes_sec > 0
            else "-"
        )

        eta_sec = None
        if self.status == "running" and self.speed_bytes_sec > 1024 and self.total_bytes > self.transferred_bytes:
            eta_sec = int((self.total_bytes - self.transferred_bytes) / self.speed_bytes_sec)

        return {
            "id": self.id,
            "job_id": self.job_id,
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
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = TaskManager(db_path=db_path)
            return cls._instance

    @classmethod
    def reset_instance_for_tests(cls):
        with cls._lock:
            if cls._instance is not None:
                cls._instance.stop_scheduler()
            cls._instance = None

    def __init__(self, db_path: Optional[str] = None):
        self.config = config
        self.storage = Storage.get_instance(db_path=db_path)
        self.tasks: Dict[str, Task] = {}
        self.lock = threading.Lock()
        self.listeners: List[queue.Queue] = []

        # 1. Recover state: clean interrupted tasks from any previous crashed sessions
        self.storage.clean_interrupted_tasks()

        # 2. Restore recent task history into in-memory cache
        recent_tasks = self.storage.list_tasks(limit=50)
        for r in recent_tasks:
            t = Task(
                task_id=r["id"],
                source=r["source"],
                dest=r["dest"],
                mode=r["mode"],
                job_id=r.get("job_id"),
                status=r["status"],
                total_bytes=r.get("total_bytes", 0),
                transferred_bytes=r.get("transferred_bytes", 0),
                current_file=r.get("current_file", ""),
                error=r.get("error"),
                created_at=r.get("created_at"),
                started_at=r.get("started_at"),
                finished_at=r.get("finished_at"),
            )
            self.tasks[t.id] = t

        # 3. Start background job scheduler thread
        self._scheduler_running = True
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    # ==========================================
    # Real-time SSE Subscriptions
    # ==========================================

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

    # ==========================================
    # Task Management & Execution
    # ==========================================

    def create_task(
        self,
        source: str,
        dest: str,
        mode: str = "copy",
        skip_existing: bool = True,
        recursive: bool = True,
        job_id: Optional[str] = None,
    ) -> Task:
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        task = Task(
            task_id=task_id,
            source=source,
            dest=dest,
            mode=mode,
            skip_existing=skip_existing,
            recursive=recursive,
            job_id=job_id,
        )
        with self.lock:
            self.tasks[task_id] = task

        # Persist task initial state
        self.storage.save_task(task.to_dict())

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
            sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)
            return [t.to_dict() for t in sorted_tasks]

    def cancel_task(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task.cancel()
            self.storage.save_task(task.to_dict())
        self.broadcast()
        return True

    def clear_completed(self):
        with self.lock:
            removable = [
                tid for tid, t in self.tasks.items()
                if t.status in ("completed", "failed", "cancelled", "interrupted")
            ]
            for tid in removable:
                del self.tasks[tid]
            self.storage.clear_tasks(only_finished=True)
        self.broadcast()

    def _persist_task_progress(self, task: Task, force: bool = False):
        now = time.time()
        if force or (now - task._last_db_save >= 2.0):
            task._last_db_save = now
            try:
                self.storage.save_task(task.to_dict())
            except Exception:
                pass

    def _run_task(self, task: Task):
        task.status = "running"
        task.started_at = time.time()
        task._last_time = time.time()
        self._persist_task_progress(task, force=True)
        self.broadcast()

        try:
            from ..utils import split_storage_uri
            src_p, src_path = split_storage_uri(task.source)
            dst_p, dst_path = split_storage_uri(task.dest)

            engine = TransferEngine()
            ondup = "skip" if task.skip_existing else "overwrite"

            stream_mode = getattr(self.config, "data", {}).get("transfer", {}).get("stream_mode", True)
            use_disk_cache = not stream_mode
            task._last_broadcast = 0.0

            if task.mode == "sync":
                def sync_cb(ev: dict):
                    now = time.time()
                    if ev.get("event") == "file_start":
                        task.current_file = ev.get("current_file", "")
                        task.file_index = ev.get("file_index", 0)
                        task.total_files = ev.get("total_files", 1)
                        self._persist_task_progress(task, force=True)
                        self.broadcast()
                    elif ev.get("event") == "chunk":
                        task.update_bytes(
                            ev.get("chunk_len", 0),
                            ev.get("transferred_bytes", 0),
                            ev.get("total_bytes", 0),
                        )
                        if now - task._last_broadcast >= 0.5:
                            task._last_broadcast = now
                            self._persist_task_progress(task, force=False)
                            self.broadcast()
                    elif ev.get("event") == "file_complete":
                        task.transferred_bytes = ev.get("transferred_bytes", 0)
                        self._persist_task_progress(task, force=True)
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
                    use_disk_cache=use_disk_cache,
                )
                task.total_bytes = res.get("total_bytes", task.total_bytes)
                task.transferred_bytes = task.total_bytes

            else:
                task.current_file = src_path.split("/")[-1]
                task.total_files = 1
                task.file_index = 1

                def file_cb(chunk_len, read_bytes, total_bytes):
                    task.update_bytes(chunk_len, read_bytes, total_bytes)
                    now = time.time()
                    if now - task._last_broadcast >= 0.5:
                        task._last_broadcast = now
                        self._persist_task_progress(task, force=False)
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
                    use_disk_cache=use_disk_cache,
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

        # Update persistent storage
        self.storage.save_task(task.to_dict())

        # Update sync job state if linked
        if task.job_id:
            now = time.time()
            job = self.storage.get_job(task.job_id)
            if job:
                interval = job.get("interval_seconds", 0)
                next_run = (now + interval) if interval > 0 else None
                self.storage.update_job(
                    task.job_id,
                    last_run_at=now,
                    last_status=task.status,
                    next_run_at=next_run,
                )

        self.broadcast()

    # ==========================================
    # Persistent Sync Jobs Management & Scheduling
    # ==========================================

    def create_job(
        self,
        name: str,
        source: str,
        dest: str,
        mode: str = "sync",
        skip_existing: bool = True,
        recursive: bool = True,
        interval_seconds: int = 0,
    ) -> Dict[str, Any]:
        job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        return self.storage.create_job(
            job_id=job_id,
            name=name,
            source=source,
            dest=dest,
            mode=mode,
            skip_existing=skip_existing,
            recursive=recursive,
            interval_seconds=interval_seconds,
        )

    def list_jobs(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.storage.list_jobs(status=status)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.get_job(job_id)

    def update_job(self, job_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        return self.storage.update_job(job_id, **kwargs)

    def delete_job(self, job_id: str) -> bool:
        return self.storage.delete_job(job_id)

    def toggle_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.storage.get_job(job_id)
        if not job:
            return None
        new_status = "paused" if job["status"] == "active" else "active"
        return self.storage.update_job(job_id, status=new_status)

    def trigger_job(self, job_id: str) -> Optional[Task]:
        job = self.storage.get_job(job_id)
        if not job:
            return None

        task = self.create_task(
            source=job["source"],
            dest=job["dest"],
            mode=job["mode"],
            skip_existing=bool(job.get("skip_existing", 1)),
            recursive=bool(job.get("recursive", 1)),
            job_id=job_id,
        )
        return task

    def run_due_jobs(self) -> List[Task]:
        """Check active scheduled jobs and trigger any that are currently due.

        Returns list of newly triggered tasks.
        """
        now = time.time()
        triggered = []
        try:
            active_jobs = self.storage.list_jobs(status="active")
            for j in active_jobs:
                interval = j.get("interval_seconds", 0)
                if interval > 0:
                    next_run = j.get("next_run_at")
                    # If next_run is due or never set
                    if next_run is None or now >= next_run:
                        # Avoid duplicate runs if one is already in progress for this job
                        with self.lock:
                            already_running = any(
                                t.job_id == j["id"] and t.status in ("pending", "running")
                                for t in self.tasks.values()
                            )
                        if not already_running:
                            task = self.trigger_job(j["id"])
                            if task:
                                triggered.append(task)
        except Exception:
            pass
        return triggered

    def _scheduler_loop(self):
        """Background scheduler polling loop that triggers scheduled sync jobs."""
        while self._scheduler_running:
            time.sleep(5)
            self.run_due_jobs()

    def stop_scheduler(self):
        self._scheduler_running = False
