# Flush

Flush delivers the staged outbox to the NAS, checks each file on arrival, and then reclaims the spool. It refuses to run at all when the share is not truly mounted, because an unmounted mount point is an empty local folder and delivering into it would silently fill the boot disk. `status --verify` later re-checks that every delivered file is still on the NAS at the size it arrived.

## Sub-features

- `flush-dry` reports what would be delivered.
- `flush-deliver` copies the outbox into `photos_root`/`video_root`, marks rows `delivered`, and empties the outbox.
- `flush-guard-unmounted` refuses with exit 1 when `mount_point` is not a mount.
- `flush-guard-marker` refuses with exit 1 when the share is mounted but `.photocopier-marker` is missing.
- `flush-idempotent` reports `nothing staged for delivery` when there is nothing left to deliver.
- `status-verify` re-checks every delivered file on the NAS. Its `lost or overwritten` line must read `0 of N`.

## How to get to it (user POV)

- `photocopier flush --dry-run`
- `photocopier flush`
- `photocopier status --verify`
- Indirectly: the third step of a scheduled pass, which runs only when the real SMB share is mounted. The sandbox cannot reach that path; see [scheduled-run](./scheduled-run.md).

## Driving it with verify.sh

Preconditions:

- A fresh sandbox, then `"$V" pc "$RUN" ingest` and `"$V" pc "$RUN" process`, which stage 4 files.

- **Before.** Run `"$V" snapshot "$RUN" before-flush`. The outbox holds 4 files. `nas/` has only `.photocopier-marker`, plus `.fseventsd`, which is normal on an HFS+ image.
- **Unmounted refusal.** Run `"$V" unmount "$RUN"`, then `"$V" pc "$RUN" flush`. Exit 1 with `exists but is not a mount point — the share is not mounted`. `flush --dry-run` refuses the same way. `ls "$RUN/sandbox/nas"` is empty, so nothing was written to the local disk. Run `"$V" remount "$RUN"`.
- **Missing marker.** Run `rm "$RUN/sandbox/nas/.photocopier-marker"`, then `"$V" pc "$RUN" flush`. Exit 1 and the output names the marker. Restore it with `touch "$RUN/sandbox/nas/.photocopier-marker"`.
- **Deliver.** Run `"$V" pc "$RUN" flush`. Exit 0, `delivered 4 file(s), 2.1 KB`.
- **Proof.** Run `"$V" snapshot "$RUN" after-flush`. The four files are under `nas/photos/...` and `nas/video/2026/` at the same sizes as their remote originals. `spool/outbox` is empty. Four ledger rows are `delivered`.
- **Second view.** Run `"$V" pc "$RUN" status --verify`. It shows `delivered 4` and `lost or overwritten 0 of 4 delivered`.
- **Again.** Run `"$V" pc "$RUN" flush`. Exit 0, `nothing staged for delivery`.

## Gotchas

- `unmount` can fail with "resource busy" if a shell has its working directory inside `nas/`. `cd` out first.
- The sandbox NAS is HFS+ on a local image, not SMB. Behaviour specific to SMB (permission prompts, slow `fsync`, case folding on the share) is not exercised.
- A conflicting file already at the destination should go to triage as `dest_conflict` and nothing should be overwritten. To drive it, put a file with different content at the destination path under `nas/` before flushing.
