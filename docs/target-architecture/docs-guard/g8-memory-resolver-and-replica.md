# G8 — Memory: one resolver, a checked index, a read-only replica

Status: DONE. The three steps landed on 2026-11-14 as
[dotfiles#1299](https://github.com/DanielH2018/dotfiles/pull/1299),
[dotfiles#1310](https://github.com/DanielH2018/dotfiles/pull/1310),
[dotfiles#1311](https://github.com/DanielH2018/dotfiles/pull/1311) and
[#8687](https://github.com/DanielH2018/server/pull/8687). The store on daniel-box carries
the owner keys.

Depends on: [G6](g6-repair-audit-defects.md) for the tree rule and the `enforced_by:`
keys.

Unblocks: none.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves.

## Goal and exit criterion

[ADR-0019](../../adr/0019-fact-support-stops-at-the-repo-store.md) rules that a memory
gets no status line and no re-verification, and that memory staleness stays with
`memory-upkeep.py`. That ruling stands. This slice completes the hook's existence check
and makes the documented rule for a replica store hold. Nothing in it reads this repo's
facts library, so no hook crosses repositories.

Exit: a session on daniel-box reads the index findings at session start, and a write to
the memory store on daniel-server fails at once, with the route in the session-start line.

## Baseline at `4d85f67c5`

- `memory-upkeep.py` and `memory_survey.py` resolve the same citations with separate code.
  On the same store the survey reported 5 dangling checks and the hook reported 0.
- Nothing checks the 23 repo links in `MEMORY.md`, or `archive/`. 16 of the 52 targets in
  the hand-kept "now lives in" table of `archive/README.md` were gone.
- The index's `[ENFORCED]` markers are hand-kept. One marked a check its own file's body
  called retired.
- `docs/claude-memory-sync.md` says sessions on daniel-server read the store and do not
  write it. Nothing enforces that. The sync deletes a memory written there at its next
  run, without a message.

## Design

- **One resolver.** `memory-upkeep.py` and `memory_survey.py` share one module, under
  G6's tree rule. It keeps the hook's extraction, which also finds a path inside a
  backticked command.
- **It reads three more places.** The repo links in `MEMORY.md`, after stripping the
  checkout prefix. Each archived file's `promoted_to: <doc>#<heading>` key, whose anchor
  must exist and must not sit under `docs/archive/`. The `enforced_by:` key, whose test
  node must exist.
- **The index is checked, not generated.** `MEMORY.md` stays hand-written, so nothing
  fights the harness prompt that appends pointer lines. The session-start check reports a
  live file with no index line, a line whose file is gone, a duplicate line, and an
  `[ENFORCED]` marker that disagrees with the file's `enforced_by:` key. No hook writes
  the index, and no tool repairs it: *Step 2 result* says why `--fix-index` is not built.
- **The archive table is generated.** `memory_survey.py --write-archive-table` writes the
  owner table in `archive/README.md` from the `promoted_to:` and `enforced_by:` keys,
  between two marker lines. The prose around the markers stays hand-written.
- **The replica is read-only.** The sync's rsync strips every write bit from what it
  leaves on the target (`--chmod=a-w`). Every writer fails there, a Bash command
  included. On a store it cannot write, the hook prints one line: this store is a replica
  of daniel-box, how many findings the source has, and where to record a memory. It lists
  no rows, because no session on that host can fix one.
- **The store path** comes from the hook's payload, so the agent user's store resolves.

```diff
 ~/.claude/hooks/                                       (dotfiles)
+  lib/memory_resolver.py   one resolver: body paths, index links, promoted_to, enforced_by
   memory-upkeep.py
-    its own resolver
+    calls memory_resolver; index check: missing, gone, duplicate, marker mismatch
+    on an unwritable store: one line with the count and the route, no rows
 ~/.claude/skills/memory-consolidation/memory_survey.py
-    its own resolver
+    calls memory_resolver; --write-archive-table writes the owner table from the keys
 memory/archive/README.md
-  hand-kept "now lives in" tables
+  one table generated from promoted_to and enforced_by
 hosts/components/claude-code/templates/claude-memory-sync.service.j2
+  rsync --chmod=a-w: the target holds no write bit between runs
```

## Steps, as PRs

1. The shared resolver and the index check, in the dotfiles repo. Done.
2. The `promoted_to:` keys on archived files and the generated archive table, in the
   dotfiles repo and the memory store on daniel-box. Done.
3. The read-only target, in this repo's `claude-memory-sync` unit, with its
   autonomous-role contract and `docs/claude-memory-sync.md` updated in the same PR.
   Done, with the hook's one-line message for a store it cannot write, in the dotfiles
   repo.

## Step 1 result

[dotfiles#1299](https://github.com/DanielH2018/dotfiles/pull/1299) landed
`hooks/lib/memory_resolver.py` on 2026-11-14, and the hook and the survey both import it.
On daniel-box and daniel-server the hook prints the same 17 lines, and the survey lists
the same 7 dangling citations.

- **The survey's answer depended on its checkout.** On daniel-server the primary checkout
  was two days behind `origin/master`. The survey listed 1 dangling `[ENFORCED]` citation
  where 7 are gone, and reported `k8s/roles/authelia/CLAUDE.md`, which the default branch
  tracks, as a dead index link. Both callers judge a path by the default branch.
- **A marked entry with no `enforced_by:` key is not a finding.** On 2026-11-14 no memory
  carries the key, so the strict rule would print a row per marked entry. The hook's
  second section still resolves such an entry's check from its prose. [G6](g6-repair-audit-defects.md) step 2 adds the keys, and the rule tightens after
  that.
- **A repo doc linked from several index lines is not a duplicate.** One doc owns the
  claims of several retired memories, a line each. The first cut flagged it and the live
  store produced 2 findings. Only a memory file linked twice is listed.
- **The live store has 0 index findings.**

Deferred to step 2: the `promoted_to:` key, and `archive/`, which the index check does
not read.

## Step 2 result

[dotfiles#1310](https://github.com/DanielH2018/dotfiles/pull/1310) added
`memory_resolver.archived_owners` and the survey's archive section.

- **The hand-kept tables were worse than the baseline says.** A dry run on a copy of the
  store, against `origin/master`, read 78 rows. 47 name an owner that resolves, some
  after the tree rename. 31 do not: most name a subject retired with its code, such as
  the Uptime Kuma role, and a few name a page that moved to `docs/archive/`.
- **The heading is optional.** `promoted_to: <path>` resolves when the path is present.
  With `#<heading>` the file must also have an ATX heading of that text. Most rows of the
  hand tables name no heading, so a required anchor would have left them without a key.
- **An owner outside the repo starts with `~/`.** Seven rows point at the user-level
  `CLAUDE.md` or a skill, which the default branch cannot judge.
- **The table sits between two marker lines.** The README is 409 lines, most of them
  prose on why each batch was retired. Generating the whole file would delete that.
- **The session-start hook does not print archive findings.** An archived memory reaches
  no session, so a stale owner misleads nobody until someone opens the archive, and the
  survey runs then.
- **`--fix-index` is not built.** The live index has 0 findings for it to repair. An
  index line holds several links, so removing a dead one rewrites a sentence, which a
  mechanical edit gets wrong.

The operator approved the store edit on 2026-11-14, and it ran on daniel-box the same
day. A dry run on a copy of that store gave the result the live run then gave, file for
file: 47 archived memories carry a key, 44 `promoted_to:` and 3 `enforced_by:`, and
`archive/README.md` holds the generated table of 47 rows. The 31 rows whose owner does
not resolve stay in the hand-kept tables for a person to judge. Afterwards the survey read
`archive: 47/142 archived memories name an owner`, with no owner gone and the table
current. A copy of `archive/` from before the edit is beside the store, as
`memory-archive-before-2026-11-14.tgz`.

## Step 3 result

The unit passes `--chmod=a-w` to rsync. The plan had the unit remove write permission
after each run and restore it before the next, over ssh. rsync does both itself: running
as the files' owner, it adds the owner's write bit back to a directory only while it
changes that directory. Measured with rsync 3.2.7, the version on both hosts: a run that
changed one file, deleted one and added two left no write bit anywhere, a run with
nothing to copy printed nothing, and a target left writable with a stray file was emptied
of the file and locked by the next run.

The way out is `chmod -R u+w` on the store, which `docs/claude-memory-sync.md` records.
With the sync turned off the store stays read-only until someone runs it.

[#8687](https://github.com/DanielH2018/server/pull/8687) landed the unit on 2026-11-14,
and [dotfiles#1311](https://github.com/DanielH2018/dotfiles/pull/1311) landed the hook's
line. The first run after daniel-box applied its package printed one permission line per
file, 194 in all. On daniel-server the store then read `dr-xr-x---`, `touch` in it
failed with `Permission denied`, and the hook printed one line: the store is read-only,
where a lesson goes, and that 2 upkeep checks report findings on the owning host.

## Verification

Start a session on daniel-box and read the index findings. On daniel-server, try to write
a memory and read the failure and the session-start line.

## Risks and what is not verified

- The harness tolerates a memory directory it cannot write. Measured on 2026-11-14 with
  Claude Code 2.1.312, on a scratch project whose store had every write bit removed: a
  headless session started, read the index, and a Write to the store failed at once with
  `EACCES: permission denied`. An interactive session is not measured. Step 3 needs no
  hook that denies the write.
- A session on daniel-server cannot save a memory. ADR-0031 defers an outbox until a count
  of refused writes justifies it.

## Complexity delta

Adds one module and two frontmatter keys. Removes one resolver, the hand-kept archive
table, and the silent loss of replica writes.
