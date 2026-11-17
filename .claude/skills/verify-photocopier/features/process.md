# Process

Process files each ingested file into the outbox, an exact replica of where it will land on the NAS. Photos go to `photos/YYYY/MM/` and videos to `video/YYYY/`, with the phone's suffix added to the name. Anything without a trustworthy date, or of an unknown type, goes to triage instead of being guessed at.

## Sub-features

- `process-dry` lists a sample of what would be staged without moving anything.
- `process-date` resolves the date by EXIF first, then filename, then source path. File mtime is never used.
- `process-layout` uses `photos/YYYY/MM/<stem>_<suffix>.<ext>` and `video/YYYY/<stem>_<suffix>.<ext>`.
- `process-triage-nodate` routes files with no resolvable date to `spool/triage/no_date/<source>/`.
- `process-triage-type` routes unknown extensions to `spool/triage/unknown_type/<source>/`.

## How to get to it (user POV)

- `photocopier process --dry-run`
- `photocopier process`
- Indirectly: the second step of every scheduled launcher pass.

## Driving it with verify.sh

Preconditions:

- A fresh sandbox, then `"$V" pc "$RUN" ingest`, which ingests 8 files.

- **Before.** Run `"$V" snapshot "$RUN" before-process`. Eight `ingested` rows, and `spool/outbox` is empty.
- **Dry run.** Run `"$V" pc "$RUN" process --dry-run`. Exit 0, `would file 4 file(s)`, `triaged 4`, and a sample of staged paths. A snapshot still shows eight `ingested` rows and an empty outbox.
- **Process.** Run `"$V" pc "$RUN" process`. Exit 0 with `filed 4 file(s)`, `triaged 4`, `date source exif=3, filename=1, none=3` and `(no_date=3, unknown_type=1)`.
- **Proof.** Run `"$V" snapshot "$RUN" after-process`. It should show:
  - in `spool/outbox/`: `photos/2024/03/only_datetime_phone-1.jpg`, `photos/2023/07/only_digitized_phone-1.jpg`, `photos/2026/10/PXL_20261101_021009424_phone-2.jpg` and `video/2026/VID_20260315_101500_phone-1.mp4`
  - in `spool/triage/`: `1J1A3188.jpg`, `garbage_date.jpg` and `zeroed.jpg` under `no_date/phone-1/`, and `notes.xyz` under `unknown_type/phone-1/`
  - ledger rows `processed`, each with `dest_path` and `date_source`; triaged rows carrying their `triage_reason`
- **Second view.** Run `"$V" pc "$RUN" status`. It shows `processed 4`, `triaged 4` and `4 file(s) staged in the outbox awaiting delivery`.

## Gotchas

- `PXL_20261101_...` files to `2026/10`, not `2026/11`. The EXIF capture date (2026-10-31) outranks the filename by design. Do not "fix" the expectation.
- `1J1A3188.jpg` is a real camera filename with no date in it and no EXIF date, so `no_date` is correct.
- The 1.0 GB spool cap in the sandbox config is far above the seed size, so cap behaviour needs a bigger file or a lower `cap_gb`.
- Process uses `rename()` within the spool. The guard that refuses a spool split across filesystems is not exercised, because the sandbox spool is on one volume.
