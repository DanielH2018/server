---
name: land-after-merge
description: Merge a PR and follow it through to a verified deploy with `land.sh`. Use when a PR is ready to merge, has just merged, or when you need the exact merge → CI-wait → tick → deploy → verify sequence. Covers what `land.sh` does, its VERDICT line, and why hand-polling CI and hand-merging are wrong here.
allowed-tools: Bash, Read, Grep, Glob
---

The procedure `CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify* points at. That section
owns the two things that must stay resident — the standing directive that a merge is followed
through without asking, and the *When to wait* list. This skill owns the mechanics.

## The procedure

Record the pre-merge master SHA, arm the merge, then hand everything else to ONE backgrounded
command:

```bash
git rev-parse origin/master          # keep this; land.sh needs it for the fallback
gh pr merge --squash --auto          # merges when the PR's checks are green
./scripts/deploy_tools/land.sh --pr <n> --since <pre-merge-sha> --await-merge
```

`--await-merge` polls the PR's state every 30s until it is merged and only then starts the
landing. It reads the state, never the checks, so it is not the hand-polling this skill
forbids. Without it the session has to notice the merge itself and start `land.sh` by hand,
which every landing on 2026-09-01 did with a hand-written `until MERGED` loop. A PR still
open after 45 minutes exits 75: it is not being merged, and the reason is on the PR.

Run `land.sh` with `run_in_background` and let the session be re-invoked when it exits. It
waits for master CI on the merge commit, ticks, deploys what the tick deferred, and prints a
`VERDICT:` line — `settled`, `unhealthy`, `deploy-failed`, `nothing-to-deploy`, `blocked`,
`needs-manual-apply` or `deferred`.

Every run also writes one logfmt line to syslog on exit (`logger -t landing-annotation`):
the PR, the merge SHA, the verdict, and seconds spent in each phase — `wait_merge`,
`wait_ci`, `tick`, `deploy`, `total`. Promtail ships it to Loki and the **Landings** Grafana
board (Infrastructure folder) plots it, so "sessions wait too long" is answered by the
phase medians there rather than by memory. CLI: `uv run python
scripts/diagnostics/probe.py loki-query '{job="syslog"} |= "event=landing" | logfmt'`.

`deferred` (exit 75) means the tick applies this PR itself — a setup role `initial_setup.yml`
includes, or the deploy plane — and has not crossed origin yet, almost always because a newer
merge's CI is still running. The next tick does it; nothing is wrong with the PR. `land.sh`
reads that from the deployer's own `behind_since` and `hold_sha` markers, since a PR with no
service tag leaves no other evidence of being applied. A held `hold_sha` is `deploy-failed`.

`needs-manual-apply` means the PR reaches something neither a deploy tag nor the tick covers,
and the line names the command that does apply it. Three things are in that position. A
**setup role `initial_setup.yml` does not include** (`k3s` is in `k3s-bringup.yml`, `common` in
no playbook) or a bring-up playbook, because the tick applies every other setup role itself
and `deploy.yml` is a `containers_list` loop. A **shared k8s role** —
`manifests`, `volume-claim`, `rollout-drain`, `volume-snapshot`, `volume-revert`,
`image-builder`, `longhorn-api`, `cronjob-gate` — has no `containers_list` entry at all, so
`--tags manifests` matches nothing and only a full `ansible/deploy.yml` applies it. A **rotated
secret** is the third and has no path to match at all: a secret's value lives in no role's
template, so `ansible/vars/secrets.yml` derives zero tags however many roles consume it. Run
`uv run python scripts/secrets_mgmt/secret_rotation.py consumers <secret>` for who holds a stale
copy and the repair command per plane. The other services in the same PR still deploy normally;
the verdict is about the half that did not.

**Do not hand-poll CI and do not hand-merge.** `await_ci.py` reads the same check-runs
endpoint the deployer reads, so its verdict and the tick's agree by construction. Hand-polling
cost 835 polls across 213 wait episodes before it existed.

It also owns the rule that used to live in `CLAUDE.md`: `cancelled`, `stale` and
`skipped_by_concurrency` mean *no verdict for this SHA*, never *this SHA is bad* —
`_CI_NO_VERDICT_CONCLUSIONS` in `deploy_logic.py` is the list, and a commit whose merge was
immediately followed by another reads `cancelled` permanently. `await_ci.py` follows the tip in
that case, but only once your commit is an ancestor of it. If you ever check by hand, check
that way.

`land.sh` runs from the primary checkout wherever you invoke it, because `deploy.sh` renders
from its working directory and a worktree is behind master after a squash merge.

It scopes the deploy to the PR's own file list rather than a SHA range, so another session's
merged work is not swept in. `gh` paginates that list at 100 files, so it falls back to
`--changed <since>` when the count disagrees — which is the only reason `--since` is needed.
Pass `--tags` to override the scope entirely.

**Verify the change, not just the workload.** The `VERDICT:` line gates the rollout and the
180s restart window. It cannot see whether *your change* took effect: an Authelia 302 fires
in the middleware before the backend is reached, and 19 dead Grafana panels sat behind a 1/1
pod. Exercise the thing you actually changed as well.

## Before you start, and when to stop

Read `CLAUDE.md` → *After a PR Merges* for the *When to wait* list and the *Working alongside
other sessions* notes. Both stay resident there deliberately: they change default behaviour, so
they have to be loaded whether or not this skill is.
