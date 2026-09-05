# Post-Hardening Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Critical/Important issues from review of `9a79af8..7a9b9e4` per `docs/superpowers/specs/2026-09-05-post-hardening-fixes-design.md`.

**Architecture:** Add server-side opaque session store + HttpOnly cookie; make CLI daemon wait for task threads and advance `next_run_at` at trigger; fix Drive Range/resume, `/__shared__/` namespace, Baidu block retry, SSE lifecycle.

**Tech Stack:** Python 3.8+, Flask/Werkzeug, Click, unittest, vanilla JS.

## Global Constraints

- Spec: HttpOnly `pgsync_session` cookie; remove `?auth=`; no password in localStorage/sessionStorage.
- Spec: HTTP Basic remains valid for non-browser clients.
- Spec: `next_run_at` advances at **trigger** time; `--once` / `job run-due` join threads before exit.
- Spec: Shared content only under `/__shared__/`; no extension-based folder heuristic.
- Spec: TDD where practical; commit only if user/SDD workflow requests.
- Branch: `fix/post-hardening-review` (already created).

## File map

| File | Responsibility |
|------|----------------|
| `pangdrive/web/session_store.py` (new) | In-memory token → expiry map; create/validate/revoke |
| `pangdrive/web/app.py` | Session routes; auth middleware without `?auth=` |
| `pangdrive/web/static/app.js` | Cookie-based login; SSE cleanup; no Basic storage |
| `pangdrive/web/task_manager.py` | Waitable tasks; next_run at trigger; poll_seconds; join helpers |
| `pangdrive/cli.py` | daemon once wait; interval → scheduler; job run-due wait |
| `pangdrive/transfer.py` | Remove extension heuristic |
| `pangdrive/gdrive_client.py` | Range on 308; retries; `/__shared__/` |
| `pangdrive/baidu_client.py` | Per-block retry |
| `README.md` | Session auth note; crontab; `/__shared__/` |
| `tests/test_hardening.py` | Invert `?auth=` test; session cookie tests |
| `tests/test_improvements.py` / `tests/test_post_hardening.py` (new OK) | Daemon wait, shared path, Range, etc. |

---

### Task 1: Session cookie auth

**Files:**
- Create: `pangdrive/web/session_store.py`
- Modify: `pangdrive/web/app.py`
- Modify: `pangdrive/web/static/app.js`
- Modify: `tests/test_hardening.py`
- Create/Modify: tests for session

**Interfaces:**
- Produces: `SessionStore.create(ttl=86400) -> token`, `.validate(token) -> bool`, `.revoke(token)`
- Cookie name: `pgsync_session`
- `POST /api/session`, `DELETE /api/session` (auth-exempt for POST login only)

- [ ] **Step 1: Failing tests**

```python
def test_query_auth_no_longer_works(self):
    # existing client with web auth configured
    token = base64.b64encode(b"admin:secret").decode()
    r = client.get(f"/api/status?auth={token}")
    self.assertEqual(r.status_code, 401)

def test_session_cookie_allows_api(self):
    r = client.post("/api/session", json={"username": "admin", "password": "secret"})
    self.assertEqual(r.status_code, 200)
    self.assertIn("pgsync_session", r.headers.get("Set-Cookie", ""))
    r2 = client.get("/api/status")  # test client keeps cookies
    self.assertEqual(r2.status_code, 200)

def test_basic_auth_still_works(self):
    # Authorization: Basic ... still 200
```

- [ ] **Step 2: Run — expect FAIL** (query auth still 200 / no session route)

- [ ] **Step 3: Implement**

`session_store.py`: thread-safe dict; `secrets.token_urlsafe(32)`; prune expired on validate.

`app.py`:
- Module/app-level `SessionStore`
- `POST /api/session` before auth gate (path exempt in `before_request`)
- `DELETE /api/session` requires valid session or Basic, then revoke
- `before_request`: skip auth for `POST /api/session`; check cookie then Basic; remove `?auth=` block
- `set_cookie(..., httponly=True, samesite="Strict", path="/", max_age=86400)`

`app.js`:
- `handleLoginSubmit`: `fetch("/api/session", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body: JSON.stringify({username, password})})`
- On success: close modal, `updateAuthUI` based on `/api/status` or a lightweight logged-in flag in sessionStorage **username only** (optional display), never password
- Remove `getAuthHeader` Basic usage from `fetchAPI`; always `credentials: "same-origin"`
- `initTaskStream`: no `?auth=`; `new EventSource("/api/tasks/events", { withCredentials: true })` if supported — note: browser EventSource `withCredentials` is standard; cookies sent for same-origin by default
- Logout: `DELETE /api/session` + clear any old localStorage keys

- [ ] **Step 4: PASS** (`python3 -m unittest tests.test_hardening -v` and related)

- [ ] **Step 5: Commit** (if SDD): `fix: replace query auth with HttpOnly session cookies`

---

### Task 2: Daemon wait + next_run_at at trigger + interval

**Files:**
- Modify: `pangdrive/web/task_manager.py`
- Modify: `pangdrive/cli.py`
- Modify: `README.md`
- Test: `tests/test_improvements.py` or new file

**Interfaces:**
- `create_task(..., waitable=True)` stores `task._thread`
- `run_due_jobs()`: on trigger, update `next_run_at = now + interval` **before** starting work; still skip if already running
- `wait_for_tasks(tasks: List[Task], timeout=None)` joins threads
- `start_scheduler(poll_seconds: int = 5)`
- Completion handler: update `last_run_at` / `last_status` but **do not** recompute next_run_at from interval (already set at trigger); if interval is 0 leave next_run null

- [ ] **Step 1: Failing test**

```python
def test_daemon_once_waits_for_transfer(self):
    # Real TaskManager + temp Storage; patch TransferEngine.sync_directory to sleep 0.3s then return
    # Invoke run_due_jobs + wait_for_tasks (or CLI daemon --once)
    # Assert task status completed and elapsed >= 0.3
    # Assert job.next_run_at >= trigger_time + interval - epsilon

def test_run_due_skips_already_running(self):
    # Create running task for job; run_due_jobs returns empty
```

- [ ] **Step 2: FAIL** (task still pending/running when once returns)

- [ ] **Step 3: Implement**

In `create_task`:
```python
thread = threading.Thread(..., daemon=True)
task._thread = thread
thread.start()
```

In `trigger_job` / `run_due_jobs` before `create_task`:
```python
now = time.time()
interval = j.get("interval_seconds", 0)
updates = {"last_run_at": now}  # or only next_run here
if interval > 0:
    self.storage.update_job(j["id"], next_run_at=now + interval)
task = self.create_task(...)
```

In `_run_task` job update block: set `last_status`, `last_run_at`; **omit** advancing `next_run_at` again (or only set if was null).

`daemon --once` / `job run-due`:
```python
triggered = task_mgr.run_due_jobs()
task_mgr.wait_for_tasks(triggered)
```

`daemon` long-running: `task_mgr.start_scheduler(poll_seconds=interval)` and sleep loop only until signal (or remove idle sleep and just join scheduler thread). Prefer: call `start_scheduler(poll_seconds=interval)` and `while running: time.sleep(1)` checking flag — scheduler loop uses `poll_seconds`.

Also check DB for running tasks of same job_id when deduping (not only in-memory), if crashed rows exist — optional: treat DB `running` as blocked or recover as interrupted at startup (already may exist).

- [ ] **Step 4: PASS**

- [ ] **Step 5: Commit:** `fix: wait for daemon jobs and advance next_run_at at trigger`

---

### Task 3: Remove extension folder heuristic

**Files:** `pangdrive/transfer.py`, tests

- [ ] **Step 1:** Test `transfer_file` with mocks: dst `baidu:/backups/report_final`, src file with `.pdf`, meta says not dir → dest path ends with `report_final` not `report_final/report.pdf`

- [ ] **Step 2–4:** Delete lines 145–150 heuristic; keep slash + meta/resolve probes

- [ ] **Step 5:** Commit `fix: stop guessing directories from missing file extensions`

---

### Task 4: Drive 308 Range + retry

**Files:** `pangdrive/gdrive_client.py`, tests

- [ ] **Step 1:** Mock `session.put` returning 308 with `Range: bytes=0-100` after sending 200-byte chunk → next request starts at offset 101

- [ ] **Step 3:**

```python
elif put_resp.status_code == 308:
    range_hdr = put_resp.headers.get("Range") or put_resp.headers.get("range")
    if range_hdr and "-" in range_hdr:
        # parse bytes=0-N
        offset = int(range_hdr.split("-")[-1]) + 1
    else:
        offset += chunk_len
elif put_resp.status_code in (429, 500, 502, 503):
    # backoff retry same chunk limited times
else:
    return self._check(put_resp)
```

If stream is not seekable, track consumed bytes carefully (only advance local read position by what was newly committed).

- [ ] **Step 5:** Commit `fix: honor Drive upload Range and retry transient errors`

---

### Task 5: `/__shared__/` namespace

**Files:** `pangdrive/gdrive_client.py`, `README.md`, tests, optionally Web UI breadcrumb hint

**Constant:** `SHARED_PREFIX = "/__shared__"`

- [ ] **Step 1:** Tests: root list query must not contain `sharedWithMe`; list `/__shared__` must; resolve `/__shared__/Foo` queries shared

- [ ] **Step 3:** In `resolve_path` / `list_dir`:
  - If path is `/` or normal: only `'{id}' in parents` (no shared OR)
  - If path == `/__shared__` or under it: strip prefix; first component resolve via `sharedWithMe = true and name = '...'`; deeper via parents
  - `normalize_path` keeps `__shared__` as normal segment

- [ ] **Step 5:** Commit `feat: isolate shared Drive items under /__shared__/`

---

### Task 6: Baidu sliced upload retry

**Files:** `pangdrive/baidu_client.py`, tests

- [ ] **Step 1:** Mock block upload fail once then succeed → overall success; fail N times → raise

- [ ] **Step 3:** Wrap block POST in retry loop (e.g. 3 attempts, backoff 0.5/1/2s) for connection/5xx; don't retry clear 4xx auth errors

- [ ] **Step 5:** Commit `fix: retry transient Baidu sliced upload block failures`

---

### Task 7: SSE lifecycle cleanup

**Files:** `pangdrive/web/static/app.js`

- [ ] **Step 1:** Manual/code inspection checklist as test substitute (no JS runner): document expected close behavior; optional tiny Node parse for `eventSource.close`

- [ ] **Step 3:**

```javascript
function initTaskStream() {
  if (state.eventSource) {
    try { state.eventSource.close(); } catch (e) {}
    state.eventSource = null;
  }
  if (state.taskPollTimer) {
    clearInterval(state.taskPollTimer);
    state.taskPollTimer = null;
  }
  const eventSource = new EventSource("/api/tasks/events");
  state.eventSource = eventSource;
  eventSource.onerror = () => {
    try { eventSource.close(); } catch (e) {}
    if (state.eventSource === eventSource) state.eventSource = null;
    if (state.taskPollTimer) clearInterval(state.taskPollTimer);
    state.taskPollTimer = setInterval(pollTasks, 3000);
  };
  ...
}
```

- [ ] **Step 5:** Commit `fix: prevent EventSource and poll timer leaks`

---

## Self-review vs spec

| Spec § | Task |
|--------|------|
| §1 Session cookie | Task 1 |
| §2 Daemon lifecycle | Task 2 |
| §3 Extension heuristic | Task 3 |
| §4 Drive Range | Task 4 |
| §5 `/__shared__/` | Task 5 |
| §6 Baidu retry | Task 6 |
| §7 SSE | Task 7 |
| Out of scope Minors | Not scheduled |

---

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-09-05-post-hardening-fixes.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task + review between tasks  
2. **Inline Execution** — this session with executing-plans checkpoints  

Which approach?
