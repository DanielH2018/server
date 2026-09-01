---
name: land-after-merge
description: Merge a PR and follow it through to a verified deploy with `land.sh`. Use when a PR is ready to merge, has just merged, or when you need the exact merge → CI-wait → tick → deploy → verify sequence. Covers what `land.sh` does, its VERDICT line, and why hand-polling CI and hand-merging are wrong here.
allowed-tools: Bash, Read, Grep, Glob
---

The procedure `CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify* points at. That section
owns the two things that must stay resident — the standing directive that a merge is followed
through without asking, and the *When to wait* list. This skill owns the mechanics.

## The procedure

Record the pre-merge master SHA, merge, then run the follow-through as ONE backgrounded
command:

```bash
git rev-parse origin/master          # keep this; land.sh needs it for the fallback
gh pr merge --squash
./scripts/deploy_tools/land.sh --pr <n> --since <pre-merge-sha>
```

Run `land.sh` with `run_in_background` and let the session be re-invoked when it exits. It
waits for master CI on the merge commit, ticks, deploys what the tick deferred, and prints a
`VERDICT:` line — `settled`, `unhealthy`, `deploy-failed`, `nothing-to-deploy`, `blocked` or
`needs-manual-apply`.

`needs-manual-apply` means the PR reaches something no deploy tag covers, and the line names the
command that does apply it. Three things are in that position. The setup plane needs
`initial_setup.yml`, because `deploy.yml` is a `containers_list` loop. A **shared k8s role** —
`manifests`, `seed-volume`, `rollout-drain`, `volume-snapshot`, `volume-revert`,
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
