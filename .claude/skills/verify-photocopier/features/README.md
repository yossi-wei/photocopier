# photocopier verification map

This directory is the maintained source for verifying photocopier's user-facing behavior. Read this index before driving anything, then use the matching feature file as the recipe.

## Baseline preconditions

- A fresh sandbox from `RUN="$("$V" up)"`, where `V` is this skill's `scripts/verify.sh`.
- `"$V" doctor "$RUN"` exits 0. If it reports that the checkout changed since `up`, run `down` and then `up` again.
- The fake OneDrive holds the seed data listed in `../SKILL.md` and nothing has been ingested yet.
- Never drive the real installation. Every command goes through `"$V" pc "$RUN" ...` or `"$V" launcher "$RUN" ...`.

## Driving conventions

- Start each recipe from a fresh sandbox unless its preconditions say otherwise. Recipes that build on another, such as flush after process, list the commands that get there.
- Commands are literal. `pc` adds `-c <sandbox config>` for you. Do not add your own.
- A phone upload is a file written under `RUN/sandbox/remote/<source path>/`. A file the user filed by hand is a file written under `RUN/sandbox/nas/`.
- `"$V" unmount` and `"$V" remount` take the NAS away and bring it back.

## Proof and skip reporting

- Run `"$V" snapshot "$RUN" <label>` before and after the action under test. Cite the snapshot paths and the transcript lines.
- CLI proof is the command, its combined output and its exit code, all in `transcript.log`.
- A state change needs a second, read-only view. A ledger row in the snapshot, a file on `nas/`, or `status` / `triage` output counts. The summary line of the command that made the change does not.
- Record the feature ID and the entry point with every claim.
- When a path cannot be reached in the sandbox, report the attempted command and the unmet precondition. The launcher's real-SMB flush branch is the usual case. Never report it verified through a different entry point.

## Feature entry contract

Each feature file opens with an H1 and one paragraph on the user-visible behavior, then exactly four H2s in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify.sh`, `Gotchas`.

## Features

- [Ingest](./ingest.md): fetch new uploads into the spool. Covers dry-run, junk skipping, idempotence, `--source`, and `--since`/`--until`.
- [Process](./process.md): date resolution, NAS layout and naming, and triage routing into the outbox.
- [Flush](./flush.md): guarded, verified delivery to the NAS, the unmounted-share refusal, and `status --verify`.
- [Triage and resolve](./triage-resolve.md): list the files waiting for a decision and close each one out.
- [Scheduled run](./scheduled-run.md): one launcher pass, its lock, the NAS-away path, and the summary email.
