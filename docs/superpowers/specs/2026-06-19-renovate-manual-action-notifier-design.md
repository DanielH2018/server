# Renovate manual-action notifier — Discord, notify-on-change

**Date:** 2026-06-19 · **Status:** Draft (awaiting review) · **Host:** daniel-server

## Problem

Renovate auto-merges only low-risk, soaked, CI-green container-image and prek-hook
minor/patch bumps. Everything else needs a human:

- **Manual-merge queue** — PRs that will never auto-merge: majors, Ansible collections
  (e.g. `community.sops`), the weekly `uv.lock` refresh (`lockFileMaintenance`), and
  meilisearch (DB-format migration).
- **Stuck PRs** — PRs that *should* auto-merge but can't: a required check is failing or the
  branch is conflicting (PR #8 on 2026-06-19 was exactly this — a stale, conflicting branch
  parking a red `image-smoke`).

Both classes are invisible unless the operator remembers to open the Dependency Dashboard.
There is no push signal, so a stuck or waiting PR can sit for weeks. The operator wants a
**Discord** nudge for anything actionable, **without notification spam**.

## Goals

- One Discord notification when — and only when — the **actionable set changes**.
- Cover two buckets: **manual-merge queue** and **stuck** PRs. Ignore on-track PRs (those
  that will merge themselves).
- No new GitHub secret (the repo is public; reading PRs needs no auth).
- Reuse the existing `gitops_deploy_discord_webhook` (no new secret, no rotation entry).
- Follow the established `gitops_deploy` role shape: thin I/O shell + pure, unit-tested
  decision logic.
- **Liveness-monitor the notifier itself** via a new monitor-bridge Kuma push monitor, so a
  silently-dead notifier (the thing whose whole job is to break silence) is itself caught.

## Non-goals (this spec)

- **One-click merge from Discord.** Deliberately deferred to a separate follow-on spec (see
  *Follow-on*). The deploy half is already automatic: once a PR merges to `master`,
  `gitops_deploy` deploys it health-gated within ≤30 min. So the message links to the GitHub
  PR; the operator clicks **Merge** on GitHub (its own auth) and the deploy follows
  automatically. No new write-scoped token or web service in this spec.
- Renovate-system health (Dependency Dashboard lookup failures, "Renovate didn't run").
  Out of scope for now (operator chose manual-queue + stuck only).

## Classification

For every **open** PR authored by Renovate (`user.login == "renovate[bot]"` or
`head.ref` starts with `renovate/`), classify using two signals:

1. **Automerge intent** — Renovate always writes `🚦 **Automerge**: Enabled.` or
   `Disabled.` in the PR body's Configuration section. This is a stable, race-free signal of
   intent (avoids the heuristic trap where a green, soaked auto-merge PR is briefly open at
   snapshot time and gets mis-flagged as manual).
2. **Health** — CI rollup (worst of GitHub check-runs + commit statuses on the head SHA:
   `failure` / `pending` / `success`) and mergeable state (`CONFLICTING` or not).

| Bucket | Condition | Actionable |
|--------|-----------|------------|
| **stuck** | Automerge *Enabled* AND (CI `failure` OR conflicting) | ✅ should land itself, can't |
| **manual** | Automerge *Disabled* | ✅ your decision; annotate CI/conflict state |
| **on-track** | Automerge *Enabled* AND not (failure or conflicting) | ❌ ignore (merges itself) |

## Anti-spam: notify-on-change

- **Fingerprint** the actionable set: sorted `#<num>:<bucket>` pairs.
- Persist to `/var/lib/renovate-notify/last_notified` (JSON).
- **Post only when the fingerprint differs** from the stored one — a PR enters the queue,
  leaves it (merged/closed), or flips bucket (on-track → stuck). Same backlog two runs in a
  row → silence.
- **Empty edges:** non-empty → empty posts a one-line `✅ Renovate backlog cleared`, then
  silence. Empty → empty is silent. So steady state is **≤ one message per real change**.

## Architecture (mirrors `ansible/roles/setup/gitops_deploy`)

```
ansible/roles/setup/renovate_notify/
  files/notify_logic.py        # PURE, no I/O, unit-tested:
                               #   classify_pr(automerge, ci, conflicting) -> bucket
                               #   actionable(prs) -> list[ActionablePR]
                               #   fingerprint(actionable) -> str
                               #   should_notify(prev_fp, cur_fp) -> (bool, kind)
                               #   render_message(actionable, repo_url) -> str
  files/renovate_notify.py     # I/O shell: GitHub REST (stdlib urllib), state r/w, Discord POST
  files/test_notify_logic.py   # pytest (classification truth table, fingerprint stability,
                               #   should_notify transitions incl. empty edges, truncation)
  tasks/main.yml               # install script + /etc/renovate-notify/config.env (0600) + units
  templates/renovate-notify.service.j2
  templates/renovate-notify.timer.j2
```

- **Stdlib only**, like `gitops_deploy` — no `gh`/token dependency.
- Wired into `initial_setup.yml` with `when: inventory_hostname == 'daniel-server'`,
  alongside `gitops_deploy`.
- **Also edits the existing `monitor-bridge` container role** (new check + Kuma monitor +
  bind-mount + env + push token) — see *Liveness monitoring* for the full diff surface.
- Writes `/var/lib/renovate-notify/last_run` (Unix-timestamp) on every clean completion —
  consumed by the monitor-bridge **Renovate Notifier — Alive** Kuma monitor (see
  *Liveness monitoring* below).

### Timer

`renovate-notify.timer`: `OnCalendar=*-*-* 13:00:00` (UTC; host is UTC ≈ 08:00 America/Chicago,
after Monday's `before 6am on monday` Renovate window), `Persistent=true`,
`RandomizedDelaySec=5min`. Daily — catches a Tuesday-stuck PR the same day.

### GitHub access (unauthenticated, public repo)

Per run: 1 list call (`GET /repos/{owner}/{repo}/pulls?state=open&per_page=100`, body
included) + for each Renovate PR ~3 calls (single-PR detail for `mergeable`; `check-runs` +
`status` on head SHA for CI). ≈ <20 calls/day, far under the 60/hr unauthenticated limit.
`mergeable` can be `null` while GitHub computes it — treat `null` as "unknown, not
conflicting" and let the next daily run settle it.

## Config / secrets

`/etc/renovate-notify/config.env` (0600), templated from Ansible:
`REPO=DanielH2018/server`, `DISCORD_WEBHOOK={{ gitops_deploy_discord_webhook }}` (reused),
`STATE_DIR=/var/lib/renovate-notify`. The notifier itself needs **no new secret** (public
repo read + reused webhook). The only new secret is the monitor-bridge **push token** below.

## Liveness monitoring (monitor-bridge)

The notifier's whole job is to break silence, so a silently-dead notifier must itself be
caught. Add an 18th monitor-bridge check, **Renovate Notifier — Alive**, a near-exact clone
of **GitOps Deploy — Alive**:

- **Producer:** `renovate_notify.py` writes `/var/lib/renovate-notify/last_run` (Unix ts) on
  every clean completion. The `renovate_notify` setup role creates that dir owned by the
  deploy user **before** monitor-bridge deploys — same bind-mount-ownership gotcha as
  `gitops_deploy` (else Docker auto-creates the mount source root-owned and the non-root
  container can't read it). So: deploy `renovate_notify` before `monitor-bridge`.
- **Consumer:** monitor-bridge bind-mounts `/var/lib/renovate-notify:/renovate-state:ro` and
  gains env `RENOVATE_STATE_DIR=/renovate-state`, `RENOVATE_MAX_AGE_MIN=2160` (36 h — one
  fully-missed daily run + slack; tunable), and `KUMA_PUSH_RENOVATE_ALIVE={{ monitor_bridge_renovate_alive_push_token }}`.
- **Logic (`check.py`):** pure `renovate_alive(age_s, max_age_s) -> (ok, msg)` mirroring
  `gitops_alive`, plus `check_renovate_alive()` reader (FileNotFound / unparseable → `down`
  with a descriptive msg, never silent-green). Unit-tested in `test_check.py`.
- **Kuma label:** `kuma('renovate-notify-alive', monitor_type='push',
  name='Renovate Notifier — Alive', interval=600, max_retries=0,
  push_token=monitor_bridge_renovate_alive_push_token)`. `interval=600` = 2× the 300 s loop
  (this is monitor-bridge's *own* dead-man's-switch backstop; the daily cadence lives in
  `RENOVATE_MAX_AGE_MIN`, which the check applies to the file's age). `max_retries=0` per the
  role's push-monitor convention.
- **Secret:** new `monitor_bridge_renovate_alive_push_token` (exactly 32 alphanumeric chars,
  e.g. `openssl rand -hex 16`) in `secrets.yml`, passed both as env and as `push_token=` in
  the label; add a `secret_rotation.yml` registry entry (tier matches the other
  `monitor_bridge_*_push_token`s).
- **Housekeeping:** bumps monitor-bridge from seventeen checks to eighteen — update its
  `CLAUDE.md` (the check list + the push-token roster) and the compose count comment.

## Message shape (single Discord post, ≤1900 chars like `gitops_deploy`)

```
📦 Renovate — 2 PR(s) need attention

🔧 Stuck (should auto-merge, can't):
 • #8 container images (non-major) — ❌ image-smoke failing / conflicting
   https://github.com/DanielH2018/server/pull/8

✋ Awaiting your merge (merging → auto-deploys, health-gated, ≤30 min):
 • #9 community.sops → v2.4.0 — major, ✅ green
   https://github.com/DanielH2018/server/pull/9
```

Overflow (more PRs than fit in 1900 chars) collapses to `…and N more`.

## Testing

`notify_logic.py` is pure and fully unit-tested in `test_notify_logic.py`. Add
`ansible/roles/setup/renovate_notify/files` to `pyproject.toml` `[tool.pytest.ini_options]
testpaths` so it runs under `uv run pytest` and the prek `pytest` hook (same as the other
suites). The I/O shell stays thin enough to verify by inspection.

## Follow-on (separate spec): one-click merge approver

A safe Discord "merge & deploy" link needs a small authenticated service, **not** a bare
mutate-on-GET link (Discord's unfurl crawler / browser prefetch would fire it on post —
could auto-merge `community.sops`, which decrypts secrets). Sketch for the follow-on spec:

- GET renders a **confirm page** only; merge happens on POST (defeats prefetch/unfurl).
- Behind **Authelia** + **LAN-only** (`*.local` / WireGuard), per the existing access model.
- **HMAC-signed, single-use, expiring** link tokens (PR# + nonce).
- A **write-scoped GitHub token** (new SOPS secret) — needed only to *merge*; the deploy is
  inherited from `gitops_deploy`, so the service contains no deploy logic.

## Risks / open questions

- **Automerge-line parsing** depends on Renovate's body format. Low risk (stable, documented
  output), but `classify_pr` defaults an *unrecognized* automerge marker to **manual** —
  fail toward surfacing, never toward silence.
- **CI rollup across check-runs + statuses.** GitHub reports CI through two disjoint APIs:
  the **Checks API** (`/commits/{sha}/check-runs` — Actions: `prek`, `image-smoke`) and the
  legacy **Commit Status API** (`/commits/{sha}/status` — `GitGuardian`,
  `renovate/stability-days`). Neither endpoint includes the other's entries, so the verdict
  must read **both** and fold them: failure if any check-run conclusion is failure-like OR
  any status state is failure/error; else pending if anything is incomplete/pending; else
  success. Reading only one side would call a stuck PR healthy. Unit-test the aggregation so
  a `failure` in either source counts.
- **Silent death** of the notifier is covered by the monitor-bridge **Renovate Notifier —
  Alive** monitor (see *Liveness monitoring*) — `down` once `last_run` exceeds
  `RENOVATE_MAX_AGE_MIN`.
