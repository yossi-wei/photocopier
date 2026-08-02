# Decision log

Architecture decisions, with the evidence behind them. Includes the ones that were
reversed — those are the useful ones.

Format: context, options, decision, evidence, consequence.

---

## D1 — rclone is a transfer engine, not a sync engine

**Context.** rclone was the obvious tool for moving files out of OneDrive. The natural
usage is `rclone copy source: dest/`, letting rclone decide what's new by comparing the
two sides.

**Problem found in review.** The spool is flushed to the NAS and emptied after each
successful delivery. rclone determines what to transfer by comparing source against
destination — so on the next run the destination is empty, and rclone re-downloads
everything from OneDrive. Every day. Forever.

**Options.**
1. `--max-age 3d` — only fetch recently-modified files.
2. Never flush the spool — keep a permanent local mirror.
3. Move the "what's new" decision out of rclone into a ledger that survives the flush.

**Decision.** Option 3. `rclone lsjson` enumerates, the ledger diffs, and
`rclone copy --files-from-raw` transfers an explicit list.

**Why not 1.** `--max-age` fails silently. Miss a few days — laptop off, travel, a crash —
and files older than the window are skipped permanently with no error and no signal. A
backup tool whose failure mode is invisible data loss is not a backup tool.

**Why not 2.** Defeats the purpose. The spool exists to be temporary; a permanent mirror
means the laptop needs as much storage as the NAS.

**Consequence.** The ledger becomes the system of record and the single most important
component. Its dedicated regression test (`test_flush_then_ingest_does_not_redownload`)
guards the exact failure above.

---

## D2 — Spool architecture over direct-to-NAS

**Context.** First design streamed from OneDrive straight to the NAS over SMB.

**Problem.** During travel — weeks at a time — the NAS is unreachable at any useful
speed. VPN is not viable for video. Direct-to-NAS means ingest simply stops, and the
backlog is only bounded by how long the trip is.

**Decision.** Decouple acquisition from delivery with a durable local spool.
Ingest runs daily regardless of NAS reachability; delivery runs when the NAS is actually
there.

**Evidence.** Measured ~2 GB/month across both sources, against 264 GB free on the
laptop. A six-week trip spools 3–6 GB. The spool is effectively free.

**Consequence.** Three benefits beyond the travel case: failure isolation (NAS down no
longer fails ingest), a review buffer (the outbox is a byte-exact replica of what will
land, inspectable before it lands), and cheap iteration (restructuring logic can be
re-run without re-downloading).

**Cost.** Every byte crosses the wire twice. See D3.

---

## D3 — No adaptive dual-path transfer

**Context.** The spool means bytes go OneDrive → laptop → NAS, where direct would be
OneDrive → NAS. At home, on a LAN, the second hop is pure waste. The tempting
optimization: stream directly when the NAS is reachable, spool only when it isn't.

**Decision.** Don't build it. One code path, always spool.

**Evidence.** ~2 GB/month over a LAN. The "waste" is measured in seconds per month. The
optimization would double the code paths, double the integration test matrix, and create
a rarely-exercised branch — which is where bugs live.

**Consequence.** This decision was made by measuring before building rather than after.
The measurement took four minutes.

---

## D4 — File mtime is excluded from date resolution

**Context.** Files must be filed into `photos/YYYY/MM/` or `video/YYYY/`, so every file
needs a date. Modification time is the cheapest source and is always present.

**Decision.** Never use mtime. The chain is EXIF `DateTimeOriginal` → filename pattern
→ source path `YYYY/MM/` → triage.

**Why.** Both OneDrive and rclone rewrite mtime during transfer. It reflects when the
file moved, not when the photo was taken. Including it would guarantee a date for every
file — and that is exactly the problem: a **confidently wrong date is worse than no
date**, because a wrong date files silently into the wrong month, while no date routes
to triage where a human looks at it.

**Evidence.** The existing library contains `Pictures/Camera Roll/1969/` — files whose
timestamps were zeroed at some point. Under an mtime-inclusive design they would be
filed with total confidence into whichever month they were last touched.

**Consequence.** A small percentage of files land in triage instead of being silently
misfiled. This is the intended trade.

---

## D5 — Destination conflicts go to triage, never `--ignore-existing`

**Context.** The NAS library is already populated by years of manual filing. Delivery
merges into a tree that already has content, so target paths sometimes exist.

**Three cases.**
- Target absent → deliver.
- Target present, identical (size + hash) → already delivered. Mark delivered, drop from
  spool. Not an error.
- Target present, **different content** → triage, reason `dest_conflict`.

**Decision.** Handle all three explicitly.

**Why not `--ignore-existing`.** It collapses cases two and three into "skip." Case two
is routine; case three means two different photos are claiming the same filename, which
is a real problem that needs a human. The one-flag solution silently discards exactly
the case that matters.

**Note.** Phone-generated variants like `PXL_20260601_223332404~2.jpg` are legitimately
distinct edits and must survive as separate files — they are not duplicates.

---

## D6 — Item identity is (source, path, hash)

**Context.** The ledger needs a unique key per item.

**Options.** `(source, path)` — natural, matches how humans think about files. Or
`(source, path, hash)`.

**Decision.** Include the hash.

**Why.** Under `(source, path)`, a file re-uploaded at the same path with different
content is an *update* to an existing row — and if that row is already marked delivered,
the new content never ships. Including the hash makes it a genuinely new item with its
own delivery, and any resulting filename collision routes through D5 to triage rather
than overwriting a delivered file.

**Consequence.** Slightly more rows. No silent overwrites.

---

## D7 — Spool lives in an XDG-style path, never a temp directory

**Context.** The spool needs a home. The instinct is a well-known temp area.

**Decision.** `$PHOTOCOPIER_SPOOL` → config → `~/.local/share/photocopier/spool`.
Identical on macOS and Linux.

**Why not temp.** macOS purges `/tmp` entries after roughly three days of inactivity and
clears `$TMPDIR` on boot and under disk pressure. The spool's entire purpose is surviving
weeks of travel. A temp directory would delete the backlog mid-trip — the one moment the
design exists to handle.

**Why not a system path** (`/opt`, `/usr/local/var`). Requires sudo, which is friction
for anyone cloning the repo, and buys nothing.

**Consequence.** No privileged install, no hardcoded paths, and the eventual move to the
NAS doesn't change the path logic. Two guards come with it: refuse to run if the spool
resolves inside a sync root (OneDrive/iCloud/Dropbox — otherwise the tool feeds its own
input), and exclude it from Time Machine at setup.

---

## D8 — Laptop-hosted now, with the spool acknowledged as vestigial

**Context.** The spool exists because the laptop is the ingest point and can't always
reach the NAS.

**The observation.** Phones upload to OneDrive from anywhere. The NAS sits at home with
internet. If ingest ran **on the NAS**, it would pull from OneDrive continuously whether
its owner is home or abroad. There would be no travel problem to solve — no spool, no
flush, no laptop in the path.

**Decision.** Build laptop-hosted anyway, for now.

**Why.** Iteration speed. The laptop has the toolchain, the debugger, and a human sitting
in front of it. TrueNAS container setup is friction during the phase where the date-
resolution rules are still being argued with.

**Consequence, stated honestly.** The spool is the laptop-hosted form of this pipeline,
not a permanent architectural fixture. When ingest moves to the NAS, stages 1 and 2 port
unchanged and stage 3 is deleted. That is a planned deletion, not a regression.

**Why this is in the log.** The most useful thing found during design was that a
requirement might not need to exist. It's worth recording that the answer to "how do we
handle the NAS being unreachable" was very nearly "stop putting the laptop in the middle."
