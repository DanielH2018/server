---
name: renovate-prs
description: Work through the repo's open Renovate PRs — triage each by class, finish the half Renovate could not do, then merge and land. Use when asked to go through / review / clear the Renovate or dependency PRs, when one has sat open for days, when a bot PR's group name says `manual — ...`, or when a green Renovate PR turns out to bump only part of a pin (a 404 asset URL, a stale checksum, a version its runtime rejects).
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

Renovate opens PRs here that are **deliberately incomplete**. Ten package rules carry
`automerge: false` and a group name ending in a parenthetical — `(manual — finish the targetAbi
+ MD5, raise jellyfin with it)`, `(manual — finish the per-arch sha256 from checksums.txt)`,
`(lockstep: app + task runners)`. That parenthetical is a **work order**, not a label. Merging
such a PR on green CI ships half a bump.

The rest is ordinary: triage by class, finish what needs finishing, then land each one through
`land-after-merge`.

## 1. Triage

```bash
gh pr list --author app/renovate --state open --limit 50 \
  --json number,title,headRefName,createdAt,mergeStateStatus \
  --template '{{range .}}{{.number}} | {{.title}} | {{.headRefName}} | {{.mergeStateStatus}} | {{.createdAt}}{{"\n"}}{{end}}'
```

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

**The title's parenthetical is the fastest tell.** A title reading `Update n8n (lockstep: app +
task runners)` names its group, and the group name is the work order.

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

## 5. Stale PRs need a rebase before landing

A digest PR opened days ago pins what was current then. Check the digest against the registry
before landing it, and if it has moved, hand the refresh back to Renovate — tick the rebase
checkbox in the PR body:

```bash
body=$(mktemp)
gh pr view <n> --json body -q .body | sed 's/- \[ \] <!-- rebase-check -->/- [x] <!-- rebase-check -->/' > "$body"
gh pr edit <n> --body-file "$body"
```

Renovate refreshes the branch within a cycle. Do not hand-edit the digest: the next Renovate
run would rewrite it anyway.

## 6. Land them one at a time

Follow the `land-after-merge` skill per PR — `gh pr merge --squash --auto`, then one backgrounded
`land.sh --pr <n> --since <sha> --await-merge` with its output redirected to a file.

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

## When to stop and say so

- The rule's parenthetical names work you cannot verify — an upgrade plan (`k3s control plane`),
  a DB format migration (`meilisearch`), a WAF component needing a deliberate redeploy
  (`crowdsec bouncer plugin`). Report what the rule asks for and leave the PR open.
- The new version's release notes name a breaking change. Renovate does not read them; you do.
- A landing hits a genuine hold — `CLAUDE.md` → *When to wait* governs, not this skill.
