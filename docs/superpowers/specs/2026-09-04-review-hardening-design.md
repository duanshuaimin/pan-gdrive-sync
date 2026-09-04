# Review Hardening Design

**Date:** 2026-09-04  
**Status:** Approved for planning  
**Scope:** Critical + Important fixes from full-repo code review (packaging approach 1)

## Goal

Make pan-gdrive-sync safe to run with the Web UI (including non-localhost binds), eliminate XSS and temp-file leaks, unify config storage, align transfer/docs semantics, and harden tests—without adding an independent scheduler process.

## Decisions (locked)

| Topic | Choice |
|-------|--------|
| Web auth | HTTP Basic Auth |
| Credential setup | `pgsync auth web` interactive; store username + password hash in config |
| Config root | Unify on `~/.config/pan-gdrive-sync/` with one-time migration from `~/.config/pangdrive/` |
| Interval jobs | Web process only; document clearly (no new `scheduler` CLI) |
| Packaging | Security + config first; same branch continues with transfer/test Important items |

## Out of scope (this round)

- Standalone `pgsync scheduler` or systemd/cron registration
- Auth schemes other than HTTP Basic
- Implementing `conflict_policy: "newer"`
- Rewriting Baidu upload to multi-chunk PCS APIs
- Global concurrent transfer caps
- Removing Flask `--debug` entirely (document risk only; refuse debug on non-loopback if cheap)

---

## 1. Web HTTP Basic Auth

### Behavior

- New CLI: `pgsync auth web` (also `pan-gdrive-sync auth web`).
  - Prompt for username and password (password confirmation).
  - Persist under `config.json`:
    - `web.username` (string)
    - `web.password_hash` (Werkzeug `generate_password_hash` output)
  - Keep `config.json` mode `0o600`.
- `pgsync web` **must** find both fields before binding. If missing → exit with non-zero status and message to run `auth web`. Applies even for `127.0.0.1`.
- Flask `before_request`:
  - Protect all `/api/*` routes with HTTP Basic Auth via `check_password_hash`.
  - On failure: `401` + `WWW-Authenticate: Basic realm="pan-gdrive-sync"`.
  - Static UI (`/`, `/static/*`) may remain unauthenticated so the shell loads; browser will prompt when `fetch`/`EventSource` hit APIs.
- Frontend: use `credentials: "same-origin"` (or equivalent) on `fetch` and ensure SSE works after Basic Auth (browsers send cached Basic credentials to same origin).

### Docs

- README: require `auth web` before `web`; remove any implication that unauthenticated `0.0.0.0` is safe.
- Note: public exposure still needs HTTPS reverse proxy; Basic Auth over plain HTTP is eavesdroppable.

### Error handling

- Invalid Basic credentials → 401, no body detail that distinguishes user vs password.
- Missing web auth config at startup → hard fail (do not start server).

---

## 2. XSS hardening (`pangdrive/web/static/app.js`)

### Behavior

- Replace `escapeHtml` so it escapes `&`, `<`, `>`, `"`, `'` for text/attribute contexts.
- Prefer `textContent` / `createElement` for toasts, names, errors, paths.
- Remove string-built `onclick="..."` / `onchange="..."` that embed cloud paths.
  - Use `data-drive`, `data-path`, `data-isdir` (or similar) + delegated `addEventListener`.
- Never insert raw API `error` or filenames into `innerHTML` without escaping (prefer text nodes).

### Testing

- Unit-test the escape helper (or a small exported/pure function) for `&<>"'` and quote-only edge cases.

---

## 3. Disk-cache temp cleanup (`pangdrive/transfer.py`)

### Behavior

- After creating `NamedTemporaryFile(..., delete=False)`, assign `tmp_path` immediately.
- Wrap download loop + upload in one `try`/`finally` that unlinks `tmp_path` if set and exists.
- Apply to both Baidu→Drive and Drive→Baidu disk-cache branches.
- Cancellation (`TransferCancelledError`) and other exceptions must still clean up.

### Testing

- Simulate cancel mid-download with a fake stream; assert temp file is gone.

---

## 4. Config directory unification

### Canonical root

`~/.config/pan-gdrive-sync/`

| Artifact | Path |
|----------|------|
| Config | `.../config.json` (existing) |
| Task/job DB | `.../tasks.db` (was `~/.config/pangdrive/tasks.db`) |
| Service account JSON (Web upload) | `.../service_account.json` with `0o600` |

### Migration

On Storage / Web SA path init:

1. If new `tasks.db` missing and old `~/.config/pangdrive/tasks.db` exists → copy to new location; log once.
2. If new SA file missing and old `~/.config/pangdrive/service_account.json` exists → copy + `chmod 0o600`; log once.
3. Do **not** delete old files automatically.

### Docs

README: single config root; mention one-time migration from `pangdrive/`.

---

## 5. Auth field and Baidu persist fixes

- Status API and UI: read/display `gdrive.auth_mode` (not nonexistent `auth_type` in config). Web POST may still accept `auth_type` as input alias but persist `auth_mode`.
- Baidu CLI/Web auth: verify BDUSS/cookies **before** `set_baidu`; on failure do not write credentials (or roll back).

---

## 6. Transfer semantics

### Drive `q` escaping

- Helper e.g. `escape_drive_query_value(s) -> str` replacing `'` with `\'`.
- All `name = '...'` interpolations in `gdrive_client.py` and `transfer.py` use it.

### Listing pagination

- Google Drive `list_dir`: loop on `nextPageToken` until exhausted.
- Baidu `list_dir`: use API pagination (limit/start or equivalent already supported by PCS) until a short page or empty.

### `--skip` = same name **and** same size

- When `ondup == "skip"`, skip only if destination exists with equal size.
- If size unknown on either side → do **not** skip (conservative).
- README already claims 同名同大小; keep that wording and make code match.

### `sync --disk-cache`

- Add CLI flag to `sync` mirroring `copy`.
- Thread `use_disk_cache` through `sync_directory` and Web/task_manager job runners the same way as single-file transfer.

### History clear

- `/api/history/clear` and UI “clear completed” must both use `clear_tasks(only_finished=True)` (or rename UI if intentional wipe-all—prefer finished-only).

---

## 7. Scheduler documentation (choice B)

- No new scheduler command.
- README + `job add --interval` help text: interval jobs fire **only while `pgsync web` is running** (TaskManager scheduler loop).

---

## 8. Testing strategy

- **Unit / default CI:** mocks or fakes; temp dirs for config/DB; Basic Auth 401/200; escape helper; Drive quote escape; skip-by-size; migration copy; temp cleanup on cancel.
- **Integration:** existing live Baidu/Drive tests marked `@pytest.mark.integration`; skip unless explicitly selected (e.g. env `PGSYNC_INTEGRATION=1` or `-m integration`).
- Web tests inject temp `Storage` DB path; never touch the developer’s real `tasks.db`.

---

## 9. Architecture / data flow (auth)

```
User → pgsync auth web → config.json (web.username, web.password_hash)
User → pgsync web → load config → reject if missing web auth
Browser → / (static OK) → /api/* with Basic → before_request verify → handlers
```

Config/DB:

```
~/.config/pan-gdrive-sync/
  config.json
  tasks.db          ← migrated from ~/.config/pangdrive/ if needed
  service_account.json
```

---

## 10. Implementation order

1. Config root constant + migration + SA chmod  
2. Web Basic Auth + `auth web` + README bind warnings  
3. XSS / DOM event delegation  
4. Disk-cache `finally` cleanup  
5. `auth_mode` + Baidu verify-before-save  
6. Drive escape, pagination, skip-by-size, `sync --disk-cache`, history clear  
7. Scheduler docs + test isolation / markers  

Each step: failing test first (TDD), then minimal fix, then green.

---

## Success criteria

- Unauthenticated `/api/*` returns 401 when web auth configured; server refuses to start without web auth.
- Crafted filenames/`<script>` cannot execute via UI list/toast/task error paths.
- Cancel during disk-cache download leaves no orphan temp file.
- New installs and migrated installs use only `~/.config/pan-gdrive-sync/` for config, DB, SA.
- `--skip` compares size; Drive names with `'` work; large folders paginate fully.
- `sync --disk-cache` available; README states interval jobs need Web.
- Default test run does not require live cloud credentials or pollute real task DB.
