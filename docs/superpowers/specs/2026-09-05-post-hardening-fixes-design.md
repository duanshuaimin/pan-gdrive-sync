# Post-Hardening Review Fixes Design

**Date:** 2026-09-05  
**Status:** Approved for planning  
**Scope:** Critical + Important findings from review of commits `9a79af8..7a9b9e4` (packaging approach 1)

## Goal

Fix daemon lifecycle so `--once` / `job run-due` actually complete transfers; replace insecure `?auth=` + localStorage password storage with HttpOnly session cookies; harden transfer/Drive/Baidu/SSE behaviors without regressing prior Basic Auth / skip-by-size / pagination hardening.

## Decisions (locked)

| Topic | Choice |
|-------|--------|
| Web session | HttpOnly `SameSite=Strict` session cookie via `POST /api/session` |
| `next_run_at` | Advance at **trigger** time (not completion) |
| Shared Drive UX | Synthetic prefix `/__shared__/` (no `sharedWithMe` OR into root) |
| Packaging | Critical first, then Important on same branch |

## Out of scope (this round)

- Login rate limiting / constant-time username compare
- Cross-process Baidu sliced-upload resume persistence
- Live verification of single-block createsuperfile empty-block hack
- Rewriting dynamic chunk-size formula beyond readability tweaks

---

## 1. HttpOnly session auth

### Endpoints

- `POST /api/session` — JSON `{username, password}` or form fields; verify with `check_password_hash`; on success create opaque random token, store server-side `{token: expiry}`, set cookie:
  - Name: `pgsync_session`
  - Flags: `HttpOnly`, `SameSite=Strict`, `Path=/`
  - Optional `Max-Age` / absolute expiry (default 24h; sliding optional — default fixed expiry)
- `DELETE /api/session` — clear cookie, invalidate server token
- Existing HTTP **Basic** on `/api/*` remains valid for CLI/scripts

### Middleware

- `before_request` for `/api/*` (except `POST /api/session` itself): accept valid session cookie **or** valid Basic Auth
- **Remove** query-string `?auth=` acceptance entirely (including SSE)

### Frontend

- Login modal → `POST /api/session` with `credentials: "same-origin"`
- All `fetch` / `EventSource` use `credentials: "same-origin"`; no `Authorization: Basic` header from JS; no `?auth=`
- Stop writing password/Basic token to `localStorage` and `sessionStorage`
- Logout → `DELETE /api/session` + clear any leftover client keys if present

### Tests

- Unauthenticated `/api/status` → 401
- After `POST /api/session`, cookie allows `/api/status` → 200
- `?auth=` no longer authorizes
- Invert/remove hardening test that asserted `?auth=` works

---

## 2. Daemon / scheduler lifecycle

### Wait for tasks

- `run_due_jobs()` returns launched `Task` objects (already) and/or thread handles
- `daemon --once` and `job run-due` **join** all threads started by that invocation before process exit
- Prefer non-orphaning: either use `daemon=False` for CLI-waited threads or keep daemon=True but always join before exit

### `next_run_at` at trigger

- When a due job is selected to run: if `interval_seconds > 0`, set `next_run_at = now + interval_seconds` **immediately** (before/at task create), persist to DB
- Failed runs do **not** auto-reschedule earlier; user may `job run` manually (document this)

### Dedup

- If job already has a task in `running` status, skip re-trigger in `run_due_jobs`

### Interval plumbing

- `daemon --interval N` → `TaskManager.start_scheduler(poll_seconds=N)` (or equivalent)
- Replace hard-coded `time.sleep(5)` in `_scheduler_loop`
- Avoid useless double-sleep loops: either CLI loop drives polling by calling `run_due_jobs`, or only the TaskManager scheduler runs — pick one coherent model in implementation (prefer: TaskManager scheduler uses N; daemon `--once` does not start the loop)

### Docs + tests

- README crontab example remains valid once wait works; note no auto-retry on failure
- Test with real `TaskManager` + stubbed transfer that sleeps briefly: `--once` exits only after completion; `next_run_at` updated at trigger

---

## 3. Transfer destination detection

- Remove extension-based heuristic (`src_ext and not dst_ext` ⇒ directory)
- Keep: trailing slash / empty path ⇒ directory; provider `resolve_path` / `meta` probes for existing folders

---

## 4. Drive resumable upload Range + retry

- On HTTP 308: parse `Range: bytes=0-N` (if present); set `offset = N + 1`; align stream position
- On 429 / 5xx: limited retries with backoff; then query/resume via 308 Range semantics where possible
- Do not advance `offset` by full `chunk_len` when Range says fewer bytes were committed

---

## 5. `/__shared__/` namespace

- Constant e.g. `SHARED_ROOT = "/__shared__"`
- Root `list_dir` / `resolve_path` for normal paths: **no** `sharedWithMe = true` OR
- `list_dir("/__shared__")` or `/__shared__`: query `sharedWithMe = true and trashed = false` (with Drive pagination + supportsAllDrives as today)
- `resolve_path("/__shared__/Name/...")`: resolve first segment among shared items, then children via parents
- Web UI / README: document the prefix for shared content

---

## 6. Baidu sliced upload retry

- Per-block upload: limited retries with exponential backoff on transient failures
- On final failure: raise with context (block index); no cross-process resume store this round

---

## 7. SSE lifecycle

- Keep `EventSource` on `state` (or module-level)
- `initTaskStream`: close existing ES; clear existing poll interval; then open new ES with credentials (cookies)
- `onerror`: close ES, clear interval before starting fallback poll

---

## 8. Implementation order

1. Session cookie auth + frontend + tests (remove `?auth=`)
2. Daemon wait + `next_run_at` at trigger + dedup + `--interval` + README + tests
3. Drop extension heuristic
4. Drive 308 Range + retry
5. `/__shared__/` namespace
6. Baidu block retry
7. SSE close/cleanup

TDD for each behavior where practical.

---

## Success criteria

- `pgsync daemon --once` completes due jobs before exit; crontab workflow works
- No password in URL or `localStorage`; SSE works via cookie
- Extensionless dest filenames no longer misrouted as folders
- Drive chunked upload respects Range; Baidu blocks retry transient errors
- Shared files only under `/__shared__/`, not duplicated at root
- SSE login cycles do not stack EventSources/pollers
- Default unit tests pass without live credentials
