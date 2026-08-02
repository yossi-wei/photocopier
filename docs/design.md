# Technical design — photocopier

**Related:** [PRD](PRD.md) · [Decision log](decisions.md) · [PRFAQ](PRFAQ.md)

Pulls camera-upload photos and videos from OneDrive into a local spool, restructures
them into an exact replica of the NAS layout, then delivers to the NAS when it is
reachable. Ingest and delivery are decoupled so weeks of travel are a non-event.

This document covers *how*. The [PRD](PRD.md) covers *what and why*, including goals,
non-goals, and success metrics. Decisions with their evidence live in the
[decision log](decisions.md); they are referenced here as D1–D8 rather than re-argued.

License: MIT. Python 3.11+, `uv`, `rclone`.

---

## 1. Design constraints

Established during planning:

| Constraint | Decision |
|---|---|
| Sources | 2 for now, config-driven list, each with a name suffix |
| Naming | `<original>_<suffix>.<ext>` — suffix last, preserves chronological sort |
| Photos destination | `photos/YYYY/MM/` |
| Video destination | `video/YYYY/` — **yearly only, no monthly folders** |
| Backfill | Cutoff date per source, set in config; earlier material handled manually |
| Undated files | Triage area for review, never auto-filed |
| OneDrive source | Never modified. Read-only. |
| Spool cap | 20 GB, hard stop with warning |
| Notifications | Deferred |

Measured volumes (planning, 2026): `Camera Roll` 0.8–3.2 GB/mo, `Pictures/Camera Roll`
~0.43 GB/mo. Combined ~2 GB/mo typical, ~4 GB peak. Videos are 1.4% of files but 30%
of bytes (~110 MB each vs 3.4 MB per photo). A six-week trip spools 3–6 GB.

---

## 2. Pipeline

```
[1] INGEST                       rclone lsjson → diff vs ledger → rclone copy --files-from-raw
     daily                       OneDrive → <spool>/incoming/
                                 ledger: discovered → ingested

[2] PROCESS                      resolve date → apply suffix → rename() into replica tree
     daily                       incoming/ → outbox/photos/YYYY/MM/
                                           → outbox/video/YYYY/
                                           → triage/          (undated, conflicts)
                                 ledger: ingested → processed | triaged

[3] FLUSH                        guards → rclone move (verified) → remove from spool
     on NAS availability         outbox/ → /Volumes/media/
                                 ledger: processed → delivered
```

Stages 1 and 2 run together daily. Stage 3 runs whenever the NAS is genuinely
reachable and the outbox is non-empty.

### Why rclone is a transfer engine, not a sync engine

rclone decides what to copy by comparing source against destination. The spool is
flushed after delivery, so on the next run rclone would see an empty destination and
re-download everything from OneDrive.

`--max-age` is the tempting fix and is unsafe: miss a few days (Mac off, travel,
crash) and files are silently skipped forever with no signal.

Therefore **the ledger owns the "already seen" decision**, and rclone is invoked with
an explicit file list. This is the load-bearing correction in the whole design.

---

## 3. Layout

```
photocopier/
  cli.py            subcommands: ingest, process, flush, run, status, reprocess, doctor
  config.py         TOML load, validation, defaults, env overrides
  ledger.py         SQLite state store + state machine
  rclone.py         subprocess wrapper: lsjson, copy, move; exit-code → typed errors
  sources.py        enumeration, cutoff filtering, new-item diffing
  dates.py          date resolution chain
  layout.py         destination path mapping + naming
  triage.py         triage reasons, manifest, reprocess
  spool.py          spool paths, cap enforcement, sync-root guard
  guards.py         mount liveness, free space, preflight
tests/
  fixtures/         sample trees, canned lsjson output
  fake_rclone/      stub binary injected on PATH for hermetic integration tests
```

### Spool

```
<spool>/
  incoming/         raw, as downloaded, source-relative paths
  outbox/           exact replica of NAS layout — what will land, verbatim
    photos/YYYY/MM/
    video/YYYY/
  triage/           needs human decision, never auto-flushed
  ledger.db
```

`incoming/` and `outbox/` **must be on the same filesystem** so stage 2 is `rename()`,
not a byte copy. Enforced at startup, not assumed.

Resolution order: `$PHOTOCOPIER_SPOOL` → `[spool].path` in config →
`~/.local/share/photocopier/spool`. Same on macOS and the NAS. Never a temp directory:
`/tmp` is purged after ~3 days idle and `$TMPDIR` on boot, which would destroy a spool
mid-travel.

---

## 4. Config

`config.toml` — gitignored. `config.example.toml` — committed, placeholders only.
TOML via stdlib `tomllib`, no dependency.

```toml
[spool]
path   = "~/.local/share/photocopier/spool"   # $PHOTOCOPIER_SPOOL wins
cap_gb = 20

[destination]
photos_root  = "/Volumes/media/photos"        # YYYY/MM
video_root   = "/Volumes/media/video"         # YYYY, flat
mount_point  = "/Volumes/media"
mount_marker = ".photocopier-marker"          # must exist and be readable

[rclone]
remote    = "onedrive"
transfers = 4
bwlimit   = ""

[[source]]
id     = "phone-1"
path   = "Camera Roll"
suffix = "phone-1"
cutoff = "2026-01-01"

[[source]]
id     = "phone-2"
path   = "Pictures/Camera Roll"
suffix = "phone-2"
cutoff = "2026-01-01"

[classify]
video_exts = ["mp4", "mov", "m4v", "avi"]
photo_exts = ["jpg", "jpeg", "heic", "png", "dng", "gif"]
```

Adding a phone is a config block. No code change.

---

## 5. Ledger

SQLite, single `items` table.

```sql
CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  source_id     TEXT NOT NULL,
  src_path      TEXT NOT NULL,      -- path within the rclone remote
  src_size      INTEGER NOT NULL,
  src_modtime   TEXT NOT NULL,
  src_hash      TEXT,               -- QuickXorHash from lsjson
  state         TEXT NOT NULL,      -- discovered|ingested|processed|delivered|triaged|failed
  spool_path    TEXT,               -- NULL once delivered
  dest_path     TEXT,               -- NAS-relative
  resolved_date TEXT,
  date_source   TEXT,               -- exif|filename|srcpath|none
  triage_reason TEXT,
  discovered_at TEXT, ingested_at TEXT, processed_at TEXT, delivered_at TEXT,
  attempts      INTEGER DEFAULT 0,
  last_error    TEXT,
  UNIQUE (source_id, src_path, src_hash)
);
```

Identity is `(source_id, src_path, src_hash)`. Same path with changed content is a
genuinely new item, not an update — it gets its own row and its own delivery, and any
resulting destination collision goes to triage rather than overwriting.

The ledger survives the flush. That is its entire reason for existing.

---

## 6. Date resolution

Applied in stage 2, when the file is local:

1. **EXIF `DateTimeOriginal`** — jpg/heic. Authoritative.
2. **Filename pattern** — `PXL_YYYYMMDD_*`, `YYYYMMDD_HHMMSS`, `IMG_YYYYMMDD_*`.
   Covers mp4, which carries no EXIF. Both current sources use these.
3. **Source path** `YYYY/MM/` from the OneDrive tree.
4. **Triage**, reason `no_date`.

**File mtime is deliberately excluded.** OneDrive and rclone rewrite it, so it would
produce confidently wrong answers — worse than no answer, because nothing routes to
triage for review.

Known case from the existing data: `Pictures/Camera Roll/1969/` holds files with
zeroed timestamps. These must land in triage, not in `photos/1969/`.

---

## 7. Destination mapping

| Input | Destination |
|---|---|
| photo ext, date resolved | `photos/YYYY/MM/<stem>_<suffix>.<ext>` |
| video ext, date resolved | `video/YYYY/<stem>_<suffix>.<ext>` |
| no date | `triage/no_date/` |
| unknown ext | `triage/unknown_type/` |

### Conflict policy at the destination

The NAS tree is already populated by prior manual work.

- Target absent → deliver.
- Target present, **identical** (size + hash) → already delivered; mark delivered, drop
  from spool. Not an error.
- Target present, **different** → `triage/dest_conflict/`. Never overwrite, never
  silently skip.

`--ignore-existing` alone is rejected: it papers over the third case, which is the only
one that matters.

Note `PXL_20260601_223332404~2.jpg`-style names are legitimate distinct edits produced
by the phone and must survive as separate files.

---

## 8. Triage

Triage is a workflow, not a dead-letter folder. Each triaged file gets a manifest entry
recording why it landed there and what was tried.

```
triage/
  no_date/
  dest_conflict/
  unknown_type/
  manifest.json
```

Two ways out:

- **One-off** — a decision recorded per item (`photocopier resolve <id> --date 2024-07-04`).
- **New rule** — a pattern added to config, then `photocopier reprocess` re-runs stage 2
  over the whole triage area.

Rules are data, not code, so adding one is a config edit. This keeps the iterate-with-Claude
loop cheap: inspect triage, decide whether it's a one-off or a pattern, encode it.

---

## 9. Guards

Each is a startup precondition with a dedicated test.

1. **Mount liveness.** If the share is unmounted, `/Volumes/media` still exists as an
   empty local directory and a naive flush writes to the boot SSD and reports success.
   Require `os.path.ismount()` **and** a readable marker file. This is the single most
   dangerous failure mode in the system.
2. **Spool not inside a sync root.** Refuse to run if the spool resolves under OneDrive,
   iCloud Drive, or Dropbox — otherwise the tool feeds its own input.
3. **Same filesystem.** `incoming/` and `outbox/` must share a device, or stage 2 silently
   becomes a full byte copy.
4. **Spool cap.** Ingest stops before exceeding 20 GB. Stop and warn; never partially
   delete to make room.
5. **Free space.** Preflight against the largest pending item.

The spool is never the only copy — OneDrive retains everything upstream. A lost or
corrupted spool costs a re-download, not data. This is why aggressive flush-on-verify
is safe, and why the spool needs no backup.

---

## 10. Tests

No network in any test. `fake_rclone` is a stub binary placed on `PATH` that emits canned
`lsjson` and copies from a fixture tree, so the full pipeline runs hermetically.

**Unit**
- Date resolution — table-driven across all four tiers, including the 1969 zeroed-timestamp
  case and mp4-with-no-EXIF.
- Layout mapping — photo vs video, `YYYY/MM` vs `YYYY`, suffix placement before extension.
- Cutoff filtering, per source.
- Config — defaults, env override precedence, validation errors, missing/duplicate source ids.
- Cap arithmetic at and across the boundary.

**Ledger**
- State transitions, legal and illegal.
- Unique constraint: same path + same hash is a no-op; same path + different hash is a new row.
- Resume after a partial run.

**Guards** — one test each
- `test_unmounted_share_is_refused` — the empty-directory trap.
- `test_spool_inside_sync_root_is_refused`.
- `test_cross_filesystem_spool_is_refused`.
- `test_cap_stops_ingest_without_deleting`.

**Regression**
- `test_flush_then_ingest_does_not_redownload` — the flaw the ledger exists to prevent.
  Non-negotiable.

**Integration**
- Idempotency: run the pipeline twice → identical tree, no duplicates, zero transfers on
  the second pass.
- Crash safety: interrupt between process and flush, and between flush and ledger update.
  Re-run must converge, with no partial files at the destination (temp + atomic rename).
- Conflict policy: identical target dropped, differing target triaged.
- Triage round-trip: undated file → triage → rule added → `reprocess` → filed correctly.

**CI** — GitHub Actions, pytest on macOS and Ubuntu.

---

## 11. Build order

Each phase is independently verifiable before the next exists.

| Phase | Deliverable | Verified by | State |
|---|---|---|---|
| 0 | Repo skeleton, MIT LICENSE, pyproject, CI, config loader, `doctor` | `doctor` reports a clean environment | ✅ |
| 1 | Ledger + `ingest`, `--dry-run` first | Enumerates both sources, respects cutoff, downloads nothing twice | ✅ |
| 2 | `process` — dates, layout, naming, triage | `outbox/` is a byte-exact replica of intended NAS layout | |
| 3 | `flush` — guards, verified move, conflict policy | Delivers to NAS; survives an unmounted share | |
| 4 | launchd scheduling, full `status` metrics, initial backfill | Runs unattended for a week | |
| 5 | NAS port — destination adapter, drop stage 3 | Same code, local destination | |

### Phase 1 as built

Two defects surfaced during phase 1 that are worth recording, since both were the kind
this design is meant to prevent:

- **Failed items were treated as known.** `filter_new` excluded anything already in the
  ledger regardless of state, so a file that failed to transfer would never be offered
  again — a permanent, invisible skip. `known_keys` now excludes FAILED explicitly, with
  a dedicated test.
- **The reporting path was untested.** The pipeline was correct but `render()` raised an
  `AttributeError`, which no unit test caught because none of them exercised output. A
  run that works and then crashes while printing is still a failed run; `tests/test_cli.py`
  now covers the command surface.

Date resolution currently implements the cheap tiers only — filename patterns and the
source path — which is all that is available before download. EXIF becomes the highest-
priority tier in phase 2, when the file is local.

Phase 5 note: the spool exists because the Mac is the ingest point and cannot always
reach the NAS. Once ingest runs on the NAS itself, it pulls from OneDrive continuously
regardless of where you are — the travel problem stops existing rather than being
solved. Stages 1 and 2 port unchanged; stage 3 is deleted. The spool work is not
wasted, but it is the Mac-hosted form of the pipeline, not a permanent fixture.

---

## 12. Repository

```
README.md               problem, architecture, key decisions, quickstart
LICENSE                 MIT
config.example.toml     placeholders only, no real paths
docs/
  PRD.md                problem, goals, non-goals, metrics, requirements, risks
  design.md             this document
  decisions.md          D1–D8 with evidence
  PRFAQ.md              parody of the format; framed as such in the document itself
photocopier/            implementation
tests/                  including fake_rclone/ stub
```

- `.gitignore` — `config.toml`, `ledger.db`, spool contents, `.venv`.
- No secrets in the repo. rclone credentials stay in rclone's own config, referenced by
  remote name.
- No hardcoded usernames, home paths, or hostnames anywhere — all machine-specific values
  come from config or environment.

### Metrics instrumentation

`photocopier status` computes the PRD's success metrics directly from the ledger:
manual-intervention rate, triage rate, delivery completeness, and the unattended-run
streak. The metrics in the PRD and the ones the tool reports are the same metrics — this
is deliberate, and it is what keeps §5 of the PRD from being decorative.

---

## 13. Open items

- **Notifications** — deferred by decision. Until then, a daily run that stops working is
  invisible. Worth revisiting before the tool is trusted unattended.
- **Metered-network video deferral** — videos are 30% of bytes at ~110 MB each. Optionally
  hold them until an unmetered connection. Low priority at current volumes; noted so it
  stays a choice rather than an oversight.
- **Cutoff dates** — parameter exists in config; values to be set before the first real run.
