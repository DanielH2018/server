# n8n — Prod (active) workflow-failure monitoring via Uptime Kuma

**Date:** 2026-06-08
**Status:** Approved (design)
**Context:** n8n runs production automations but failed *executions* are invisible until
someone opens the UI. The homelab already alerts on infra health through `monitor-bridge`
(a stdlib-Python sidecar that pushes `up|down` to Uptime Kuma **push** monitors). This adds
a 10th check that pages when an **active** n8n workflow has a failed execution — reusing the
existing pattern rather than standing up anything new.

## Goal

Surface failed executions of "Prod" workflows as an Uptime Kuma push monitor, defined
entirely in the IaC pipeline (Ansible + AutoKuma label + unit-tested Python). No clicking in
the n8n or Kuma UI beyond the one-time API-key/push-token prerequisites.

**"Prod" = active workflows** — any workflow toggled Active in n8n (running on a
trigger/webhook/schedule). This maps directly to the n8n API's `?active=true` filter.

Non-goals (YAGNI): per-workflow tag filtering; handling the rarer `crashed` status; n8n
Prometheus metrics/scrape; a dedicated Grafana panel; an in-n8n Error Workflow. All are easy
follow-ons if wanted later.

## Why this approach

Considered three options (see brainstorming):

- **A — `monitor-bridge` polls the n8n REST API (chosen).** Drops into the existing
  check contract; "active workflows" is a native API filter; fully IaC + unit-testable;
  the push-monitor up/down recovery model works cleanly because the check evaluates a
  rolling window.
- **B — in-n8n Error Workflow → Kuma push.** Event-driven but lives inside n8n's DB (not
  clean IaC) and fights the push-monitor model — nothing pushes `up` on success, so the
  monitor never cleanly recovers (the [[kuma-push-down-no-heartbeat]] gotcha).
- **C — n8n Prometheus metrics → `prom_vector` check.** No API key, but n8n's exported
  execution counters are version-dependent and carry **no "active" label**, so Prod scope
  is unachievable.

## Architecture

- **No new container.** Extend the existing `monitor-bridge` role.
- **Reachability:** add `apps` to monitor-bridge's `networks` (host_vars), so it can reach
  `http://n8n:5678/api/v1/...` on the internal Docker network — the same trusted-infra move
  it already makes for `kopia`. This **bypasses Traefik/Authelia**; the `X-N8N-API-KEY`
  header is the auth.
- **Detection model:** every `INTERVAL` (300s) the check evaluates failures over a rolling
  `N8N_FAIL_WINDOW` (default 15m) and pushes `up|down` — the same window-based,
  stateless, auto-recovering shape as `check_oom` / `check_restarts`. A 15m window is 3× the
  poll interval, so no failure slips between polls, and a single failure stays visible ~15m
  before auto-recovery.

## Detection logic (`check_n8n` in `check.py`)

Each loop:
1. `GET /api/v1/workflows?active=true` → build `{id: name}` for active ("Prod") workflows.
2. `GET /api/v1/executions?status=error&limit=100` → recent failed executions.
3. Keep failures whose `workflowId` is in the active set **and** whose `stoppedAt`
   (fallback `startedAt`) is within `N8N_FAIL_WINDOW`.
4. `> N8N_FAIL_MAX` (default 0) failures → `down`, naming offenders sorted by count, e.g.
   *"2 active workflow(s) failed in 15m: Backup Sync (3), Invoice Flow (1)"*.
   Else `up`: *"no active-workflow failures in 15m"*.

**Pure logic factored out** as `n8n_failures(workflows_json, executions_json, window_s, now)`
→ `[(workflow_name, count), ...]` sorted desc — unit-tested without HTTP, mirroring
`backup_age_hours`. `check_n8n()` does the two HTTP GETs and calls it.

**Graceful states:**
- Empty `N8N_API_KEY` → `up` with *"n8n monitoring disabled (no API key)"* — avoids a false
  page during rollout before the key is set.
- API unreachable / non-success → the loop's existing per-check `try/except` renders `down`
  with the error string (a dead API surfaces, not silent-green) — consistent with
  `check_targets_down`.
- No active workflows / no recent failures → `up`.

**Timestamps:** reuse the existing `parse_rfc3339()` helper (handles trailing `Z` and
sub-microsecond precision) for n8n's ISO timestamps.

## Components / changes

| File | Change |
|------|--------|
| `roles/containers/monitor-bridge/files/check.py` | Add `N8N_*` env reads, `n8n_failures()` pure helper, `check_n8n()`, and a `("n8n", _env("KUMA_PUSH_N8N", ""), check_n8n)` entry in `CHECKS`. |
| `roles/containers/monitor-bridge/files/test_check.py` | New cases for `n8n_failures` (see Testing). |
| `roles/containers/monitor-bridge/templates/docker-compose.yml.j2` | New env: `N8N_URL=http://n8n:5678`, `N8N_API_KEY={{ n8n_api_key }}`, `N8N_FAIL_WINDOW=15m`, `N8N_FAIL_MAX=0`, `KUMA_PUSH_N8N={{ monitor_bridge_n8n_push_token }}`. New AutoKuma label: `{{ kuma('monitor-bridge-n8n', monitor_type='push', name='n8n Prod Workflows', interval=600, push_token=monitor_bridge_n8n_push_token) }}`. |
| `ansible/inventory/host_vars/daniel-server.yml` | Add `- apps` to monitor-bridge's `networks`. |
| `ansible/vars/secrets.yml` | Add `monitor_bridge_n8n_push_token` (32 alphanumeric chars) and `n8n_api_key`. |
| `roles/containers/monitor-bridge/CLAUDE.md` | Document 10 checks, the n8n check, the `apps` network, the API-key prereq, and the two new env tunables. |

**No change** to `monitor-bridge/meta/deps.yml`: deploy ordering doesn't strictly require n8n
(an unreachable n8n just makes the monitor `down`), and the bridge already depends on
prometheus/uptime-kuma/kopia. (Optional: add n8n to `role_deps` so a full deploy brings n8n
up first — decide during implementation; default is to leave deps unchanged.)

## Window / unit note

`N8N_FAIL_WINDOW` and `N8N_FAIL_MAX` follow the `RESTART_WINDOW`/`RESTART_MAX` convention
(window as a duration string parsed to seconds, max as a float; alert when count > max).
`N8N_FAIL_MAX=0` means "any failure pages".

## Secrets & operator prerequisites

1. `sops ansible/vars/secrets.yml` → add `monitor_bridge_n8n_push_token` — **exactly 32
   alphanumeric chars** (Kuma rejects others; AutoKuma silently refuses the monitor on an
   invalid token). e.g. `openssl rand -hex 16`.
2. In the n8n UI: *Settings → n8n API → Create an API key*, scoped to read **workflows** and
   **executions**. Add as `n8n_api_key` in the same `secrets.yml`.
3. Notifications attach automatically — the `kuma()` macro tags every monitor with the
   AutoKuma-managed Discord notification (`kuma_notification_id`). No per-monitor clicking.

## Testing

Unit tests (`uv run pytest ansible/roles/containers/monitor-bridge/files`), TDD-first:
- Failure of an active workflow **inside** the window → reported, named, counted.
- Failure **outside** the window → ignored.
- Failure of an **inactive** workflow → ignored (not in the active set).
- **Multiple** failures of the same workflow → counted and summed; offenders sorted desc.
- **Empty** executions / empty workflows → no offenders.
- Execution missing `stoppedAt` → falls back to `startedAt`.

Also covered automatically by the `pytest` prek hook.

## Deploy / verification path

1. TDD `n8n_failures` (red → green), then implement `check_n8n`.
2. Add the two secrets; add `apps` to host_vars; wire env + label.
3. `prek run --all-files` (lint + template validation + tests).
4. `uv run ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`.
5. Smoke test: `docker exec monitor-bridge python /app/check.py --once` → confirm the n8n
   line logs `OK` (or a descriptive `DOWN`), and the **n8n Prod Workflows** monitor appears
   in Uptime Kuma.

## Risks / edge cases

- **API key scope:** must include workflows+executions read, or both GETs 401 → monitor goes
  `down` with the error (visible, not silent). Documented in the prereqs.
- **High failure volume:** `limit=100` on the `status=error` query is ample for a 15m window;
  if exceeded we're `down` regardless, so no correctness loss.
- **Secret in rendered compose:** `n8n_api_key` lands in the rendered `containers/` compose
  file — identical to how push tokens and `n8n_runner_auth_token` are already handled; no new
  exposure surface.
