# G6 — Repair what the facts audit found

Status: PLANNED.

Depends on: none.

Unblocks: [G8](g8-memory-resolver-and-replica.md), which builds on the tree rule and the
`enforced_by:` keys this slice adds.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves.

## Goal and exit criterion

The defects the audit of 2026-11-13 found are fixed, and the memory hook asks the right
tree whether a cited path exists.

Exit: a session that starts in this repo on daniel-box reads a hook line that lists every
memory citing a path `origin/master` no longer tracks, and no path under a gitignored
directory.

## Baseline at `4d85f67c5`

- `memory-upkeep.py` counts a repo path as present when any checkout or worktree holds it.
  The repo had 140 worktrees. The hook reported 0 of the 10 cited paths that were gone
  from `origin/master`, and reported all 10 when run with no sibling checkouts.
- The rule exists for a reason. On 2026-08-29 the hook flagged paths under the gitignored
  `docs/superpowers/`, which the primary checkout holds and no branch tracks.
- Three memory entries are out of date, and the index marks one retired check as enforced.
- The check command in `docs/claude-memory-sync.md` compares the agent user's store, which
  the sync does not read while `claude_code_user` is the operator.
- The store's bodies hold 11 `file:line` spans, which rot with every edit above the line.

## Steps, as PRs

1. **The hook's tree rule**, in the dotfiles repo
   ([dotfiles#1262](https://github.com/DanielH2018/dotfiles/issues/1262)). A server-repo
   path counts as present when `origin/master` tracks it, or when the primary checkout
   holds it and git reports it ignored. A test keeps the gitignored shape present.
2. **The memory entries**, edited in a session on daniel-box
   ([#8420](https://github.com/DanielH2018/server/issues/8420)). The same session replaces
   each `file:line` span with a symbol or a test node where the line number carried
   meaning, and adds `enforced_by: <test node>` to each entry the index marks `[ENFORCED]`.
   This step is operator-reserved: daniel-server holds a copy that the sync overwrites.
3. **The sync doc's check command**
   ([#8417](https://github.com/DanielH2018/server/issues/8417)).

## Verification

Start a session on daniel-box after step 1 and read the hook's line. Run the hook's test
for the gitignored shape.

## Complexity delta

Flat. One rule in one hook narrows, and no module is added.
