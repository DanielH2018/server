# G8 — Memory: one resolver, a checked index, a read-only replica

Status: PLANNED.

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
  the index. `memory_survey.py --fix-index` repairs it, and the `memory-consolidation`
  skill or the operator runs that.
- **The archive table is generated.** `memory_survey.py` writes `archive/README.md` from
  the `promoted_to:` keys.
- **The replica is read-only.** The sync unit removes write permission from its target
  after each run and restores it before the next. Every writer fails there, a Bash command
  included. On a store it cannot write, the hook prints one line: this store is a replica
  of daniel-box, how many findings the source has, and where to record a memory. It lists
  no rows, because no session on that host can fix one.
- **The store path** comes from the hook's payload, so the agent user's store resolves.

```diff
 ~/.claude/hooks/                                       (dotfiles)
+  memory_resolver.py   one resolver: body paths, index links, promoted_to, enforced_by
   memory-upkeep.py
-    its own resolver
+    calls memory_resolver; index check: missing, gone, duplicate, marker mismatch
+    on an unwritable store: one line with the count and the route, no rows
 ~/.claude/skills/memory-consolidation/memory_survey.py
-    its own resolver
+    calls memory_resolver; --fix-index; writes archive/README.md from promoted_to
 memory/archive/README.md
-  hand-kept "now lives in" table
+  generated from promoted_to
 hosts/components/claude-code/templates/claude-memory-sync.service.j2
+  the target is writable only while the rsync runs
```

## Steps, as PRs

1. The shared resolver and the index check, in the dotfiles repo.
2. The `promoted_to:` keys on archived files, the generated archive table and
   `--fix-index`, in the dotfiles repo and the memory store on daniel-box.
3. The read-only target, in this repo's `claude-memory-sync` unit, with its
   autonomous-role contract and `docs/claude-memory-sync.md` updated in the same PR.

## Verification

Start a session on daniel-box and read the index findings. On daniel-server, try to write
a memory and read the failure and the session-start line.

## Risks and what is not verified

- Whether the harness tolerates a memory directory it cannot write, without failing the
  session. If it does not, step 3 falls back to a hook that denies the write.
- A session on daniel-server cannot save a memory. ADR-0031 defers an outbox until a count
  of refused writes justifies it.

## Complexity delta

Adds one module and two frontmatter keys. Removes one resolver, the hand-kept archive
table, and the silent loss of replica writes.
