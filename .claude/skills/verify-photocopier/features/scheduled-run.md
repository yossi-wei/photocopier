# Scheduled run

The launcher `scripts/photocopier-run.sh` is what launchd runs four times a day. One pass takes a lock shared with the nightly backup, runs `ingest` and `process`, and runs `flush` only if the NAS share is mounted. Every step has a time limit. The user sees the result as email: one summary per calendar day, and at most one failure email per 24 hours for a repeated failure. The summary warns about triaged files and about undelivered files older than `BACKLOG_WARN_DAYS`.

## Sub-features

- `sched-pass` runs ingest then process, and logs to `STATE_DIR/run-*.log`.
- `sched-nas-away` skips flush when the NAS is not mounted, with the note "Files are safe in the spool and OneDrive".
- `sched-lock` skips the whole pass when a live process holds `NAS_JOBS_LOCK`, and takes over a stale lock.
- `sched-summary` sends one summary per calendar day; `STATE_DIR/summary-date` records the day.
- `sched-backlog-warn` adds `WARNING: undelivered files have been waiting N days` when the oldest undelivered file is at least `BACKLOG_WARN_DAYS` old.
- `sched-failure-dedupe` sends the same failure at most once per 24 hours.
- `sched-scheduled-flag` marks ledger `runs` rows as `scheduled=1`, which feeds the unattended-streak metric.
- `sched-dry` with `PHOTOCOPIER_DRY_RUN=1` passes `--dry-run` to every step and sends no email.

## How to get to it (user POV)

- launchd job `local.photocopier`, which runs the installed `~/.local/bin/photocopier-run`. Never trigger this during verification.
- Running `photocopier-run` by hand.
- In the sandbox: `"$V" launcher "$RUN" [VAR=value ...]`, which runs the checkout's script with every setting pointed into the sandbox.

## Driving it with verify.sh

Preconditions:

- A fresh sandbox with doctor passing. For the backlog sub-feature, also run `"$V" pc "$RUN" ingest` and `"$V" pc "$RUN" process` first, so 4 files are staged and undelivered.

- **Pass with the NAS away.** Run `"$V" launcher "$RUN" BACKLOG_WARN_DAYS=0`. It exits 0. The log shows `== ingest`, `== process` and `flush skipped: NAS not mounted at .../sandbox/nas`.
- **Summary email.** Run `cat "$RUN/evidence/notify.log"`. There is exactly one `=== SUBJECT: photocopier daily summary on <host>: ...` entry. Its body has the NAS-away note, the STATUS block with `4 file(s) staged in the outbox awaiting delivery`, and THIS RUN. `RUN/sandbox/state/summary-date` holds today's date.
- **Backlog warning.** In that same email, expect `WARNING: undelivered files have been waiting 0 days.` because `BACKLOG_WARN_DAYS=0` and 4 files are undelivered.
- **Subject.** Expect `...: needs attention` when there are triaged files or a warning, and `...: OK` otherwise.
- **Second pass the same day.** Run `"$V" launcher "$RUN"` again. No new summary goes to `notify.log`, because one was already sent today and there are no failures.
- **Scheduled flag.** Run `"$V" snapshot "$RUN" after-launcher`. The `runs` rows from the launcher have `scheduled = 1`, and `status` shows `unattended streak 1 day`.
- **Lock held.** Run `mkdir "$RUN/sandbox/lock/lock" && echo $$ > "$RUN/sandbox/lock/lock/pid" && echo test > "$RUN/sandbox/lock/lock/job"`, then `"$V" launcher "$RUN"`. The log line is `skipped: another NAS job (test, pid ...) holds the lock` and no new `runs` rows appear. Remove the lock with `rm -rf "$RUN/sandbox/lock/lock"`.
- **Dry run.** Run `"$V" launcher "$RUN" PHOTOCOPIER_DRY_RUN=1`. No new `notify.log` entry; the ledger `runs` rows have `dry_run = 1`.

## Gotchas

- **Two past regressions to re-check after any change to the reporting block.** Both were fixed on 2026-11-17:
  - **Backlog warning.** It silently never fired. `sqlite3 -readonly` cannot open the WAL-mode ledger once SQLite has removed its `-wal`/`-shm` files, and the error was being discarded. The query must open the ledger without `-readonly`. Errors from it now go to the run log, so after a pass, `grep -i 'unable\|error' "$RUN"/evidence/run-*.log` should find nothing.
  - **Subject.** It once read `needs attention1`. To drive the `OK` subject, deliver everything, resolve every triaged file, run `rm "$RUN/sandbox/state/summary-date"` to start a new day, and run the launcher again.
- **Flush is never reached.** The launcher's `is_mounted` requires an `smbfs` mount at `NAS_MOUNT`, and the sandbox NAS is an HFS+ image, so every sandbox pass takes the NAS-away branch. The flush step, auto-mount through `NAS_URL`, and the `/Volumes/media-1` detection stay unverified until the user tests them on the real share. Never point `NAS_MOUNT` or `NAS_URL` at the real share from a sandbox.
- **Step timeouts** of 6 h for ingest and flush and 1 h for process cannot be shortened by environment variable, so the timeout path is not driven.
- The launcher calls `photocopier` without `-c`, so it finds the config through `PHOTOCOPIER_CONFIG`. The `launcher` helper sets that to the sandbox config. Never run the script without the helper.
