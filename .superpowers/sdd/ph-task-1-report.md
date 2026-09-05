# Task 1: Session Cookie Auth Report

## Scope completed

- Replaced browser use of `?auth=` Basic credentials with the HttpOnly
  `pgsync_session` cookie.
- Preserved HTTP Basic authentication for CLI and script clients.
- Removed browser creation and use of Basic authorization headers and removed
  `pgsync_auth` persistence. The UI only keeps the optional display username in
  `sessionStorage`; logout removes it and all legacy storage keys.
- Changed SSE to connect to `/api/tasks/events` with credentials and no query
  credential.

## Implementation

- Added `SessionStore`, a lock-protected in-memory opaque token store. Tokens
  are generated with `secrets.token_urlsafe(32)`, have a fixed 24-hour TTL,
  prune on validation after expiry, and can be revoked.
- Added `POST /api/session`, accepting JSON or form `username` and `password`.
  Valid credentials set `pgsync_session` with `HttpOnly`, `SameSite=Strict`,
  `Path=/`, and `Max-Age=86400`.
- Added authenticated `DELETE /api/session`, which revokes the presented
  session token and expires the cookie.
- Auth middleware now permits login only for `POST /api/session`, then accepts
  a valid session cookie or HTTP Basic credentials for all other API routes.
  It no longer reads query-string credentials.

## Tests added or updated

- Query-string Basic credentials are rejected.
- Session login sets the required cookie flags, authorizes a subsequent API
  request, and logout revokes access.
- Invalid session credentials are rejected without a Basic challenge.
- Session token creation, revocation, expiry, and pruning are covered.

## Verification

- `python3 -m unittest tests.test_hardening -v`: 35 passed.
- `python3 -m unittest discover -s tests -v`: 57 passed, 2 live-cloud tests
  skipped as intended.
- `node --check pangdrive/web/static/app.js`: passed.
- `python3 -m compileall -q pangdrive`: passed.
- `git diff --check`: passed.

## Concern

Sessions are deliberately process-local as requested. Restarting the web
process invalidates active browser sessions.
