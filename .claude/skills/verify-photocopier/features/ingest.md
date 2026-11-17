# Ingest

Ingest pulls files the phones have uploaded to OneDrive into the local spool. It downloads only what the ledger has not seen before, skips OS detritus, and can be limited to one phone or a date window. Running it twice never fetches anything twice.

## Sub-features

- `ingest-dry` reports what would come down without downloading.
- `ingest-run` downloads new files into `spool/incoming` and records them as `ingested`.
- `ingest-idem` downloads nothing on a second run: the ledger owns "what's new".
- `ingest-junk` skips `.DS_Store` and dotfiles.
- `ingest-source` limits the run to one configured phone with `--source ID`.
- `ingest-window` admits only files dated in `[--since, --until)` for one run.

## How to get to it (user POV)

- `photocopier ingest --dry-run`
- `photocopier ingest`, optionally with `--source phone-2`
- `photocopier ingest --since YYYY-MM-DD --until YYYY-MM-DD` (catching up a missed month)
- Indirectly: every scheduled launcher pass runs `ingest` first (see [scheduled-run](./scheduled-run.md)).

## Driving it with verify.sh

Preconditions:

- A fresh sandbox, doctor passing, nothing ingested.

- **Before.** Run `"$V" snapshot "$RUN" before-ingest`. The snapshot shows `(no ledger yet)` and nine remote files.
- **Dry run.** Run `"$V" pc "$RUN" ingest --dry-run`. Exit 0. phone-1 shows `would ingest 7 file(s)` with `(8 seen, ... 1 junk)`, phone-2 shows `would ingest 1 file(s)`, and the total is `would ingest 8 file(s)`. A second snapshot shows `spool/incoming` empty.
- **Ingest.** Run `"$V" pc "$RUN" ingest`. Exit 0 and `total: ingested 8 file(s), 4.0 KB`. Snapshot `after-ingest`: eight ledger rows in state `ingested`, and the files under `spool/incoming/`. There is no `.DS_Store` row.
- **Idempotence.** Run `"$V" pc "$RUN" ingest` again. Exit 0, `total: ingested 0 file(s)`, phone-1 shows `7 already in ledger`. The ledger still has eight rows.
- **New upload.** Run `cp "$RUN/sandbox/remote/Camera Roll/only_datetime.jpg" "$RUN/sandbox/remote/Camera Roll/IMG_20260202_120000.jpg"`, then `"$V" pc "$RUN" ingest`. Exactly `ingested 1 file(s)` for phone-1 and a ninth ledger row.
- **One source.** On a fresh sandbox, run `"$V" pc "$RUN" ingest --source phone-2`. Only phone-2 is listed and the ledger holds one row. `--source nope` exits 1 and names the configured sources.
- **Window.** On a fresh sandbox, run `"$V" pc "$RUN" ingest --dry-run --since 2026-04-01 --until 2026-12-01`. Exit 0. The output starts with `window: 2026-04-01 to 2026-12-01 (end exclusive); undated files still come down`. phone-1 shows `would ingest 6 file(s)` with `1 before 2026-04-01`, the VID dated 2026-03-15 by its filename. The total is `would ingest 7 file(s)`.
- **Window validation.** Run `"$V" pc "$RUN" ingest --since 2026-02-01 --until 2026-01-01`. Exit 2 with `error: --since must be earlier than --until`.

## Gotchas

- `ingest --dry-run` still creates `spool/`, `ledger.db` and a `runs` row with `dry_run=1`. It does not download anything. Check `spool/incoming` to confirm.
- The `--since`/`--until` window and source cutoffs can only judge files whose date is known before download, from the filename or the folder path. EXIF-only files are admitted and dated at `process`. The sandbox config has no cutoffs, so set `cutoff` in `RUN/sandbox/config.toml` to test one. Stay inside the sandbox: `pc` refuses a config whose paths escape it.
- The fake rclone hashes with MD5 but labels it `QuickXorHash`. Real hash behaviour is not exercised.
- `FAKE_RCLONE_FAIL_ON=<relative path>` makes the fake `copy` fail at that file, which is how to drive partial-transfer recovery. `pc` clears that variable, so a failure-injection run has to invoke the sandbox binary directly with the same `-c` and environment. Write down the exact command you used.
