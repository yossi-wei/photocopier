---
name: verify-photocopier
description: Drive the real photocopier CLI (ingest, process, flush, status, triage/resolve, doctor) and the scheduled launcher scripts/photocopier-run.sh end to end in a disposable sandbox, with a fake OneDrive and a genuinely mounted stand-in NAS, and capture evidence. Use it to prove a change to photocopier works the way the user runs it, not just that pytest passes, and never touch the live installation that launchd runs four times a day.
---

# verify-photocopier

photocopier is a CLI: it moves phone uploads from OneDrive (through rclone) into a spool, files them into an outbox shaped like the NAS library, then delivers them to the NAS share. The user touches it in two ways:

1. **The `photocopier` CLI**, run by hand: `doctor`, `ingest`, `process`, `flush`, `status`, `triage`, `resolve`, `validate`. This is the primary surface.
2. **The scheduled launcher** `scripts/photocopier-run.sh`, which launchd runs at 05:00, 11:00, 17:00 and 23:00 as an installed copy at `~/.local/bin/photocopier-run`. The user only sees its emails and logs.

Everything here runs through one helper, `scripts/verify.sh` in this skill directory. Call it by its path:

```bash
V="$(git rev-parse --show-toplevel)/.claude/skills/verify-photocopier/scripts/verify.sh"
```

## The live installation is off limits

A real installation runs on this machine against the user's photos. A verification run must never read or write any of these:

- `~/.config/photocopier/config.toml` (real config) and `config.toml` in the repo root
- `~/.local/share/photocopier/` (real spool, ledger, venv)
- `~/.local/state/photocopier/` (real launcher state) and `~/.local/state/nas-jobs/lock` (lock shared with the nightly backup)
- `/Volumes/media` (the real NAS share) and the launchd job `local.photocopier`
- the real `rclone` binary and the `onedrive:` remote

`verify.sh` makes this hold. Every CLI call passes `-c <sandbox config>` and clears `PHOTOCOPIER_SPOOL`, `PHOTOCOPIER_CONFIG` and `PHOTOCOPIER_SCHEDULED` first. It refuses to run if any absolute path in the sandbox config points outside the sandbox, or if the config's rclone is anything other than `tests/fake_rclone/rclone`. Never run a bare `photocopier` command, or the checkout's `.venv/bin/photocopier`, during verification: without `-c`, config lookup falls back to the real config. (The checkout's `.venv` console script also fails with `ModuleNotFoundError` right now, so it proves nothing either way.)

Sandboxes are isolated from each other. Each `up` creates its own run directory, its own disk image and its own lock, so several runs can exist side by side without conflict.

## Launch

```bash
RUN="$("$V" up)"      # prints the run directory; takes ~5 s
```

`up` builds a usable instance from nothing:

- **Build:** `uv venv` + `uv pip install <repo>`, a non-editable install of the checkout, the same way production is installed.
- **Fake OneDrive:** `RUN/sandbox/remote/`, served by the repo's `tests/fake_rclone/rclone`. It holds two phone folders seeded from `tests/fixtures/` plus three extra files (contents under "Seed data" below).
- **Stand-in NAS:** a 64 MB sparse HFS+ image, `hdiutil attach`-ed at `RUN/sandbox/nas` with `photos/`, `video/` and `.photocopier-marker`. It is a **real mount**, so the app's mount guard (`os.path.ismount` + marker) runs unpatched.
- **Config:** `RUN/sandbox/config.toml`, every path inside the sandbox, no cutoffs, 1 GB spool cap.
- **Notify stub:** `RUN/sandbox/notify.sh` appends every launcher email to `RUN/evidence/notify.log`.

Ready means `"$V" doctor "$RUN"` exits 0 (next section). There is no server: every drive is a short-lived CLI call.

### Seed data (fake OneDrive)

| Source | Remote file | Expected outcome after `process` |
|---|---|---|
| phone-1 `Camera Roll/` | `only_datetime.jpg` | photos/2024/03/only_datetime_phone-1.jpg (exif) |
| | `only_digitized.jpg` | photos/2023/07/only_digitized_phone-1.jpg (exif) |
| | `VID_20260315_101500.mp4` | video/2026/VID_20260315_101500_phone-1.mp4 (filename) |
| | `1J1A3188.jpg`, `garbage_date.jpg`, `zeroed.jpg` | triage `no_date` |
| | `notes.xyz` | triage `unknown_type` |
| | `.DS_Store` | skipped as junk at ingest |
| phone-2 `Pictures/Camera Roll/` | `PXL_20261101_021009424.jpg` | photos/2026/10/PXL_20261101_021009424_phone-2.jpg (exif: EXIF says 2026-10-31 and outranks the filename) |

## Doctor

```bash
"$V" doctor "$RUN"
```

Doctor is read-only. Run it first, and again whenever something looks off. It checks:

- every config path is inside the sandbox
- the image is attached at `RUN/sandbox/nas`, and the marker is present
- the installed build is present and still matches the checkout (HEAD plus a fingerprint of uncommitted changes to `photocopier/`, `scripts/`, `pyproject.toml`)
- the package in the sandbox venv is still the one `up` installed (a fingerprint of its `.py` files), so a hand-run `uv pip install` into the venv is caught
- `photocopier doctor` passes against the sandbox config

If doctor says the checkout changed or the installed build differs, the sandbox is testing other code. Run `down`, then `up` again. Never install into `RUN/sandbox/venv` by hand.

To probe the installed code directly, run its Python from outside the checkout, `cd / && RUN/sandbox/venv/bin/python -I -c ...`. Run from the checkout, `import photocopier` finds the checkout's own `photocopier/` folder instead of the venv, and hides any difference.

`photocopier validate PATH` needs `YYYY` folders directly inside PATH, so point it at `nas/photos` or `nas/video`, not the share. It exits 1, and names the right folder when it checks 0 files.

## Drive

```bash
"$V" pc "$RUN" <photocopier args...>    # e.g. ingest --dry-run, process, flush, status --verify
"$V" launcher "$RUN" [VAR=value ...]     # one scheduled pass, e.g. BACKLOG_WARN_DAYS=0 PHOTOCOPIER_DRY_RUN=1
"$V" unmount "$RUN" / "$V" remount "$RUN"   # take the NAS away and bring it back
```

- `pc` runs the sandbox's installed `photocopier` with `-c` the sandbox config and prints the output. It appends the command, combined stdout+stderr and `[exit N]` to `RUN/evidence/transcript.log`, and returns the command's exit code.
- `launcher` runs the checkout's `scripts/photocopier-run.sh` with every setting pointed into the sandbox: `PHOTOCOPIER_BIN`, `PHOTOCOPIER_CONFIG`, `PHOTOCOPIER_SPOOL`, `NAS_MOUNT`, `STATE_DIR`, `NAS_JOBS_LOCK`, `NOTIFY_CMD`, and `NAS_URL=""` so it never mounts anything. It copies the run log into evidence.
- To change the fake OneDrive, write files under `RUN/sandbox/remote/<source path>/`. That is the user's phone uploading.
- To change the stand-in NAS, write under `RUN/sandbox/nas/`. That is the user filing something by hand, which `resolve --filed-at` expects.

The per-feature recipes are in [`features/README.md`](features/README.md). Read the index, then follow the feature file for what you changed. When a feature lists several entry points, drive every one of them.

## Evidence

```bash
"$V" snapshot "$RUN" <label>    # writes RUN/evidence/NN-<label>.txt and prints its path
```

A snapshot records:

- every file, with its size, under the fake remote, the spool and the NAS
- the ledger's `items` table (state, dest_path, resolved_date, date_source, triage_reason)
- the ledger's `runs` table

Proof standards:

- **Bracket the action.** Take a snapshot before the action under test and one after. The transcript entry for the command sits between them. A final screen alone is not proof.
- **Check side effects as well as output.** "delivered 4 file(s)" is a claim. The proof is the files under `nas/` at the expected paths and sizes, the ledger rows in `delivered`, and the spool reclaimed. `status --verify` is a second, independent read of the NAS.
- **Use the user's path.** Use the CLI commands and the launcher. Do not import modules and call functions, and do not edit the ledger with sqlite to set up a state the CLI can reach.
- **The fake is allowed in two places only.** The fake rclone stands in for OneDrive, and the disk image stands in for the SMB share. Both sit at boundaries the code already treats as external. Mock nothing else.
- **Observe what a dry run skips.** Do not trust the flag's name. `ingest --dry-run` downloads nothing, but it still creates the spool directories and `ledger.db` and records a `runs` row. `flush --dry-run` still enforces the mount guard.

Evidence lives at `RUN/evidence/`, under `${PHOTOCOPIER_VERIFY_HOME:-$HOME/.local/state/photocopier-verify}/runs/<timestamp>-<pid>/evidence/`. It holds `transcript.log`, the `NN-*.txt` snapshots, `notify.log`, `run-*.log` and `run.env` (build identity). It survives `down`.

## Cleanup

```bash
"$V" down "$RUN"     # detach this run's image, delete RUN/sandbox; RUN/evidence is kept
"$V" ls              # every run: LIVE (image attached), sandbox present, or evidence only
```

`down` acts only on the run it is given. It detaches the image at that run's own mount point and refuses to delete a sandbox whose image will not detach. It never kills processes by name and never touches the real installation. Run `down` after every attempt, failed ones included. A failed `up` cleans up after itself.

If a session crashed mid-run, use `"$V" ls` to find `LIVE` runs, then `down` each one. `mount | grep photocopier-verify` shows any image still attached.

## Known gaps

- **The launcher never delivers in the sandbox.** Its `is_mounted` check looks for an `smbfs` mount at `NAS_MOUNT`, and the disk image is HFS+. In the sandbox the launcher always takes the "NAS not mounted, flush skipped" branch. Prove delivery through `pc flush`. Treat the launcher's flush branch, auto-mount (`NAS_URL`) and the `/Volumes/media-1` detection as unverified unless a real SMB share is involved, which needs the user.
- **The real rclone and OneDrive are never exercised.** A change to `photocopier/rclone.py` that depends on real rclone flags or output needs `rclone lsjson` against the real remote, read-only, and only with the user's go-ahead.
- **This is macOS only** (`hdiutil`). The NAS port in phase 5 will need a different stand-in, such as a loopback or tmpfs mount.
