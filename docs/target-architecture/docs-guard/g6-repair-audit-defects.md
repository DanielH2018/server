# G6 — Repair what the facts audit found

Status: IN PROGRESS. Step 1, the hook's tree rule, is done.

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
   ([dotfiles#1262](https://github.com/DanielH2018/dotfiles/issues/1262)). Done, with a
   different second rule: *Step 1 result* below. A server-repo path counts as present
   when `origin/master` tracks it, or when some checkout holds it and `origin/master`
   never tracked it. A test keeps the gitignored shape present.
2. **The memory entries**, edited in a session on daniel-box
   ([#8420](https://github.com/DanielH2018/server/issues/8420)). The same session replaces
   each `file:line` span with a symbol or a test node where the line number carried
   meaning, and adds `enforced_by: <test node>` to each entry the index marks `[ENFORCED]`.
   This step is operator-reserved: daniel-server holds a copy that the sync overwrites.
3. **The sync doc's check command**
   ([#8417](https://github.com/DanielH2018/server/issues/8417)).

## Step 1 result

[dotfiles#1278](https://github.com/DanielH2018/dotfiles/pull/1278) and
[dotfiles#1280](https://github.com/DanielH2018/dotfiles/pull/1280) landed the rule on
2026-11-14, and both hosts run it. Before, the hook reported no stale path in this repo's
store. It reports 16 memories on daniel-box and on daniel-server, and 13 of them cite a
path under `ansible/`, which the tree rename moved.

The rule that landed differs from the plan in three ways.

- **A never-tracked rule replaces the ignored rule.** The plan's two rules report a file
  on a branch that has not landed, which is the false alarm dotfiles#557 fixed. A path
  some checkout holds counts as present only when `origin/master` never tracked it. One
  walk of the default branch's history lists every path it ever dropped, in about half a
  second on 7,577 commits.
- **The ignored rule is gone.** dotfiles#1278 kept it, and daniel-box then reported 15
  memories where daniel-server reported 16. The primary checkout on daniel-box holds
  8,519 leftover files under the deleted `ansible/` tree, mostly `__pycache__`, and this
  repo's `.gitignore` ignores every root-level entry it does not name. So three deleted
  directories read as ignored and present. A gitignored directory that no commit ever
  held, such as `docs/superpowers/`, stays present under the never-tracked rule.
- **The top-directory filter reads history.** The hook skips a token whose first segment
  is no top-level directory of the repo. It judged that against the session's checkout,
  so a memory citing a path under `ansible/` was reported only from a checkout that
  still held the directory.

A check's file is read from the default branch as well. A test the default branch
renamed is missing even while a worktree that lags still defines it.

Two costs: an ignored file that `origin/master` once tracked reads as gone, and a symbol
added to a tracked file on an unlanded branch is missing until it lands.

## Verification

Start a session on daniel-box after step 1 and read the hook's line. Run the hook's test
for the gitignored shape.

## Complexity delta

Flat. One class in one hook replaces the checkout list that two of its functions took,
and no module is added.
