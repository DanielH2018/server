# CI pipeline + Renovate auto-test-deploy + archive cleanup

- **Date:** 2026-06-07
- **Status:** Approved (design) — pending spec review
- **Author:** Daniel (with Claude)
- **Scope:** new `.github/workflows/`, `renovate.json`, a new `ansible/roles/setup/gitops_deploy/`
  role, `ansible/vars/secrets.yml` (one new secret), CLAUDE.md backfills, and
  `ansible/inventory/host_vars/daniel-server.yml` + `roles/containers/minecraft` cleanup.

## Problem

Three gaps surfaced during a non-security review of the homelab:

1. **No CI.** `.github/` holds only `copilot-instructions.md`. Every quality gate
   (`ansible-lint`, `validate_compose_templates.py`, the pytest suites, gitleaks) runs
   **only locally via `prek`**. Renovate opens up to 4 image-bump PRs every Monday and
   nothing validates them before they can reach `master` — the branch `deploy.yml` reads.
2. **Stale config clutter.** `inventory/host_vars/daniel-server.yml` carries 16
   commented-out `containers_list` blocks for services that have already been relocated to
   `roles/containers/archive/`. Separately, `roles/containers/minecraft/` is an orphaned
   top-level role (commented out in inventory) that should be archived like
   valheim/terraria/foundry.
3. **No path from a Renovate image bump to a deployed, verified container.** Pinned-image
   bumps require a manual `ansible-playbook` run, and nothing checks the new image actually
   works.

## Goals

- Validate every PR and push to `master` with the **existing** quality gates, server-side.
- For **Renovate-managed pinned images** with semver-comparable tags, provide an
  end-to-end flow: bump → pre-merge smoke test → auto-merge (minor/patch) → host deploy →
  post-deploy health gate → automatic local rollback + alert on failure.
- Tidy `host_vars`/`minecraft` into the established `archive/` convention **without losing**
  the information needed to reactivate a service.

## Non-goals / scope boundaries

- **Out of scope: the ~41 `:latest` images.** Those are Watchtower's domain (daily pull,
  1-week cooldown) and are deliberately unpinned; this pipeline does not touch them.
- **Out of scope: non-semver pinned tags** (`master-web`, `master-collector`, `release`,
  `jvm-stable`, `python3.12-bookworm-slim`, `python:3.12-alpine`). Renovate's docker
  datasource can't version-compare them, so they never produce a bump this flow acts on.
- The flow therefore governs the handful of semver-pinned images:
  cadvisor (`v0.53.0`), meilisearch (`v1.37.0`), influxdb (`2.2`), couchdb (`3`),
  uptime-kuma (`2`), alpine-chrome (`124`). (`influxdb:2.2` is a stale pin; `2.2 → 2.x` is a
  *minor* bump, so it **will** auto-merge — the post-merge health gate + local rollback
  (Section D) is the safety net if a Scrutiny TSDB incompatibility surfaces. An InfluxDB
  *major* would stay manual like every other major.)
- **Major** version bumps are **never** auto-merged, for any package or manager — they keep
  opening a normal (CI-tested) PR for a deliberate human look, matching the current
  `renovate.json` intent. This is enforced by scoping the `automerge` rule to
  `matchUpdateTypes: ["minor","patch"]` only; no rule grants `automerge` to `major`.
- No deploys run from GitHub-hosted runners. Hosts are `ansible_connection=local`, LAN-only,
  no inbound; CI is static validation + image smoke testing only. Deployment is pull-based
  on the host.

## Decisions (resolved during brainstorming)

| Decision | Choice |
|---|---|
| How CI reaches the host | **Pull-based GitOps** (systemd timer on the host), not a self-hosted runner |
| What "test the image" means | **Both** — a shallow pre-merge smoke test *and* a post-merge health gate |
| Health-gate failure behaviour | **Local rollback + alert** (no repo write access) |
| Auto-merge scope | **minor/patch only**; majors stay manual |
| Poll interval | **30 minutes** |
| Alert channel | **New dedicated Discord webhook**, stored in SOPS, separate from the `discord` notifier |

---

## Section A — CI workflow

**Artifact:** `.github/workflows/ci.yml`. Triggers: `pull_request` and `push` to `master`.

**Behaviour:** on a GitHub-hosted `ubuntu-latest` runner, check out with `fetch-depth: 0`
(so gitleaks can scan history), install Python + the `prek` binary, and run
`prek run --all-files`.

**Rationale:** running `prek` directly makes CI and the local pre-commit hook a single
source of truth — they cannot drift. `prek` provisions each hook's isolated environment
itself (ansible collections for `ansible-lint`, `pytest`/`pyyaml`/`ansible-core` for the
test hook, jinja2 for the template validator), so the workflow stays a thin wrapper.

**Boundaries:** pure static validation. No host access, no SOPS decryption, no deploy.

**This workflow is the required status check** that gates Renovate auto-merge (Section C),
so branch protection on `master` must require it.

---

## Section B — Archive cleanup + detail preservation

One commit, three steps, **no behavioural change** (the deleted blocks are inert today):

1. **Backfill** each `roles/containers/archive/<svc>/CLAUDE.md` so it records that service's
   intended `containers_list` entry — `port`, `networks`, `use_authelia`, `tags` — read
   from the comment blocks before they are removed. Most archive CLAUDE.md files already
   carry an `Intended:` line (e.g. wallabag's `port 80 · apps net · Authelia: no`); only
   gaps are filled. This is what satisfies "keep the details in case they're un-archived."
2. **Move** `roles/containers/minecraft/` → `roles/containers/archive/minecraft/` and add a
   CLAUDE.md in the same style, matching how valheim/terraria/foundry were retired.
3. **Delete** the 16 commented `containers_list` blocks from
   `inventory/host_vars/daniel-server.yml`.

**Result:** `host_vars` is the clean list of what is actually deployed; `archive/` is the
single record of parked services and how to bring them back (`archive/CLAUDE.md` already
documents the reactivation steps).

---

## Section C — Renovate: pre-merge smoke test + auto-merge

### C1 — `renovate.json` change

Add `automerge: true` and `platformAutomerge: true` to the **existing minor/patch
packageRule only** (`matchManagers: ["custom.regex"]`, `matchUpdateTypes:
["minor","patch"]`). Majors are untouched and keep opening a normal PR. Auto-merge is
contingent on the CI checks passing (Section A + C2) via branch protection.

### C2 — Pre-merge image smoke test

**Artifact:** a job (in `ci.yml` or a sibling `image-smoke.yml`) that runs on
`pull_request` events whose diff touches
`ansible/roles/containers/**/templates/docker-compose.yml.j2`.

**Behaviour:**
1. Diff the PR to extract the changed `image:tag` from the `.j2`.
2. `docker pull` it.
3. `docker run -d` it with minimal arguments; then wait for **either** the image's declared
   `HEALTHCHECK` to report healthy, **or** (if the image declares none) the container to
   survive a fixed window (30s) without crash-looping.
4. Tear down. Non-zero exit fails the check and blocks auto-merge.

**Acknowledged limitation (this layer is intentionally shallow):** it runs the image
standalone, so it cannot exercise services that need real secrets, volumes, or network
peers. It catches "the new image is outright broken / won't start." Deeper integration
validation is the post-merge health gate's job (Section D). The job is kept isolated so
that, if it proves noisy, it can be removed in a single-file change without affecting the
rest of the pipeline.

---

## Section D — Host-side GitOps deployer (core new component)

**Artifact:** a new role `ansible/roles/setup/gitops_deploy/` that installs, on
`daniel-server`:

- a deploy script (`/usr/local/bin/gitops-deploy`),
- a **systemd service + timer** firing every **30 minutes**, running as `ubuntu`,
- a state directory `/var/lib/gitops-deploy/` (last-deployed SHA, hold marker).

The script runs `ansible-playbook` exactly as the operator does today; `become_password`
and the new webhook secret are pulled from SOPS non-interactively (the age key is already
on the host).

### Happy path (per tick)

1. `git fetch` `origin/master`. (Read-only deploy key, or no auth if the repo is public —
   to be confirmed at implementation; **no write/push access either way**.)
2. If `origin/master == local HEAD` → exit. If `origin/master == hold-marker SHA`
   (a known-bad commit) → skip and re-alert.
3. `git diff --name-only HEAD origin/master`, map each changed
   `roles/containers/<svc>/templates/docker-compose.yml.j2` to its service tag(s). (A change
   to a shared macro or `host_vars` broadens the set; a Renovate image bump is normally one
   template.)
4. For each changed service, record the **currently-running image digest**
   (`docker inspect`) for possible rollback.
5. `git merge --ff-only`, then `ansible-playbook deploy.yml --tags <svc>` for the changed
   service(s).
6. **Health gate:** poll the container's health status up to `max(5min, 2×start_period)`.
   Healthy → record success, clear any hold, ping the liveness monitor, done.

### Failure path (local rollback + alert)

When the health gate times out without the container becoming healthy:

1. `git reset --hard <prev-HEAD>` — the compose template reverts to the previous tag.
2. Redeploy → the service comes back on the previously-recorded working digest.
3. Write a **hold marker** = the bad SHA so step 2 of the next tick won't redeploy it.
4. Fire a **loud alert** to the dedicated Discord webhook (service name + bad version +
   "revert the PR").
5. When the operator reverts the offending PR, `origin/master` advances past the bad SHA →
   the hold condition no longer matches → the revert deploys cleanly on the next tick and
   the hold clears.

This keeps the host **read-only** against the repo: rollback is local-only and self-guarding,
so no push credentials are needed. The transient git/running-state divergence is explicit
(the hold marker) and surfaced (the alert), rather than silent.

### Observability

- The deployer pings an **Uptime-Kuma push monitor** every tick, so a *dead deployer*
  surfaces through the existing monitor-bridge → Uptime-Kuma → Discord alerting.
- Rollback events alert via the dedicated webhook (`gitops_deploy_discord_webhook`).

### Secret

`gitops_deploy_discord_webhook` is added to `ansible/vars/secrets.yml` (SOPS/age). It is a
**new, dedicated** webhook, distinct from the shared `discord` notifier used by AutoKuma, so
deploy/rollback alerts can be routed or muted independently. The value is never stored in
plaintext in any tracked file (this spec included).

---

## Data flow (end to end, pinned minor/patch image)

```
Renovate (Mon)                GitHub                         daniel-server (every 30m)
   │                            │                                   │
   ├─ open PR (bump tag) ──────▶│                                   │
   │                            ├─ CI: prek run --all-files         │
   │                            ├─ CI: image smoke (pull+run+probe) │
   │                            │      └─ green ──┐                  │
   │                            ├─ auto-merge ◀───┘ (minor/patch)    │
   │                            ├─ master advances                   │
   │                            │                                    │
   │                            │◀──────── git fetch ───────────────┤
   │                            │                 detect changed svc │
   │                            │                 record old digest  │
   │                            │                 ff-merge + deploy  │
   │                            │                 health gate        │
   │                            │             healthy ─▶ success +   │
   │                            │                        kuma ping   │
   │                            │             unhealthy ─▶ reset +   │
   │                            │                redeploy old digest │
   │                            │                + hold marker       │
   │                            │                + Discord alert ────┼─▶ #deploys
```

## Testing

- **Section A:** the workflow itself is exercised by every PR; correctness = `prek` parity
  (the hooks already have their own pytest coverage).
- **Section C2:** validate the smoke job against a known-good bump (passes) and a deliberately
  bad tag (fails the check, blocks merge).
- **Section D:** the deploy script's pure logic (diff→service mapping, hold-marker
  comparison, rollback decision) gets pytest coverage under `ansible/tests/` or alongside the
  role, consistent with the toposort/monitor-bridge test pattern. Manual end-to-end: trigger
  a real minor bump and confirm deploy+health-gate success; simulate a failure (e.g. a tag
  that boots unhealthy) and confirm local rollback + hold + alert.

## Open implementation details (decide during writing-plans)

- Whether the repo is public (no fetch auth) or needs a **read-only** deploy key.
- Exact mapping rules for non-`docker-compose.yml.j2` changes (shared macros, `host_vars`)
  to the set of services to redeploy — conservative default: if a shared template or
  `host_vars` changed, the deployer alerts and defers to a manual full deploy rather than
  guessing scope.
- Smoke-test handling for images that need a minimal env to start at all (e.g. couchdb
  single-node admin creds) — provide throwaway env in the job, or skip-with-note per image.
