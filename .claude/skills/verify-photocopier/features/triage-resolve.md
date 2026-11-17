# Triage and resolve

`triage` lists every file waiting for a human decision, with its id, the reason and where the copy sits. `resolve` closes one out: `--filed-at` once the user has filed it in the library themselves, or `--discard` to drop it. The ledger keeps the row either way, so the file is never fetched again.

## Sub-features

- `triage-list` shows one entry per pending file with id, reason, source, file path and a "why" line.
- `resolve-discard` marks the row `discarded` and removes the triaged copy.
- `resolve-filed` marks the row `delivered` at the given library path and removes the triaged copy.
- `resolve-filed-missing` refuses with exit 1 when nothing exists at `--filed-at`.
- `resolve-dry` reports what would change without changing it.

## How to get to it (user POV)

- `photocopier triage`
- `photocopier resolve ID --discard [--dry-run]`
- `photocopier resolve ID --filed-at photos/YYYY/MM/<name>` (or an absolute path under `photos_root`/`video_root`) `[--dry-run]`
- `photocopier status` shows the `awaiting review` counts.

## Driving it with verify.sh

Preconditions:

- A fresh sandbox, then `"$V" pc "$RUN" ingest` and `"$V" pc "$RUN" process`. Four files are triaged: ids 1, 3 and 7 as `no_date`, and id 4 as `unknown_type`.

- **List.** Run `"$V" pc "$RUN" triage`. Exit 0 and `4 item(s) awaiting review`, with entries `#1 no_date phone-1 1J1A3188.jpg`, `#3 ... garbage_date.jpg`, `#4 unknown_type ... notes.xyz` and `#7 ... zeroed.jpg`.
- **Discard dry run.** Run `"$V" pc "$RUN" resolve 4 --discard --dry-run`. The output says `#4 would mark discarded`. `spool/triage/unknown_type/phone-1/notes.xyz` still exists.
- **Discard.** Run `"$V" pc "$RUN" resolve 4 --discard`. `#4 marked discarded` and the copy is removed.
- **Filed, but not yet filed.** Run `"$V" pc "$RUN" resolve 1 --filed-at photos/2019/05/1J1A3188.jpg`. Exit 1 with `nothing at .../nas/photos/2019/05/1J1A3188.jpg. File it there first`.
- **Filed.** Do what the user would do: `mkdir -p "$RUN/sandbox/nas/photos/2019/05" && cp "$RUN/sandbox/spool/triage/no_date/phone-1/1J1A3188.jpg" "$RUN/sandbox/nas/photos/2019/05/"`. Then run `"$V" pc "$RUN" resolve 1 --filed-at photos/2019/05/1J1A3188.jpg`, which reports `#1 marked delivered at photos/2019/05/1J1A3188.jpg`.
- **Proof.** Run `"$V" pc "$RUN" triage`, which shows `2 item(s)` (#3, #7). Run `"$V" snapshot "$RUN" after-resolve`: row 1 is `delivered` with `dest_path photos/2019/05/1J1A3188.jpg`, row 4 is `discarded`, and `spool/triage/` holds only `garbage_date.jpg` and `zeroed.jpg`.
- **Never refetched.** Run `"$V" pc "$RUN" ingest`, which ingests 0 files. The discarded and resolved files are still in the fake remote, but they are not downloaded again.

## Gotchas

- Ids are ledger ids. They depend on ingest order, so they only hold for the seed data on a fresh sandbox. Read them from `triage` output rather than hard-coding them after other changes.
- `spool/triage/manifest.jsonl` is append-only and keeps entries for resolved files. Use `triage` output, not the manifest, to see what is pending.
- `resolve --filed-at` only checks that a file exists at that path. It does not compare content. A wrong file at the right path is accepted.
