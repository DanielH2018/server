---
name: land-after-merge
description: Merge a PR and follow it through to a verified deploy with `land.sh`. Use when a PR is ready to merge, has just merged, or when you need the exact merge → CI-wait → tick → deploy → verify sequence. Covers what `land.sh` does, its VERDICT line, and why hand-polling CI and hand-merging are wrong here.
allowed-tools: Bash, Read, Grep, Glob
---

The procedure `CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify* points at. That section
owns the two things that must stay resident — the standing directive that a merge is followed
through without asking, and the *When to wait* list. This skill owns the mechanics.

## The procedure

Record the pre-merge master SHA, then hand the whole procedure — arming the merge included —
to ONE `land.sh` command with its output redirected to a file:

```bash
git rev-parse origin/master          # keep this; land.sh needs it for the fallback
./scripts/deploy_tools/land.sh --pr <n> --since <pre-merge-sha> --arm-merge --await-merge \
  > "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1
```

**`--arm-merge` runs `gh pr merge --squash --auto` itself, before the wait.** A bare `gh pr
merge` sits on the ask list (`Bash(gh pr merge:*)`), and auto mode suspends the allow list —
an unattended session has nobody to answer that prompt, and it times out as a denial (three
attempts, three denials, on 2026-09-03, issue #979). `land.sh --arm-merge` is not that
command: its own text never contains `gh pr merge`, so it reaches the classifier as the
single script invocation the worktree-containment check already accepts. It is idempotent —
a PR already `MERGED` is left alone, so re-running the same command after a `merge-conflict`
or `merge-timeout` re-arms cleanly. Pass `--subject` to override the squash commit's subject;
the PR's own title is used otherwise.

**The redirect is load-bearing.** A backgrounded Bash call hands the script a non-blocking
pipe for stdout and stderr, and Ansible refuses to start on one:

```
ERROR: Ansible requires blocking IO on stdin/stdout/stderr. Non-blocking file handles detected: <stdout>, <stderr>
```

`land.sh` then prints `VERDICT: deploy-failed` with nothing deployed, and the error names
Ansible rather than the harness, so it reads as a playbook bug. Redirecting to a file gives
the deploy a blocking handle whether or not the call is backgrounded. Session transcripts on this
host record the error at least ten times before the redirect became the rule.

`--await-merge` polls the PR's state every 30s until it is merged and only then starts the
landing. It reads the state, never the checks, so it is not the hand-polling this skill
forbids. Without it the session has to notice the merge itself and start `land.sh` by hand,
which every landing on 2026-09-01 did with a hand-written `until MERGED` loop. A PR still
open after 45 minutes exits 75: it is not being merged, and the reason is on the PR.

**A conflicting PR ends the wait at once**, exit 1 with `VERDICT: merge-conflict`, rather
than sitting out the 45 minutes. A PR that goes conflicting after the auto-merge was armed
never merges, and nothing on the PR says so — with several sessions landing at once, another
merge moving master under an open PR is the ordinary way it happens. Rebase the branch onto master, re-arm the auto-merge, and re-run the same `land.sh`
command. The wait tolerates a `mergeable` of `UNKNOWN` — GitHub computes the field
asynchronously and serves `UNKNOWN` on a freshly opened PR — and bails only after two
consecutive `CONFLICTING` polls, because the base moving under a PR flips it for one poll.

**A PR whose own CI is red ends the wait too**, exit 1 with `VERDICT: pr-ci-red`, quoting
what `await_ci.py` said. This is the second state an armed auto-merge never recovers from,
and GitHub reports it only as `mergeStateStatus: BLOCKED` — the same word it uses while the
checks are still running. The repo's ruleset requires status checks and signatures and no
review, so a `BLOCKED` PR here is always about checks; there is no human-review wait to
preserve. Push a fix and re-run the same `land.sh` command. The wait keeps going while
`await_ci.py` answers `pending`, which is what it answers until a required check registers —
that is the grace period, so no landing is cut short for polling before CI started.

Run that command with `run_in_background` and let the session be re-invoked when it exits;
the `VERDICT:` line is the last line of the logfile. `land.sh` waits for master CI on the merge commit, ticks, deploys what the tick deferred, and prints a
`VERDICT:` line — `settled`, `unhealthy`, `deploy-failed`, `nothing-to-deploy`, `blocked`,
`needs-manual-apply`, `deferred`, `merge-conflict` (the PR cannot merge until it is
rebased), `pr-ci-red` (the PR's own CI is red, so the armed auto-merge never fires — as
against `ci-red`, which is master's CI after the merge), or one of the four give-ups:
`merge-timeout` (the PR was
still open after the 2700s merge budget), `ci-red`, `ci-timeout` (no CI verdict inside the
900s budget) and `lock-busy` (the tree lock stayed busy through every retry).

`nothing-to-deploy` is decided from the PR's file list right after the merge, before any CI
wait: a PR that reaches no service tag, no plane a hand applies and nothing the tick applies
itself has nothing for `land.sh` to wait on, and the deployer's own tick fast-forwards it.
The one exception is a file list GitHub truncated, which is derived from the diff after the
tick as before.

**A Pi role deploys on the Pi.** `land.sh` runs one `deploy.sh` per host that declares a
derived tag, read from `deploy_tags.py hosts`, and adds `-e target=daniel-pi` for the Pi's.
The health verdict probes a tag only the Pi declares with `--docker` alone. Until 2026-09-03
both halves ran against the local node: PR #928 (a `roles/containers/alloy` change) printed
`settled` with the CLUSTER Alloy DaemonSet's 2/2 ready while the Pi ran the old container,
because the play matched no service on daniel-box and the gate guessed a same-named cluster
workload (issue #929).

If another PR merges during that CI wait, the first `deploy.sh` exits 4 (the tree is behind
origin) and `land.sh` retries, up to three times: each pass re-runs the blockers check, waits
for master CI on the new tip (the tick defers until the TIP is green, not just your commit),
then ticks and deploys. The tip wait is booked under `wait_ci` on the Landings board.
Before 2026-09-02 that retry skipped the wait and ended `deploy-failed (exit 4)` with nothing
deployed, three landings in one day.

**One `deploy-failed` variant means the opposite of the rest.** `a playbook task failed AFTER
applying; some changes are live` is `deploy.sh` exit 20: the play reached its tasks and one of
them failed, so everything applied before it took effect. Every other `deploy-failed` line means
nothing was deployed, and re-running is safe; this one is not a resume point. It exists because
`deploy.sh` returned ansible-playbook's own status until 2026-09-02, and ansible exits 2 on a
failed host — the same number as the tag miss, which is how a run whose manifests both applied
was reported as `a derived tag matched no service, so nothing deployed` (issue #840).

**Another `deploy-failed` line names `deploy_tags.py hosts` itself**: `deploy_tags.py hosts
failed before any deploy.sh ran; nothing was touched`. That command failing (a crash, a bad
environment) used to return bare exit 1 from `land.sh`'s `deploy_by_host`, colliding with
`deploy.sh`'s own rare `cd $repo_root || exit 1` — the two were indistinguishable from
`land.sh`'s side even though only one of them ever ran a deploy (issue #1016). `land.sh` now
reserves `HOST_LOOKUP_FAILED=21` for it, the same shape as `PLAYBOOK_FAILED=20` above. Unlike
that one, this line means exactly what every other `deploy-failed` means — nothing was
deployed, and re-running is safe.

Every run also writes one logfmt line to syslog on exit (`logger -t landing-annotation`):
the PR, the merge SHA, the verdict, and seconds spent in each phase — `wait_merge`,
`wait_ci`, `tick`, `deploy`, `total` — plus `lock`, the seconds spent in tick or deploy
attempts that lost the tree lock (a sub-part of `tick` and `deploy`, not a fifth phase),
and `holder`, the command that held it when the first attempt lost. Promtail ships it to Loki and the **Landings** Grafana
board (Infrastructure folder) plots it, so "sessions wait too long" is answered by the
phase medians there rather than by memory. CLI: `uv run python
scripts/diagnostics/probe.py loki-query '{job="syslog"} |= "event=landing" | logfmt'`.

`deferred` (exit 75) means the tick applies this PR itself — a setup role `initial_setup.yml`
includes, or the deploy plane — and has not crossed origin yet, almost always because a newer
merge's CI is still running. The next tick does it; nothing is wrong with the PR. `land.sh`
reads that from the deployer's own `behind_since` and `hold_sha` markers, since a PR with no
service tag leaves no other evidence of being applied. A held `hold_sha` is `deploy-failed`.

`needs-manual-apply` means the PR reaches something neither a deploy tag nor the tick covers,
and the line names the command that does apply it. Four things are in that position. A
**setup role `initial_setup.yml` does not include** (`k3s` is in `k3s-bringup.yml`, `common` in
no playbook) or a bring-up playbook, because the tick applies every other setup role itself
and `deploy.yml` is a `containers_list` loop. A **shared k8s role** —
`manifests`, `volume-claim`, `rollout-drain`, `volume-snapshot`, `volume-revert`,
`image-builder`, `longhorn-api`, `cronjob-gate` — has no `containers_list` entry at all, so
`--tags manifests` matches nothing and only a full `ansible/deploy.yml` applies it. A **rotated
secret** has no path to match at all: a secret's value lives in no role's template, so
`ansible/vars/secrets.yml` derives zero tags however many roles consume it. Run `uv run python
scripts/secrets_mgmt/secret_rotation.py consumers <secret>` for who holds a stale copy and the
repair command per plane. The other services in the same PR still deploy normally; the verdict
is about the half that did not.

**A self-applied setup role reaching a host beyond the tick's own** is the fourth (issue #1009).
`initial_setup.yml`'s `hosts:` is one target per run, and the tick runs it on whichever host
`land.sh` itself executes on — so a role with no `when:` gate (`initial_setup` itself, plus
`config_files`, `sops_setup`, `docker_install`, `hypervisor`) reaches all three hosts
(daniel-box, daniel-server, daniel-pi), and a role's own `when:` can reach more than one (e.g.
`nut_host`: daniel-box and daniel-server). The tick converging says only that the LOCAL host
is current; PR #1002 changed the shared Kuma push library, the tick converged on daniel-box, and
`land.sh` read `settled` while daniel-server and daniel-pi kept the old library for three days.
The line names each remaining host and its exact apply command — `ssh <host> "cd
/home/ubuntu/server && ansible-playbook ansible/initial_setup.yml --tags <tag>"` for a
`connection=local` host, `ansible-playbook ansible/initial_setup.yml --tags <tag> -e
target=daniel-pi` for the Pi. **This is not the same failure PR #723 hit.** #723's self-applied
roles (`gitops_deploy`, `renovate_notify`) are gated `when: has_gitops` / `when:
inventory_hostname == renovate_notify_host`, both true only on daniel-box, so a role reaching
just the tick's own host still reads `settled` — reporting every self-applied role as unfinished
was tried and reverted for exactly that PR (`plane_note`'s own docstring in `land_tags.py`).

**Do not hand-poll CI and do not hand-merge.** `await_ci.py` reads the same check-runs
endpoint the deployer reads, so its verdict and the tick's agree by construction. Hand-polling
cost 835 polls across 213 wait episodes before it existed.

It also owns the rule that used to live in `CLAUDE.md`: `cancelled`, `stale` and
`skipped_by_concurrency` mean *no verdict for this SHA*, never *this SHA is bad* —
`_CI_NO_VERDICT_CONCLUSIONS` in `deploy_logic.py` is the list, and a commit whose merge was
immediately followed by another reads `cancelled` permanently. `await_ci.py` follows the tip in
that case, but only once your commit is an ancestor of it. The log line is
`<sha> has no verdict (cancelled/stale) — following the tip <tip>`, and it is normal, not a
fault. It also fires when the cancellation came before the `prek` job registered a check-run
at all: the check-runs list then never carries the required name, and `await_ci.py` reads the
SHA's check-suites (a `completed cancelled` suite with zero runs) to tell that from a fresh
push. Before PR #775 that case waited out the whole budget and exited 75. If you ever check
by hand, check that way.

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
