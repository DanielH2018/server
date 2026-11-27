# Worktree storage: what a tree costs, and what would make it cheaper

The operator asked on 2026-11-27 whether worktrees could be cheaper, for example by giving only
edited files their own copy. This page holds the measurements from that day, the options
compared against them, and the recommendation.

**Recommendation: change no filesystem, mount or host package.** A worktree already shares its
object store with the primary clone, and uv already hardlinks each `.venv` into the uv cache.
A fresh tree holds about 67 MB of its own bytes and is usable in under a second. The largest
avoidable cost found was outside the trees: superseded tool versions in `.git/alert-tools/`,
364 MB on each host. `scripts/validate/alert_tools.sh` removes them past a 14-day grace.

## Measurements (2026-11-27)

Both hosts keep `/`, `/home` and `/var/lib` on one ext4 logical volume
(`/dev/mapper/ubuntu--vg-ubuntu--lv`). ext4 has no reflinks. `/tmp` is a tmpfs on both.

| | daniel-box | daniel-server |
|---|---|---|
| Disk size, free | 914 GB, 422 GB | 455 GB, 170 GB |
| Trees under `~/server/.claude/worktrees` | 37 | 18 |
| Their size with venv hardlinks counted once | 3.9 GB | 3.2 GB |
| Trees in the agent user's clone (`/var/lib/claude/server`) | 12, 1.7 GB | 0 |
| uv cache (`~/.cache/uv`) | 964 MB, plus 2.0 GB for the agent user | 2.8 GB |
| `.git/alert-tools/` | 1.2 GB, 364 MB of it superseded | 1.2 GB, 364 MB of it superseded |
| `git worktree add` of master | 0.32 s | 0.34 s |
| `uv sync --frozen` in that tree | 0.25 s | 0.17 s |

**One tree, split by what holds the bytes:**

| Part | Size | Shared? |
|---|---|---|
| Git objects | 0 | The tree's `.git` is a file pointing at the primary's object store, about 150 MB of packs. |
| Checked-out files | 67 MB on disk | No. 4,201 tracked files hold 31 MB of content; the rest is per-file block overhead. |
| `.venv` | 292 MB apparent, 1.9 MiB unique when fresh | 95% of its files are hardlinks into the uv cache. A tree that has run the suite reaches about 13 MB unique. |
| Repo `__pycache__` | 0 when fresh, about 34 MB once the suite has run | No. Across daniel-box's trees it totals 359 MB. |
| `.hypothesis`, `.ruff_cache` | about 3 MB | No. |

The worktree pruner bounds the count. No tree on daniel-box was older than three days.

### What a landing's fresh checkout costs

`land.sh` makes each fresh checkout with `git worktree add` under
`~/.cache/homelab/land-fresh/` (`scripts/deploy_tools/land_lib/fresh.py:checkouts_root`). The
landing units set `PYTHONDONTWRITEBYTECODE=1`, so the checkout gathers no bytecode. One landing
costs the same as a fresh tree: about 67 MB on disk and 1.9 MiB of venv, for the length of the
landing.

The 353 MB figure in [#10473](https://github.com/DanielH2018/server/issues/10473) describes the
checkout under `/tmp`. There, uv could not hardlink from the on-disk cache into the tmpfs, so it
copied every package and printed `warning: Failed to hardlink files; falling back to full copy`.
168 of the 987 landing logs in `/tmp/homelab-landings-ubuntu` carry that warning, all from
`/tmp` checkouts. The landing log written after PR 10477 moved the checkouts to disk prints
`Installed 66 packages in 268ms` with no warning. The journals of the landing units on both
hosts hold no warning for the two days before the measurement. The agent user's per-user landing
logs were not readable from the `ubuntu` account. Even so, no landing's on-disk checkout can sit
on a different filesystem from its uv cache, because both hosts keep every home and cache on the one volume.

## Options compared

| Option | Saves | Breaks or costs | Verdict |
|---|---|---|---|
| Copy-on-write through reflinks (XFS `reflink=1` or btrfs) | At most the 67 MB checkout per tree, about 2.5 GB on daniel-box, under 1% of free space. `git worktree add` writes files from objects and makes no reflinks, so a tree would have to be `cp --reflink` of a template tree instead. | A new logical volume or a reformat on both hosts. The uv cache must move onto the same filesystem, or uv loses its hardlinks, which is the #10473 failure again. The pruner, `land_lib/fresh.py` and the fan-out's tree creation all need the template-copy path. | No. The saving is small against 422 GB and 170 GB free, and creation already takes 0.3 s. |
| Sparse checkout, or `git worktree add --no-checkout` plus a partial checkout | Under half of the 31 MB of tracked content, since most sessions touch `scripts/`, `tests/`, `k8s/` and `docs/`. | The census tests, `repo_guards.py`, the prek hooks and the renderer read the whole tree. A sparse tree passes a check that a full tree fails. Fan-out batches would need each batch's path set before the tree exists. | No. |
| One uv cache shared by `ubuntu` and the `claude` agent user | About 1 GB on daniel-box, where the two caches overlap. | The cache would need group write across the two user IDs, which undoes part of the agent user's separation (`docs/claude-agent-user.md`). uv's cache lock assumes one owner. | No. |
| `uv cache prune` on a timer | Unreferenced wheels and builds. daniel-server's cache is 2.8 GB against daniel-box's 964 MB. | A host package change: a timer per host and user. A prune during a sync is safe, because uv locks its cache. | Worth asking the operator. It is the one host-package change these numbers support. |
| A shared read-only `.venv` | 2 to 13 MB per tree. | The venv's editable install points at one checkout's `scripts/`, so every tree would import another tree's code. A branch that changes `uv.lock` could not have its own dependencies. | No. |
| overlayfs over a master tree | Most of the 67 MB checkout. | A mount per tree needs root or a user-namespace FUSE mount. The agent user has no sudo. A lower layer cannot change under a live mount, so master moving forces a remount. The pruner must unmount before it removes anything. | No. |
| `PYTHONPYCACHEPREFIX` or `PYTHONDONTWRITEBYTECODE` for sessions | Up to 34 MB per tree. | A prefix keys bytecode by source path, so trees still share nothing and the bytes only move. No bytecode makes every test run recompile. | No. |
| Prune superseded `.git/alert-tools/` versions | 364 MB per host, plus about 100 MB per future Prometheus or Loki bump. | Nothing in a tree. The grace keeps a version that a tree from before the bump still pins. | **Built.** |

### The alert-tools prune

`alert_tools.sh` caches each tool binary under `.git/alert-tools/<tool>-<version>/`, in the git
common dir, so every worktree of a clone shares one copy. Before this change nothing removed a
version once its image pin moved on. On 2026-11-27 each host held three Alertmanager versions,
two of Loki, four of pint and two of Prometheus, beside the five pinned ones.

On each run, once the pinned version's `.provisioned` stamp is older than 14 days, the script
removes that tool's version directories whose stamps are older than the pin's. The rules that
shape it:

- **A version newer than the pin stays.** A Renovate branch provisions the next version before
  master pins it, and master's runs must not delete it.
- **The grace counts from the successor's stamp.** The stamp records when the newer version was
  first provisioned on this host, which is no later than when master started to pin it. A tree on
  a base from before the bump keeps its version for 14 days, which outlasts the pruner's
  few-day age-out.
- **The prune takes the cache lock without waiting.** A run that finds a provision in progress
  skips the prune and runs its tool.

The first run after the grace removes the 364 MB on each host. CI restores the cache from its
own key and is unaffected.

## Not worktree storage, but found on the way

- [#10462](https://github.com/DanielH2018/server/issues/10462) remains the RAM problem on
  daniel-box: Claude scratchpads and pytest trees on the `/tmp` tmpfs. The worktrees themselves
  hold no RAM.
- Automatic `git gc` runs: the packs are from the last three days, and no `rr-cache` entry
  is past the 60-day limit of `git rerere`.
