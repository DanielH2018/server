---
name: renovate-prs
description: Work through the repo's open Renovate PRs — triage each by class, finish the half Renovate could not do, then merge and land. Use when asked to go through / review / clear the Renovate or dependency PRs, when one has sat open for days, when a bot PR's group name says `manual — ...`, or when a green Renovate PR turns out to bump only part of a pin (a 404 asset URL, a stale checksum, a version its runtime rejects).
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

Renovate opens PRs here that are **deliberately incomplete**. Ten package rules carry
`automerge: false` and a group name ending in a parenthetical — `(manual — finish the targetAbi
+ MD5, raise jellyfin with it)`, `(manual — finish the per-arch sha256 from checksums.txt)`,
`(manual — append each resolved version to base-pin-history.tsv; lockstep: app + task runners)`.
That parenthetical is a **work order**, not a label. Merging such a PR on green CI ships half a
bump.

The rest is ordinary: triage by class, finish what needs finishing, then land each one through
`land-after-merge`.

## 0. An update with no PR is still an update

`gh pr list` cannot see an update Renovate detected but never raised. Those sit in the
Dependency Dashboard's **Pending Status Checks** section (issue #3), and they are supposed to
leave it within their `minimumReleaseAge` — 3 days for a digest bump, 7 for a version one.
Measured 2026-09-02, seven had not: grafana/promtail sat there for 111 days against a 7-day
soak, so the homelab ran promtail 3.3.0 that whole time (issue #886).

`renovate-notify` now measures each item's continuous dwell in that section and posts a Discord
digest naming any that passes its soak plus a 7-day grace. **The remedy is to tick the item's
checkbox on issue #3** — that forces the branch and the PR within a couple of minutes, after
which the update becomes an ordinary PR and the triage below applies. To see the section
without waiting for the digest:

```bash
gh issue view 3 --json body -q .body | sed -n '/## Pending Status Checks/,/^## /p'
```

The underlying reason Renovate holds these is undetermined — it runs here as the Mend hosted
app, whose run log lives on developer.mend.io and is not readable from a session. The check
surfaces the symptom; it does not fix the cause.

## 1. Triage

```bash
gh pr list --author app/renovate --state open --limit 50 \
  --json number,title,headRefName,createdAt,mergeStateStatus \
  --template '{{range .}}{{.number}} | {{.title}} | {{.headRefName}} | {{.mergeStateStatus}} | {{.createdAt}}{{"\n"}}{{end}}'
```

**Compare each PR's merge base against `origin/master` before you read any diff.** Renovate
regenerates a PR's body from current data but leaves the branch on the base it was cut from, so
the body describes one bump while the branch writes another. `gh pr diff` renders against that
same stale base, which is why the disagreement survives a careful read of the diff.

```bash
git fetch -q origin master && git rev-parse origin/master
gh pr list --author app/renovate --state open --limit 50 \
  --json number,baseRefOid,title -q '.[] | "\(.baseRefOid[0:8]) #\(.number) \(.title)"'
```

Any PR whose `baseRefOid` is not that SHA is triaged as stale: tick its rebase checkbox (§5)
and read the diff only after Renovate has refreshed the branch. Two of six open Renovate PRs
failed this on 2026-09-10, and both were raised within seven minutes of each other, so **the
tell is the age of the merge base, not the age of the PR**:

- #1513 — body table `0.5.0` → `0.5.2`, branch writes `prek==0.5.0` into
  `.github/workflows/ci.yml`, which is what master already held. Merged as-is it lands nothing
  behind a green CI. A tooling pin that silently no-ops has no repo-side guard.
- #1507 — body table `307d606` → `f98bb7c` for `n8nio/n8n`, branch writes a digest resolving
  to 2.37.9 against a live 2.37.10. That downgrade is caught by
  `test_the_ledger_last_entry_is_the_live_pin`, but only after the ledger row is appended.

For each PR read the file list and the diff — `gh pr diff <n> --name-only`, then
`gh pr diff <n> | grep -E '^[-+]' | grep -vE '^(\+\+\+|---)'`. The files decide the class:

| Files | Class | Handling |
|---|---|---|
| `uv.lock` | lock file maintenance | merge; nothing to deploy |
| `ansible/roles/k8s/*/defaults/main.yml` | k8s image pin | merge, land with the role's tag |
| `ansible/roles/setup/*/defaults/main.yml` | host plane | often `manual —`; check the rule |
| `ansible/roles/k8s/*/templates/Dockerfile*.j2` | in-cluster-built image | merge, land, then verify the pod took the rebuild |
| `prek.toml`, `.github/workflows/*` | tooling | merge; CI is the only consumer |
| anything else | read the rule | see below |

**The title's parenthetical is the fastest tell.** A title reading `Update n8n (manual — append
each resolved version to base-pin-history.tsv; lockstep: app + task runners)` names its group,
and the group name is the work order — here, resolve each new digest to its
`org.opencontainers.image.version` label and append a row to
`ansible/roles/k8s/n8n-images/base-pin-history.tsv`, whose header carries the commands. A digest
bump changes no version string, so the diff alone cannot tell an upgrade from a downgrade: PR
#1440 proposed a lockstep downgrade through nine green checks (issue #1493).

**A body naming a package the diff never touches is a triage signal, not a merge blocker.**
#939 opened titled "Update dependency prek to v0.5.0" with prek's release notes attached, but
its diff touched only the Vale binary pin — a second instance of the jellyfin-ani-sync class
below, this time from a group branch Renovate reused across two unrelated deps. Retitle to
match the diff and merge the real content.

## 2. Read the rule that produced the PR

Never guess at what a bot PR left undone — the rule says so, at length:

```bash
jq -r '.packageRules[] | select(.groupName // "" | test("<fragment>")) | .description' renovate.json
```

Every `automerge: false` rule in this repo carries a description explaining **why it cannot
automerge**, which is the same thing as **what you have to finish**. To see the whole landscape:

```bash
jq -r '.packageRules[] | "\(.groupName // "-") | automerge=\(.automerge // false)"' renovate.json
```

Completion criterion for this step: for every open PR you can name either "nothing to finish"
or the specific artifact the rule says Renovate cannot produce.

## 3. Verify what Renovate actually wrote

**A custom regex manager rewrites only what its `matchStrings` capture.** Where a version
appears in a URL more than once — or where a *different* value in the same string is coupled to
it — the rewritten URL is wrong and CI cannot tell, because nothing in the pipeline fetches it.

The 2026-09-02 case: `jellyfin-ani-sync` release assets are named
`<targetAbi>.-.ani-sync_<version>.zip`. Renovate bumped `v4.1` → `v4.4` and `4.1.0.0` →
`4.4.0.0`, leaving `10.11.6.` — a URL that 404s, next to a checksum still belonging to the old
release, for a plugin whose ABI the pinned server then rejects. Three defects, green CI.

So for any PR that changes a download URL or a version used to build one:

```bash
curl -sIL -o /dev/null -w '%{http_code}\n' '<the URL from the diff>'   # must be 200
curl -sL '<url>' | md5sum                                             # must match the pinned checksum
```

Take the URL and checksum from the **publisher's own manifest** where one exists, not by
editing the string Renovate produced.

## 4. Finish the incomplete ones in a worktree

A half-done bump is a normal code change: worktree, fix, test, PR. Two rules specific to here:

- **Rebase Renovate's commit onto master and build on top of it** rather than opening a
  parallel PR. Fetch the bot's branch, `git rebase origin/master`, add your commit, push to
  your own branch, then `gh pr close <renovate-pr>` naming the superseding PR. Keeping the
  bot's commit preserves the provenance of the version bump.
- **A coupled pin moves with it.** `raise jellyfin with it` means the image bump is part of
  the same PR, because the repo guards the pair (`test_anisync_pin_matches_server.py`). Run
  the guard the rule points at; it is the completion criterion.

## 5. A stale merge base needs a rebase before landing

A branch pins what was current when it was cut. Age of the PR is one way that happens and the
smaller one — §1's `baseRefOid` check is the reliable tell, and it fires on a PR raised
minutes ago. Check the digest against the registry before landing it, and where either the
digest or the base has moved, hand the refresh back to Renovate — tick the rebase checkbox in
the PR body:

```bash
body=$(mktemp)
gh pr view <n> --json body -q .body | sed 's/- \[ \] <!-- rebase-check -->/- [x] <!-- rebase-check -->/' > "$body"
gh pr edit <n> --body-file "$body"
```

Renovate refreshes the branch within a cycle. Do not hand-edit the digest: the next Renovate
run would rewrite it anyway.

## 6. Land them one at a time

Follow the `land-after-merge` skill per PR — one backgrounded
`land.sh --pr <n> --since <sha> --arm-merge --await-merge` with its output redirected to a
file. `--arm-merge` runs `gh pr merge --squash --auto` inside the script itself, which
matters here: the unattended daily run has nobody to answer the permission prompt a bare
`gh pr merge` raises (issue #979).

**Serialize.** `land.sh` retries a stale tree three times and then gives up with
`deploy-failed (exit 4)`; running two landings while other sessions are also merging burns those
retries on each other. When several PRs touch nothing in common, one `land.sh` with an explicit
`--tags a,b,c` covering all of them costs one lock acquisition instead of three.

## 7. Verify the bump, not the rollout

`VERDICT: settled` says the workload is healthy. It cannot say the new version is running.

- **An upstream version bump:** ask the app. `kubectl -n homelab logs deploy/<svc> | grep -i version`,
  or the service's own version endpoint.
- **A built image** (`Dockerfile*.j2`): confirm the pod resolved the digest the registry serves.
  A failure here reads `<svc> is stale: the registry serves sha256:… but at least one running
  pod resolved something else` — the drift gate in `ansible/post_tasks/k8s_image_drift_gate.yml`.
  `ansible/tests/k8s/test_built_images_pull_always.py` guards the usual cause.
- **A plugin or extension:** confirm the host loaded it, not just that the file is on disk.

## 8. Census the branches no PR and no dashboard entry speaks for

A `renovate/*` branch outlives the PR that carried it. The repo sets
`delete_branch_on_merge`, but that removes only the head branch of a PR GitHub merged, so a PR
closed by hand and a branch Renovate cut but never raised both leave a branch behind. Such a
branch reads `diverged` against master and is invisible to every arm of `renovate-notify` by
construction: the notifier reads open PRs and the Dependency Dashboard, and an orphan branch
appears in neither. Nothing else reports it, so census it here.

```bash
gh api repos/DanielH2018/server/branches --paginate -q '.[].name' | grep '^renovate/' | sort > /tmp/rb-all
gh pr list --state open --limit 100 --json headRefName -q '.[].headRefName' > /tmp/rb-live
gh issue view 3 --json body -q .body | grep -oE '[a-z-]+-branch=renovate/[^ ]+' | sed 's/.*branch=//' >> /tmp/rb-live
sort -u /tmp/rb-live -o /tmp/rb-live
comm -23 /tmp/rb-all /tmp/rb-live
```

Both queries are deliberately wider than the markers and authors seen on any one day (#1629).
The dashboard grep matches `[a-z-]+-branch=` rather than the three verbs issue #3 happened to
carry, because Renovate emits other section markers with their own `<verb>-branch=` prefix and an
unmatched one makes every branch in that section read as an orphan. The `gh pr list` carries no
`--author` filter, because the question is whether ANY open PR speaks for the branch — §4's
pattern of rebasing the bot's commit onto your own branch opens exactly such a PR. Both errors
ran toward over-reporting, so neither ever hid an orphan.

Twelve branches answered that on 2026-09-10, and nine branches with an empty orphan set answered
it later the same day — the branches were pruned in between, so treat the count as a reading
rather than a baseline. Sort each one into a class before you say anything
about it. `git fetch -q origin` then
`git diff origin/master...origin/<branch> | grep -E '^\+[^+]'` names the file and the value the
branch writes — then read that key's value **on master**, because a three-dot diff renders the
`-` side from the merge base and inherits §1's trap:

1. **Settled.** Master is at, or ahead of, the value the branch writes — merge residue, or a
   bump a later one overtook. Nothing is pending and nothing is lost. Eight of the twelve were
   residue and three more were overtaken, `renovate/prek` writing `prek==0.5.0` against a
   master already on 0.5.2.
2. **A bump for a dependency master no longer has.** `renovate/k8s-image-grafanapromtail` bumps
   `grafana/promtail` in `ansible/roles/k8s/loki-homelab/defaults/main.yml`; that file pins
   `grafana/alloy` now, and no k8s role pins promtail at all. The bump has nothing to land on.
3. **A pending update that lost its dashboard entry.** The dependency still exists in the tree
   and master's pin is behind what the branch writes. This is the only class that costs
   anything, and it is §0's failure with the dashboard row gone too — file a finding naming the
   dependency and the version master is stuck on.

**Report the list; never delete a branch.** A branch with no PR is not proof the work on it is
gone, and a sweep is the operator's call. When the operator asks for one,
`git push origin --delete <branch>` per branch is the whole of it. Do not reach for a config
fix instead: Renovate's own `pruneStaleBranches` defaults to true and should already remove an
orphan branch it created, and why it has not here is undetermined — the Mend hosted run log is
not readable from a session, the same limit §0 names. Name the orphan list in the run's
closing summary so the count is visible over time.

## When to stop and say so

- The rule's parenthetical names work you cannot verify — an upgrade plan (`k3s control plane`),
  a DB format migration (`meilisearch`), a WAF component needing a deliberate redeploy
  (`crowdsec bouncer plugin`). Report what the rule asks for and leave the PR open.
- The new version's release notes name a breaking change. Renovate does not read them; you do.
- A landing hits a genuine hold — `CLAUDE.md` → *When to wait* governs, not this skill.
