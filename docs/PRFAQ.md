# PRFAQ — photocopier

> *A note before we begin.* This document is a parody. It is written in the working-
> backwards format, with a straight face, about a program that copies roughly four
> hundred photos a month from one folder to another folder.
>
> The format is genuinely useful and I have written real ones. This is not that. The
> [PRD](PRD.md) next door is entirely serious, as is the [decision log](decisions.md).
> Every number quoted below is accurate, which is the only thing here that is.

---

## Press Release

### photocopier Now Generally Available, Ending Decades of Manual Photo Filing

*New service eliminates the undifferentiated heavy lifting of dragging files into folders, freeing customers to focus on their core competency of taking more photos of the same dog*

**A HOME OFFICE, SOMEWHERE ON THE EAST COAST — August 1, 2026** — Today marks the
general availability of photocopier, a fully managed solution that automatically
transfers photos and video from cloud storage to network-attached storage, correctly
filed by year and month, with zero customer involvement.

For decades, customers have faced an impossible choice: file their photos manually, or
have their photos not be filed. Research indicates that the affected population spends
approximately **two hours per month** on this task, or twenty-four hours per year — the
equivalent of an entire waking day spent dragging JPEGs. Customers told us they wanted
their photos on the NAS. They did not tell us this, because they are the author, but the
signal was clear.

photocopier removes this friction entirely. Built on a durable spool architecture,
the service ingests continuously from cloud storage, restructures files into the
customer's existing twenty-six-year folder taxonomy, and delivers to the NAS when it
becomes available — a critical innovation for customers experiencing the industry-wide
challenge of Being On Vacation.

"Before photocopier, I would think about copying my photos to the NAS, and then not do
it," said Yossi Weihs, a customer. "Now I don't think about it at all. It's the same
outcome from my perspective, but the photos are actually there."

"When we looked at this space, we saw a customer who was clearly underserved," said a
Senior Principal Group Product Leader II in the Household Storage Experiences
organization, who is also the customer, and also wrote this quote. "We started from the
customer and worked backwards. It turned out the customer was standing right behind us
the entire time, which shortened the discovery phase considerably."

photocopier is generally available today in all regions. The region is a house.

Getting started takes under ten minutes. Customers install rclone, edit one config file,
and never think about it again — the intended and only supported end state.

---

## Tenets

*(unless you know better ones)*

1. **The customer's photos are sacred. The customer's time is not, but we're optimizing it anyway.**
2. **A wrong answer delivered confidently is worse than no answer.** We would rather hand
   you eleven files to look at than silently file a photo into 1969.
3. **We are read-only upstream.** We will not touch your OneDrive. We will not "clean up"
   your OneDrive. We have thought about it. We will not.
4. **Boring is a feature.** The highest praise this tool can receive is that you forgot
   it exists.
5. **Two-way doors, wherever possible.** The spool can be deleted at any time with no
   consequence, because the cloud still has everything. This is the most relaxing
   sentence in the entire architecture.

---

## External FAQ

**What happens if my NAS is offline for a month?**
Nothing bad. Ingest keeps running and files accumulate in a local spool, which is capped
at 20 GB. At the measured rate of ~2 GB/month, that's roughly ten months of headroom.
When the NAS reappears, one flush run drains the backlog. This scenario is the reason the
spool exists — see [D2](decisions.md#d2--spool-architecture-over-direct-to-nas).

**What happens to the files in my OneDrive?**
Nothing. The source is strictly read-only. This is a hard constraint, not a default —
it's what makes deleting the local spool safe, since the cloud copy is always the fallback.

**What if a photo has no date?**
It goes to a triage folder for you to look at, along with a note explaining why. It is
never guessed at. The tool specifically refuses to use file modification time, because
that would produce a confident answer that is wrong — see
[D4](decisions.md#d4--file-mtime-is-excluded-from-date-resolution).

**Are videos handled differently?**
Yes. Videos go to `video/YYYY/` with yearly folders only; photos go to `photos/YYYY/MM/`.
This matches a filing convention that predates the tool by about two decades and was not
up for negotiation.

**I already copied some of these files by hand. Will it duplicate them?**
No. If the destination file is identical, it's recognized as already delivered and
dropped from the spool. If a file with the same name but *different* content is already
there, that's a genuine conflict and goes to triage rather than overwriting anything.

**How many customers does this have?**
One. The addressable market is a family. We are comfortable with our position in it.

---

## Internal FAQ

**Why not just `rclone sync` and go outside?**
Because the spool is emptied after delivery, so on the next run rclone sees an empty
destination and cheerfully re-downloads the entire library. Every day. This was caught in
design review rather than in production, which is the single best thing that happened
during this project. The fix — moving the "what's new" decision into a ledger that
survives the flush — is [D1](decisions.md#d1--rclone-is-a-transfer-engine-not-a-sync-engine).

**Why a spool at all? Isn't that just an extra hop?**
It is literally an extra hop, and every byte crosses the wire twice. At 2 GB/month over a
LAN, that costs seconds per month. We measured before optimizing, declined to build the
adaptive dual-path version, and saved ourselves a rarely-exercised code branch —
[D3](decisions.md#d3--no-adaptive-dual-path-transfer). We consider this our most
impactful decision, because it is the one where we did nothing.

**Why not run it on the NAS from day one, which would eliminate the travel problem
entirely rather than solving it?**
An excellent question, and the honest answer is iteration speed. It is also the correct
question, and it is documented as such in [D8](decisions.md#d8--laptop-hosted-now-with-the-spool-acknowledged-as-vestigial),
including the part where the spool becomes vestigial. We are not going to pretend we
didn't notice.

**What is the riskiest failure mode?**
An unmounted network share. On macOS, `/Volumes/media` still exists as an empty local
directory when the share is gone, so a naive delivery writes the entire backlog onto the
laptop's boot disk and reports complete success. This is guarded by requiring both a real
mount point and a readable marker file, and it has its own test with a deliberately
unsubtle name.

**How do we know it's working?**
`photocopier status` computes the PRD's success metrics from the ledger. Instrumenting
the metrics we wrote down was cheaper than explaining why we didn't.

**What are we deliberately not building?**
Albums, tagging, face recognition, transcoding, deduplication of the existing library, a
GUI, and a mobile app. See the non-goals table in the PRD, which is longer than the goals
table on purpose.

**Is there a north star metric?**
Yes: **zero files lost, overwritten, or misfiled.** It is not a target to move toward.
It is an invariant, and one violation turns the tool off.

**Why does this document exist?**
Because the format forces you to state the customer benefit before the mechanism, and
doing that honestly for a personal utility is a useful discipline — it is very easy to
build the interesting version of a tool instead of the useful one. It also forced the
non-goals to be written down, which is the section that has been most load-bearing.

Also it was fun. We are on Day 1 of copying photos to a NAS.
