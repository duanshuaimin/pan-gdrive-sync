# Review Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden pan-gdrive-sync per `docs/superpowers/specs/2026-09-04-review-hardening-design.md`: HTTP Basic Auth for Web, XSS fixes, disk-cache cleanup, unified config dir, transfer/docs/test fixes.

**Architecture:** Keep existing layering (clients → TransferEngine → CLI/Web). Add Web Basic Auth at Flask `before_request`; centralize paths via `CONFIG_DIR`; pure helpers for HTML/Drive escaping; migrate DB/SA from legacy `~/.config/pangdrive` once.

**Tech Stack:** Python 3.8+, Flask/Werkzeug, Click, unittest/pytest-compatible unittest, vanilla JS static UI, SQLite.

## Global Constraints

- Spec: HTTP Basic Auth only; credentials via `pgsync auth web` → `web.username` + `web.password_hash` in config.json (`0o600`).
- Spec: Config root is `~/.config/pan-gdrive-sync/` only; migrate from `~/.config/pangdrive/` without deleting old files.
- Spec: Interval jobs run only while `pgsync web` is up; document, do not add scheduler CLI.
- Spec: `--skip` means same name **and** same size; unknown size → do not skip.
- Spec: TDD — failing test before production code for each behavior.
- Spec: Do not implement standalone scheduler, `conflict_policy: newer`, Baidu chunked upload rewrite, or global concurrency caps.
- Do not commit unless the user asks (plan commit steps are optional checkpoints; skip if user has not requested commits).

## File map

| File | Responsibility |
|------|----------------|
| `pangdrive/config.py` | `CONFIG_DIR`, default `web` section, `set_web_auth`, `has_web_auth`, `clear_baidu`/verify-before-save helpers as needed |
| `pangdrive/paths.py` (new) | Canonical paths + one-time migrate helpers for DB/SA |
| `pangdrive/utils.py` | `escape_html`, `escape_drive_query_value` |
| `pangdrive/storage.py` | Default DB under `CONFIG_DIR`; use migrate helper; testable singleton reset |
| `pangdrive/web/app.py` | Basic Auth gate; SA path; `auth_mode`; verify-before-save Baidu; history clear finished-only |
| `pangdrive/cli.py` | `auth web`; web startup gate; `sync --disk-cache`; baidu verify-before-save; job interval help |
| `pangdrive/transfer.py` | Disk-cache finally; skip-by-size; Drive query escape; `use_disk_cache` on sync |
| `pangdrive/gdrive_client.py` | Query escape; list pagination; skip-by-size on upload |
| `pangdrive/baidu_client.py` | List pagination |
| `pangdrive/web/static/app.js` | Full escapeHtml; DOM events; credentials on fetch |
| `pangdrive/web/task_manager.py` | Pass `use_disk_cache` if exposed on tasks (optional if CLI-only flag first) |
| `README.md` | Auth web, bind warnings, config root, interval needs web |
| `tests/test_hardening.py` (new) | Unit tests for this work |
| `tests/test_sync.py` | Mark live tests integration; avoid real DB |

---

### Task 1: Path helpers + Storage default dir + migration

**Files:**
- Create: `pangdrive/paths.py`
- Modify: `pangdrive/storage.py`
- Create: `tests/test_hardening.py`
- Test: `tests/test_hardening.py`

**Interfaces:**
- Consumes: `pangdrive.config.CONFIG_DIR`
- Produces:
  - `LEGACY_CONFIG_DIR: Path` → `~/.config/pangdrive`
  - `tasks_db_path() -> Path`
  - `service_account_path() -> Path`
  - `migrate_legacy_artifacts() -> None` (copy DB/SA if new missing and old exists; chmod SA `0o600`; no delete)
  - `Storage.reset_instance_for_tests()` classmethod clearing `_instance`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hardening.py
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pangdrive import paths
from pangdrive.storage import Storage


class TestPathsAndStorage(unittest.TestCase):
    def tearDown(self):
        Storage.reset_instance_for_tests()

    def test_migrate_copies_tasks_db_when_new_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_root = Path(tmp) / "pan-gdrive-sync"
            old_root = Path(tmp) / "pangdrive"
            old_root.mkdir()
            new_root.mkdir()
            old_db = old_root / "tasks.db"
            old_db.write_bytes(b"sqlite-fake")
            with mock.patch.object(paths, "CONFIG_DIR", new_root), mock.patch.object(
                paths, "LEGACY_CONFIG_DIR", old_root
            ):
                paths.migrate_legacy_artifacts()
                self.assertTrue((new_root / "tasks.db").is_file())
                self.assertEqual((new_root / "tasks.db").read_bytes(), b"sqlite-fake")
                self.assertTrue(old_db.is_file())  # not deleted

    def test_storage_default_db_under_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "pan-gdrive-sync"
            cfg.mkdir()
            with mock.patch("pangdrive.storage.CONFIG_DIR", cfg), mock.patch(
                "pangdrive.paths.CONFIG_DIR", cfg
            ), mock.patch("pangdrive.paths.LEGACY_CONFIG_DIR", Path(tmp) / "missing"):
                Storage.reset_instance_for_tests()
                s = Storage.get_instance()
                self.assertEqual(Path(s.db_path), cfg / "tasks.db")
```

Note: import `CONFIG_DIR` into `storage` from `pangdrive.config` or `pangdrive.paths` consistently (prefer `paths.tasks_db_path()`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_hardening.TestPathsAndStorage -v`  
Expected: FAIL (module/helpers/reset missing)

- [ ] **Step 3: Write minimal implementation**

```python
# pangdrive/paths.py
import logging
import os
import shutil
from pathlib import Path

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

LEGACY_CONFIG_DIR = Path.home() / ".config" / "pangdrive"


def tasks_db_path() -> Path:
    return CONFIG_DIR / "tasks.db"


def service_account_path() -> Path:
    return CONFIG_DIR / "service_account.json"


def migrate_legacy_artifacts() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    pairs = [
        (LEGACY_CONFIG_DIR / "tasks.db", tasks_db_path()),
        (LEGACY_CONFIG_DIR / "service_account.json", service_account_path()),
    ]
    for src, dst in pairs:
        if dst.exists() or not src.is_file():
            continue
        shutil.copy2(src, dst)
        if dst.name == "service_account.json":
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
        logger.info("Migrated %s -> %s", src, dst)
```

In `storage.py`: import `tasks_db_path`, `migrate_legacy_artifacts`; default `__init__` call migrate then `str(tasks_db_path())`; add:

```python
@classmethod
def reset_instance_for_tests(cls):
    with cls._lock:
        cls._instance = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_hardening.TestPathsAndStorage -v`  
Expected: OK

- [ ] **Step 5: Commit (only if user asked)**

```bash
git add pangdrive/paths.py pangdrive/storage.py tests/test_hardening.py
git commit -m "fix: unify task DB path under pan-gdrive-sync config dir"
```

---

### Task 2: Web Basic Auth + `auth web` CLI

**Files:**
- Modify: `pangdrive/config.py` (default `web` dict; `set_web_auth`, `has_web_auth`)
- Modify: `pangdrive/cli.py` (`auth web`, gate in `web_cmd`)
- Modify: `pangdrive/web/app.py` (`before_request` Basic Auth)
- Modify: `pangdrive/web/static/app.js` (`fetch` credentials)
- Modify: `README.md` (auth + bind warnings)
- Test: `tests/test_hardening.py`

**Interfaces:**
- Consumes: Werkzeug `generate_password_hash`, `check_password_hash`; `config.has_web_auth()`
- Produces:
  - `Config.set_web_auth(username: str, password: str) -> None`
  - `Config.has_web_auth() -> bool`
  - `create_app()` rejects unauthenticated `/api/*` with 401 + `WWW-Authenticate`

- [ ] **Step 1: Write the failing tests**

```python
import base64
from unittest import mock
from werkzeug.security import generate_password_hash

from pangdrive.config import Config
from pangdrive.web.app import create_app


class TestWebBasicAuth(unittest.TestCase):
    def test_api_requires_basic_auth(self):
        cfg = Config.__new__(Config)
        cfg.config_dir = Path(tempfile.mkdtemp())
        cfg.config_file = cfg.config_dir / "config.json"
        cfg.data = {
            "baidu": {},
            "gdrive": {"auth_mode": "oauth2"},
            "transfer": {},
            "web": {
                "username": "admin",
                "password_hash": generate_password_hash("secret"),
            },
        }
        with mock.patch("pangdrive.web.app.config", cfg), mock.patch(
            "pangdrive.web.task_manager.TaskManager.get_instance"
        ) as gi:
            # Return a lightweight fake task manager if needed
            from pangdrive.web.task_manager import TaskManager
            gi.return_value = mock.MagicMock()
            app = create_app()
            client = app.test_client()
            r = client.get("/api/status")
            self.assertEqual(r.status_code, 401)
            self.assertIn("Basic", r.headers.get("WWW-Authenticate", ""))
            token = base64.b64encode(b"admin:secret").decode()
            r2 = client.get("/api/status", headers={"Authorization": f"Basic {token}"})
            self.assertNotEqual(r2.status_code, 401)

    def test_has_web_auth_false_when_empty(self):
        cfg = Config.__new__(Config)
        cfg.data = {"web": {"username": "", "password_hash": ""}}
        self.assertFalse(Config.has_web_auth(cfg))  # or instance method
```

Adjust test to match how `config` singleton is patched; prefer injecting auth check that reads the same object `create_app` uses. If `TaskManager.get_instance` starts threads, mock it before `create_app`.

Also test CLI gate conceptually:

```python
def test_web_cmd_requires_auth_config(self):
    # invoke click with mocked config.has_web_auth False → exit code 1
    from click.testing import CliRunner
    from pangdrive.cli import cli
    with mock.patch("pangdrive.cli.config") as c:
        c.has_web_auth.return_value = False
        runner = CliRunner()
        result = runner.invoke(cli, ["web"])
        self.assertNotEqual(result.exit_code, 0)
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `python3 -m unittest tests.test_hardening.TestWebBasicAuth -v`

- [ ] **Step 3: Implement**

`config.py` defaults:

```python
"web": {
    "username": "",
    "password_hash": "",
},
```

```python
def set_web_auth(self, username: str, password: str) -> None:
    from werkzeug.security import generate_password_hash
    self.data.setdefault("web", {})
    self.data["web"]["username"] = username.strip()
    self.data["web"]["password_hash"] = generate_password_hash(password)
    self.save()

def has_web_auth(self) -> bool:
    web = self.data.get("web") or {}
    return bool(web.get("username") and web.get("password_hash"))
```

`cli.py` — new command under `auth_group`:

```python
@auth_group.command("web")
@click.option("--username", "-u", prompt=True)
@click.option("--password", "-p", prompt=True, hide_input=True, confirmation_prompt=True)
def auth_web_cmd(username, password):
    """Set HTTP Basic Auth credentials for the Web UI."""
    if not username or not password:
        console.print("[bold red]Username and password are required.[/bold red]")
        sys.exit(1)
    config.set_web_auth(username, password)
    console.print("[bold green]✓ Web UI Basic Auth credentials saved.[/bold green]")
```

In `web_cmd` **before** `create_app()` / `app.run`:

```python
if not config.has_web_auth():
    console.print("[bold red]Web auth not configured.[/bold red] Run: pan-gdrive-sync auth web")
    sys.exit(1)
if debug and host not in ("127.0.0.1", "localhost", "::1"):
    console.print("[bold red]Refusing --debug on non-loopback bind.[/bold red]")
    sys.exit(1)
```

`app.py` after creating `app`:

```python
from werkzeug.security import check_password_hash

@app.before_request
def require_basic_auth():
    if not request.path.startswith("/api/"):
        return None
    auth = request.authorization
    web = config.data.get("web") or {}
    user = web.get("username") or ""
    pw_hash = web.get("password_hash") or ""
    if not user or not pw_hash:
        return jsonify({"ok": False, "error": "Web auth not configured"}), 503
    if (
        auth
        and auth.username == user
        and auth.password
        and check_password_hash(pw_hash, auth.password)
    ):
        return None
    return Response(
        "Unauthorized",
        401,
        {"WWW-Authenticate": 'Basic realm="pan-gdrive-sync"'},
    )
```

`app.js` — ensure fetch helper sends credentials:

```javascript
async function fetchAPI(url, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  // existing headers/json logic
}
```

EventSource: browsers attach Basic credentials for same-origin after first challenge; document in README if SSE fails until one authenticated XHR.

README: replace Web start section with `auth web` then `web`; warn HTTPS for non-local; note interval jobs need web process (can land fully in Task 7).

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Optional commit**

```bash
git commit -m "feat: require HTTP Basic Auth for Web API"
```

---

### Task 3: XSS hardening in `app.js`

**Files:**
- Modify: `pangdrive/web/static/app.js`
- Optionally add: `pangdrive/web/static/escape.js` only if needed for testing — prefer pure function in `pangdrive/utils.py` mirrored in JS, and unit-test **Python** `escape_html`; manually verify JS matches (or duplicate expected strings in a tiny Node-less test by reading the JS function with regex — skip; rely on Python helper + JS parity comment).
- Add Python `escape_html` in `utils.py` for any server-rendered strings (none today) and document JS must match.
- Test: `tests/test_hardening.py` for `escape_html`

**Interfaces:**
- Produces: `escape_html(s: str) -> str` escaping `& < > " '`

- [ ] **Step 1: Failing test**

```python
from pangdrive.utils import escape_html

class TestEscapeHtml(unittest.TestCase):
    def test_escapes_all_special(self):
        self.assertEqual(
            escape_html("<script>alert('x')</script>\"&"),
            "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;&quot;&amp;",
        )
```

(Choose one encoding for `'` — either `&#x27;` or `&#39;`; stick to it in JS.)

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement Python + JS**

```python
def escape_html(s: str) -> str:
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
```

JS:

```javascript
function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}
```

Refactor `renderFileList` to build rows with `createElement`, set `textContent` for names, set `dataset.drive` / `dataset.path` / `dataset.isdir`, attach listeners via delegation on the container (one listener for click/change). Toast: `el.textContent = msg` or append text span without `innerHTML`.

- [ ] **Step 4: PASS + smoke-read app.js for remaining raw interpolations into HTML**

Grep: `innerHTML` and `onclick=` in `app.js` — eliminate dangerous path embeddings.

- [ ] **Step 5: Optional commit**

```bash
git commit -m "fix: harden Web UI HTML escaping and event binding"
```

---

### Task 4: Disk-cache cancel cleanup

**Files:**
- Modify: `pangdrive/transfer.py`
- Test: `tests/test_hardening.py`

**Interfaces:**
- Consumes: existing `transfer_file(..., use_disk_cache=True, cancel_event=...)`
- Produces: temp file removed on cancel during download

- [ ] **Step 1: Failing test**

```python
import threading
from pangdrive.transfer import TransferEngine, TransferCancelledError

class TestDiskCacheCleanup(unittest.TestCase):
    def test_cancel_during_disk_cache_download_removes_tmp(self):
        engine = TransferEngine.__new__(TransferEngine)
        # Build fake baidu/gdrive with download that yields chunks then waits on cancel
        # Simpler approach: patch NamedTemporaryFile to record path; raise cancel on first chunk
        ...
```

Concrete minimal approach:

```python
def test_disk_cache_branch_unlinks_on_cancel(self):
    import tempfile
    from unittest.mock import MagicMock, patch
    from pangdrive.transfer import TransferEngine, TransferCancelledError

    engine = TransferEngine.__new__(TransferEngine)
    engine.baidu = MagicMock()
    engine.gdrive = MagicMock()

    class FakeResp:
        def iter_content(self, chunk_size=65536):
            yield b"abc"
            raise TransferCancelledError("Transfer cancelled by user")

    engine.baidu.download_stream.return_value = (FakeResp(), 3, "md5")
    cancel = threading.Event()
    # Force cancel path: set event before loop OR raise inside iter as above
    with patch("pangdrive.transfer.tempfile.NamedTemporaryFile", wraps=tempfile.NamedTemporaryFile) as ntf:
        with self.assertRaises(TransferCancelledError):
            engine.transfer_file(
                "baidu", "/a.bin", "gdrive", "/b.bin",
                use_disk_cache=True, cancel_event=cancel,
            )
        # Discover created paths from wrap or track via side_effect
```

Cleaner: implement production code with `tmp_path = None` + `finally`, and test by:

```python
created = []

class TrackingNTF:
    def __init__(self, *a, **k):
        self._f = tempfile.NamedTemporaryFile(*a, **k)
        created.append(self._f.name)
    def __enter__(self): ...
    def __exit__(self, *a): ...
    # delegate write/name
```

Assert after cancel: `not os.path.exists(created[0])`.

- [ ] **Step 2: FAIL** (file still exists)

- [ ] **Step 3: Fix both disk-cache branches**

```python
tmp_path = None
try:
    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp_f:
        tmp_path = tmp_f.name
        for chunk in resp.iter_content(chunk_size=65536):
            if cancel_event and cancel_event.is_set():
                raise TransferCancelledError("Transfer cancelled by user")
            ...
    with open(tmp_path, "rb") as f_in:
        res = self.gdrive.upload_stream(...)
finally:
    if tmp_path and os.path.exists(tmp_path):
        os.remove(tmp_path)
```

Same for GDrive→Baidu branch.

- [ ] **Step 4: PASS**

- [ ] **Step 5: Optional commit**

```bash
git commit -m "fix: always remove disk-cache temp files on cancel"
```

---

### Task 5: `auth_mode` + Baidu verify-before-save

**Files:**
- Modify: `pangdrive/web/app.py` (status `auth_mode`; SA path via `service_account_path()` + chmod; Baidu verify then save)
- Modify: `pangdrive/cli.py` (`auth baidu` verify then save; on failure do not leave bad BDUSS — clear or never write first)
- Test: `tests/test_hardening.py`

**Interfaces:**
- Status JSON field `gdrive.type` sourced from `auth_mode`
- `service_account_path()` for Web SA writes

- [ ] **Step 1: Failing tests**

```python
def test_status_uses_auth_mode(self):
    # With mocked clients authenticated, patch config.data['gdrive']['auth_mode']='token'
    # GET /api/status with Basic auth → gdrive.type == 'token'

def test_baidu_auth_does_not_persist_on_verify_failure(self):
    # POST /api/auth/baidu with invalid bduss; mock BaiduClient.get_user_info to raise
    # Assert config.set_baidu not called with credentials before verify OR config bduss unchanged
```

For CLI: unit-test by extracting verify-then-save order — simplest fix: remove the first `config.set_baidu` before verify; only save after success (and optionally save minimal for client construction via in-memory override).

BaiduClient reads from config — pattern:

```python
# Keep old credentials
old = dict(config.data["baidu"])
try:
    config.set_baidu(bduss=..., ...)  # temporary — BAD per spec
```

Better:

```python
client = BaiduClient(config)
# Temporarily set on instance without save — or
config.data["baidu"]["bduss"] = bduss  # memory only
info = client.get_user_info()
config.set_baidu(...)  # persist on success
```

On failure restore `old` if memory was mutated.

- [ ] **Step 2: FAIL**

- [ ] **Step 3: Implement** + chmod SA file after write

```python
key_path = str(service_account_path())
...
os.chmod(key_path, 0o600)
```

- [ ] **Step 4: PASS**

- [ ] **Step 5: Optional commit**

```bash
git commit -m "fix: align auth_mode and verify Baidu before persisting"
```

---

### Task 6: Drive escape, pagination, skip-by-size, sync disk-cache, history clear

**Files:**
- Modify: `pangdrive/utils.py` (`escape_drive_query_value`)
- Modify: `pangdrive/gdrive_client.py`, `pangdrive/baidu_client.py`, `pangdrive/transfer.py`, `pangdrive/cli.py`, `pangdrive/web/app.py`, `pangdrive/web/task_manager.py` (pass `use_disk_cache` if tasks gain the field; otherwise CLI sync flag + `sync_directory` param is enough for CLI; Web can add JSON field later — **require** plumbing `use_disk_cache: bool = False` on `sync_directory` and CLI; Web transfer start optional same field)
- Test: `tests/test_hardening.py`

**Interfaces:**
- `escape_drive_query_value(s: str) -> str` → `s.replace("\\", "\\\\").replace("'", "\\'")` (Drive docs: escape `\` and `'`)
- `list_dir` returns full pages
- skip only if sizes equal
- `sync_directory(..., use_disk_cache: bool = False)` passed to `transfer_file`
- `history/clear` → `clear_tasks(only_finished=True)`

- [ ] **Step 1: Failing tests**

```python
from pangdrive.utils import escape_drive_query_value

class TestDriveEscape(unittest.TestCase):
    def test_escapes_quote(self):
        self.assertEqual(escape_drive_query_value("O'Brien"), r"O\'Brien")

class TestSkipBySize(unittest.TestCase):
    def test_skip_requires_equal_size(self):
        # Mock gdrive list returning file size 10; baidu download would be size 10 → skip
        # Mock dest size 9 → does not skip (calls download)
        ...

class TestGDriveListPagination(unittest.TestCase):
    def test_follows_next_page_token(self):
        # Mock session.get returning page1 + nextPageToken, then page2
        ...
```

Baidu pagination: PCS `list` supports `limit` (default 1000) and `start`. Loop `start += len(page)` until empty or `len(page) < limit`.

- [ ] **Step 2: FAIL**

- [ ] **Step 3: Implement**

Replace every `name = '{x}'` with `name = '{escape_drive_query_value(x)}'`.

`gdrive_client.list_dir`:

```python
items = []
page_token = None
while True:
    params = {..., "pageSize": 1000}
    if page_token:
        params["pageToken"] = page_token
    data = self._check(resp)
    for f in data.get("files", []):
        ...
    page_token = data.get("nextPageToken")
    if not page_token:
        break
```

Include `nextPageToken` in `fields` param: `"nextPageToken, files(...)"`.

Skip logic in `transfer_file` (Baidu→GDrive): if files and `int(files[0].get("size", -1)) == total_size_from_src` — but size known only after `download_stream` meta. Order today: skip check **before** download. Fix: for skip, fetch source size first (baidu `meta` / gdrive list) then compare dest size.

```python
if ondup == "skip":
    src_size = ...  # from baidu.meta(src_p)[0]['size'] or gdrive metadata
    # dest lookup with size field
    if dest_exists and dest_size == src_size:
        return skipped
    # if dest_size unknown: do not skip
```

`upload_stream` skip path: compare `existing[0].get("size")` to `size` arg; if `size is None` or unequal → overwrite path or upload (do not skip).

CLI:

```python
@click.option("--disk-cache", is_flag=True, ...)
def sync_cmd(..., disk_cache):
    engine.sync_directory(..., use_disk_cache=disk_cache)
```

`history/clear`: `only_finished=True`.

- [ ] **Step 4: PASS**

- [ ] **Step 5: Optional commit**

```bash
git commit -m "fix: harden Drive queries, pagination, skip-by-size, sync disk-cache"
```

---

### Task 7: Docs + test isolation for existing suite

**Files:**
- Modify: `README.md` (Web auth, config root, interval needs `web`, testing section)
- Modify: `pangdrive/cli.py` (`job add --interval` help text)
- Modify: `tests/test_sync.py` (mark integration; reset Storage; skip unless env)

**Interfaces:**
- Env: `PGSYNC_INTEGRATION=1` runs live tests

- [ ] **Step 1: Write failing guard test / adjust suite**

```python
import os
import unittest

def integration(fn):
    return unittest.skipUnless(
        os.environ.get("PGSYNC_INTEGRATION") == "1",
        "Set PGSYNC_INTEGRATION=1 for live cloud tests",
    )(fn)

class TestPanGDriveSync(unittest.TestCase):
    @integration
    def test_02_baidu_live_connection(self):
        ...
```

Mark any Web test that hits real status/auth similarly. For Web unit parts that need app: use temp Storage:

```python
Storage.reset_instance_for_tests()
Storage.get_instance(db_path=temp_db)
```

Fix `TaskManager` singleton similarly if it caches Storage — add `TaskManager.reset_instance_for_tests()` if required for isolation.

- [ ] **Step 2: Run default suite without env — live tests skipped; unit OK**

Run: `PGSYNC_INTEGRATION= python3 -m unittest tests.test_sync tests.test_hardening -v`  
Expected: no live Baidu failures; hardening green.

- [ ] **Step 3: README edits**

- Auth: `pan-gdrive-sync auth web` before `web`
- Bind `0.0.0.0` only behind HTTPS reverse proxy; Basic Auth required
- Config dir: `~/.config/pan-gdrive-sync/` (migrates from `pangdrive/`)
- Job interval: “定时任务仅在 `pan-gdrive-sync web` 进程运行期间由内置调度器触发；仅 CLI 不会执行 interval。”
- Testing: default unit; `PGSYNC_INTEGRATION=1` for live

- [ ] **Step 4: Full verify**

Run: `python3 -m unittest discover -s tests -v`

- [ ] **Step 5: Optional commit**

```bash
git commit -m "docs: clarify Web auth, config path, and interval scheduling"
```

---

## Self-review vs spec

| Spec section | Task |
|--------------|------|
| §1 Basic Auth + auth web + README bind | Task 2, 7 |
| §2 XSS | Task 3 |
| §3 Disk-cache cleanup | Task 4 |
| §4 Config unification + migration | Task 1, 5 (SA path) |
| §5 auth_mode + Baidu verify-before-save | Task 5 |
| §6 Drive escape, pagination, skip size, sync disk-cache, history clear | Task 6 |
| §7 Scheduler docs only | Task 7 |
| §8 Testing strategy | Tasks 1–7 + Task 7 markers |
| Out of scope items | Not scheduled |

Placeholder scan: none intentional.  
Type consistency: `has_web_auth`, `set_web_auth`, `escape_html`, `escape_drive_query_value`, `migrate_legacy_artifacts`, `Storage.reset_instance_for_tests` used consistently.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-04-review-hardening.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — execute tasks in this session with executing-plans checkpoints  

Which approach?
