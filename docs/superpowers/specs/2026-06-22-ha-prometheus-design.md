# HA → Prometheus Metrics — Design

**Date:** 2026-06-22
**Status:** approved (pre-plan)
**Origin:** Long-standing approved-but-deferred item (operator picked it after the validation work).
Today HA exposes nothing to the homelab's Prometheus/Grafana stack — observability is the up/wedged
Kuma heartbeat only, no history.

## Goal

Get HA's metrics into Prometheus (`hass_*`, queryable and available to Grafana): automation trigger
counts, entity state/attribute trends, integration health over time.

## Governing principle

> Additive, declarative config; no new heavy CI. There is no unit-testable logic here, so the
> deterministic gate is **post-deploy verification against the live scrape** (the target is `up` and
> a `hass_*` metric returns samples) — the same "prove it actually works" discipline as
> `verify-automations`.

## Non-Goals

- **No Grafana dashboard in this effort (deferred).** The deliverable is metrics flowing into
  Prometheus and queryable. A dashboard (a community HA dashboard, e.g. Grafana id 11693, or a
  custom one in the grafana role) is a clean visualization follow-up. Tracked in memory
  `ha-deferred-followups` so it is not forgotten.
- **No metric filtering.** Expose HA's default curated metric set (`namespace: hass`, no `filter`).
  Cardinality is fine for a homelab; add a `filter` later only if it proves noisy. (YAGNI.)
- **No new long-lived token creation in code.** The dedicated `prometheus_ha_token` is already
  created (HA UI), stored in SOPS, and registered in the rotation registry (assisted tier,
  commit `0d751590`).

## Components — three edits across two roles

### 1. HA exposes metrics — `home-assistant` role

Add a top-level block to `templates/configuration.yaml.j2`:

```yaml
prometheus:
  namespace: hass
```

Enables HA's **core** `prometheus` integration (not HACS) at `/api/prometheus`, which requires a
bearer token. Metrics are prefixed `hass_`. The block is plain YAML (no Ansible `{{ }}`), so it is
copied verbatim and parses under the HA config validator like the rest of the file. It feeds
`common_config_changed`, so the edit recreates HA on deploy.

### 2. Prometheus scrapes HA — `prometheus` role

Add a scrape job to `templates/prometheus.yml.j2` (mirrors the existing `static_configs` jobs):

```yaml
  - job_name: "home-assistant"
    scrape_interval: 1m
    metrics_path: /api/prometheus
    authorization:
      credentials: "{{ prometheus_ha_token }}"
    static_configs:
      - targets: ["home-assistant:8123"]
```

`authorization.credentials` (default type `Bearer`) carries the SOPS-decrypted token. `prometheus.yml.j2`
is Ansible-rendered at deploy; the global "Load encrypted secrets" pre-task puts `prometheus_ha_token`
in scope (referenced as `{{ prometheus_ha_token }}` per the repo secrets convention). The token lands
in the rendered `prometheus.yml` on the trusted host (not in git) — acceptable; the `bearer_token_file`
alternative is unnecessary complexity here. **If the prometheus role does not already load secrets**,
the implementation wires `common_config_changed`/secret access the same way other secret-consuming
roles do (verify during implementation).

### 3. Networking — `host_vars/daniel-server.yml`

Add `apps` to Prometheus's `networks` (currently `[monitoring]` → `[monitoring, apps]`) so it can
resolve `home-assistant:8123`. This is exactly what `monitor-bridge` already does to reach HA over
`apps`. `monitoring` stays `networks[0]`, so Prometheus's own Traefik label binding is unchanged.
HA is **not** moved (it stays off the broad `monitoring` net).

## Verification

- **Pre-deploy:** the `validate-compose` PostToolUse hook re-renders Prometheus's compose (catches a
  malformed `networks` change); the HA config validator parses the new `prometheus:` block.
- **Post-deploy** (deploy both `home-assistant` and `prometheus`):
  - `uv run python scripts/probe.py targets` → the `home-assistant` job shows `health: up` (a `down`
    here with a 401 means the token/auth is wrong; a connection error means the network change
    didn't take).
  - `uv run python scripts/probe.py metric 'hass_…'` (e.g. a known `hass_` metric) → returns samples.
  - Optional: confirm the rendered `prometheus.yml` on the host contains the `home-assistant` job +
    target (without printing the token).

## Boundaries

Two declarative config edits + one host_vars networking line; the credential is already provisioned.
No application logic, no new files. Each edit is independently understandable; the cross-cutting
concern (Prometheus reaching HA with auth) is verified end-to-end by the live scrape check.
