# Task 2 Report: Daemon wait and scheduling

Status: completed.
Changes:
- Scheduled tasks retain their worker thread; one-shot CLI commands wait for triggered work.
- Schedules advance at trigger time, while completion records only final status and timestamp.
- Scheduler startup accepts the daemon poll interval; the web app starts its default scheduler.
- Worker SQLite connections are closed after task completion.
Tests: `python3 -m unittest discover -s tests -v` — 59 passed, 2 integration tests skipped.
Additional checks: `python3 -m compileall -q pangdrive` and `git diff --check` passed.
Concerns: none.
