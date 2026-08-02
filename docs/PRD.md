# PRD — photocopier

**Status:** Approved, in build
**Author:** Yossi Weihs
**Last updated:** 2026-08-01
**Related:** [Design](design.md) · [Decision log](decisions.md) · [PRFAQ](PRFAQ.md)

---

## 1. Problem

Two phones continuously upload photos and video to OneDrive. The permanent home for
that material is a 14 TB NAS organized as `photos/YYYY/MM/` going back to 2000, and
`video/YYYY/`. Nothing connects the two.

The gap is closed by hand, monthly: download from OneDrive, work out what's already been
filed, rename for the source phone, sort into year and month folders, copy to the NAS,
verify, delete the local copies.

**Current cost: approximately two hours per month — around 24 hours per year.**

Beyond the time, the manual process has three failure characteristics:

- **It is skipped.** A monthly chore with no deadline slips. Backlogs grow to hundreds of
  files, which makes the next session longer, which makes it more likely to be skipped.
- **It is error-prone at exactly the wrong moment.** The tedious part — deciding what's
  already been copied — is done by eye, near the end of a long session.
- **It stops entirely during travel**, which is when the most photos are taken.

## 2. User

One user, stated plainly: the author. Two phones, a NAS, a laptop, and a filing
convention maintained for twenty-six years.

This is not a market. It is a real problem with a real user who has a real 14 TB
library, and the design is better for being built for someone specific.

## 3. Goals

| # | Goal |
|---|---|
| G1 | Photos and video reach the NAS in the correct folder with no human involvement |
| G2 | Nothing is ever lost, overwritten, or silently misfiled |
| G3 | Ingest continues during weeks of travel, and drains when the NAS is reachable again |
| G4 | Ambiguous cases surface for review rather than being guessed at |
| G5 | The tool can move to the NAS later without a rewrite |

## 4. Non-goals

Equal weight to the goals. These are things it would be reasonable to expect, and this
tool deliberately does not do.

| # | Non-goal | Why |
|---|---|---|
| N1 | Photo management — albums, tagging, faces, search | The NAS library is browsed with other tools. This moves files. |
| N2 | Deduplicating the existing 26-year library | Separate problem, far riskier, no forcing function |
| N3 | Editing, transcoding, or format conversion | Originals are the archive. Lossy transformation is not this tool's business. |
| N4 | Modifying or deleting anything in OneDrive | Source is strictly read-only. The upstream copy is the safety net that makes aggressive local cleanup safe. |
| N5 | A general-purpose cloud sync tool | rclone exists. This is the restructuring layer on top of it. |
| N6 | A GUI | It runs unattended. The interface is a config file and a status command. |
| N7 | Multi-user, multi-tenant, or hosted anything | One household. |

## 5. Success metrics

Each is computable from the ledger. `photocopier status` reports them — the metrics are
instrumented, not aspirational.

| Metric | Baseline | Target |
|---|---|---|
| Manual minutes per month | ~120 | 0 |
| Eligible files delivered without intervention | n/a | ≥ 99% |
| Files requiring triage | n/a | < 1% |
| **Files lost, overwritten, or misfiled** | unknown | **0 — invariant** |
| Spool drain after travel | n/a | 1 flush run |
| Longest unattended streak | 0 days | tracked, growing |

The fourth is not a target, it is an invariant. It is enforced by the conflict policy
(D5), atomic writes, and the read-only source constraint (N4), and it is the metric that
would cause the tool to be turned off if violated once.

The last is the honest one: a daily job that quietly stops working is the realistic
failure mode for a tool like this. Tracking the streak makes silence visible.

## 6. Requirements discovery

Design started with questions rather than a proposal. Each answer changed the
architecture materially — recorded here because the changes are the point.

| Question | Answer | Effect on design |
|---|---|---|
| How are the phones' folders organized in OneDrive? | Shared into one account | Single OAuth path; sources modeled as a config-driven list rather than one tree |
| Where does this run? | Laptop now, NAS later | Destination abstracted behind an interface; see D8 |
| How should multiple phones merge? | Merged tree, source name in filename | Suffix-last naming, preserving chronological sort |
| What happens to the OneDrive copy after transfer? | Leave it; free the local copy | Confirmed API-based ingest over reading the sync folder — nothing hydrates locally, so there is nothing to evict |

A fifth requirement arrived unprompted and reshaped everything: **the NAS is unreachable
at usable speed during travel, sometimes for weeks.** That produced the spool
architecture (D2), which turned out to improve failure isolation and reviewability as
well.

Measurement preceded design. Before any architecture, the actual filesystem was
inspected: source volumes (~2 GB/month combined), free space (264 GB), existing folder
conventions, and the fact that OneDrive files on macOS are dataless placeholders whose
contents download implicitly on read. Two design decisions turned directly on those
numbers (D2, D3).

## 7. Requirements

**Functional**

- R1 — Enumerate configured OneDrive source folders and identify material not yet handled
- R2 — Respect a per-source cutoff date; earlier material is out of scope by policy
- R3 — Resolve a capture date per file: EXIF → filename pattern → source path → triage
- R4 — File photos to `photos/YYYY/MM/`, video to `video/YYYY/` (yearly, no month folders)
- R5 — Rename to `<original>_<source>.<ext>`, suffix last to preserve chronological sort
- R6 — Stage everything in a byte-exact replica of the destination layout before delivery
- R7 — Deliver only when the NAS is verifiably mounted; verify each file; then reclaim spool
- R8 — Route undated files and destination conflicts to triage with a recorded reason
- R9 — Report success metrics from the ledger via `status`

**Non-functional**

- R10 — Source is read-only. No writes, moves, or deletes in OneDrive, ever.
- R11 — Idempotent. Any stage re-runnable at any point with identical results.
- R12 — Crash-safe. Interruption at any point converges on re-run; no partial files at the destination.
- R13 — Spool capped at 20 GB. Exceeding it halts ingest with a warning; never deletes to make room.
- R14 — No machine-specific values in committed files. All paths from config or environment.
- R15 — Runs unattended under launchd, and later under cron on the NAS, from the same codebase.

## 8. Scope and phasing

Each phase is independently verifiable before the next exists.

| Phase | Deliverable | Done when |
|---|---|---|
| 0 | Repo, config loader, `doctor` | `doctor` reports a clean environment |
| 1 | Ledger + `ingest` | Both sources enumerate, cutoff respected, nothing downloaded twice |
| 2 | `process` | Outbox is a byte-exact replica of intended NAS layout |
| 3 | `flush` | Delivers to NAS; survives an unmounted share |
| 4 | Scheduling, `status`, backfill | Runs unattended for a week |
| 5 | NAS port | Same code, local destination, stage 3 deleted |

**Phase 3 is the credibility line.** Before it, this is a design. After it, files move.

## 9. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Unmounted share writes to laptop's boot disk | **High** — silent, looks like success | Require `ismount()` **and** a readable marker file; dedicated test |
| Daily job stops silently | High | Unattended-streak metric; notifications deferred (see below) |
| Spool fills the laptop during long travel | Medium | 20 GB cap, halt-and-warn, preflight free-space check |
| Date resolution misfiles at scale | Medium | mtime excluded (D4); triage over guessing; table-driven tests |
| OAuth token expiry breaks ingest | Medium | Surfaced by `doctor` and the streak metric |
| Spool corruption loses data | **Low** | OneDrive retains everything upstream — worst case is re-download, not loss |

**Known gap:** notifications are deferred. Until they exist, a stopped job is invisible
between manual `status` checks. This is an accepted, temporary risk and the first thing
to close after Phase 4.

## 10. Open questions

- Cutoff dates per source — parameter exists, values to be set before the first real run
- Whether to defer video ingest on metered connections (30% of bytes, ~110 MB each — a
  real cost on hotel wifi, marginal at current volumes)
- Notification channel, when it stops being deferred
