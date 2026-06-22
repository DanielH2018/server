# HA → Prometheus Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose HA metrics to Prometheus (`hass_*`) so they're queryable and available to Grafana.

**Architecture:** Three declarative edits across two roles — HA enables its core `prometheus` integration; Prometheus adds a bearer-auth scrape job (token already in SOPS); Prometheus joins the `apps` network to reach HA. No application logic; the gate is post-deploy scrape health.

**Tech Stack:** Home Assistant core `prometheus` integration, Prometheus `static_configs` scrape, Ansible (`community.sops` secret already provisioned), `probe.py` for verification.

## Global Constraints

- **`namespace: hass`, no `filter`** — HA's default curated metric set (YAGNI on filtering).
- **Auth via the dedicated `prometheus_ha_token`** (already in SOPS + rotation registry, commit `0d751590`); referenced as `{{ prometheus_ha_token }}` (global `community.sops.load_vars` puts it in scope). Never echo the token value.
- **`monitoring` stays Prometheus's `networks[0]`** (Traefik binding unchanged); `apps` is added second — mirrors `monitor-bridge`. HA is NOT moved onto `monitoring`.
- **No Grafana dashboard, no metric filtering** (deferred — memory `ha-deferred-followups`).
- **`containers/` is read-only** — edit role `templates/` + `host_vars` only.
- **Verification is post-deploy** (no unit-testable logic): the `home-assistant` scrape target must be `up` and a `hass_*` metric must return samples.

---

### Task 1: Enable HA metrics, add the scrape job, and the network

**Files:**
- Modify: `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2` (add the `prometheus:` block)
- Modify: `ansible/roles/containers/prometheus/templates/prometheus.yml.j2` (add the scrape job)
- Modify: `ansible/inventory/host_vars/daniel-server.yml` (add `apps` to Prometheus's networks)

**Interfaces:**
- Consumes: `prometheus_ha_token` (SOPS, in scope via `load_vars`); the `home-assistant` container name resolvable over the `apps` network.
- Produces: HA `/api/prometheus` endpoint; a Prometheus `home-assistant` scrape job.

- [ ] **Step 1: Enable HA's prometheus integration**

In `ansible/roles/containers/home-assistant/templates/configuration.yaml.j2`, add a top-level block immediately after the `system_log:` block (the one with `fire_event: true`), before `http:`:

```yaml
# Expose HA metrics to the homelab Prometheus at /api/prometheus (core integration, bearer-auth).
# Scraped by the `home-assistant` job in the prometheus role; metrics are prefixed `hass_`. No
# `filter` — HA's default curated metric set (tune later only if cardinality is noisy).
# Spec: docs/superpowers/specs/2026-06-22-ha-prometheus-design.md.
prometheus:
  namespace: hass
```

- [ ] **Step 2: Add the Prometheus scrape job**

In `ansible/roles/containers/prometheus/templates/prometheus.yml.j2`, add this job after the `terraria-stats` job (the last `job_name` block, ~line 66-69):

```yaml

  - job_name: "home-assistant"
    scrape_interval: 1m
    metrics_path: /api/prometheus
    authorization:
      credentials: "{{ prometheus_ha_token }}"
    static_configs:
      - targets: ["home-assistant:8123"]
```

- [ ] **Step 3: Add `apps` to Prometheus's networks**

In `ansible/inventory/host_vars/daniel-server.yml`, the `prometheus` entry's `networks` is currently:

```yaml
    networks:
      - monitoring
```

Change it to (keep `monitoring` first):

```yaml
    networks:
      - monitoring
      - apps  # reach home-assistant:8123 for the /api/prometheus scrape (mirrors monitor-bridge)
```

- [ ] **Step 4: Validate the HA config**

Run: `uv run python scripts/validate_ha_config.py`
Expected: exit 0 — the new `prometheus:` block parses (top-level key, plain YAML).

- [ ] **Step 5: Confirm the compose templates still render**

Editing `host_vars` triggers the `validate-compose` PostToolUse hook automatically; if running validation manually, run: `uv run python scripts/validate_compose_templates.py`
Expected: success — Prometheus's rendered compose now includes the `apps` network with no YAML error.

(Note: `prometheus.yml.j2`'s rendered content — including the `{{ prometheus_ha_token }}` substitution — is only fully exercised at deploy; there is no offline Prometheus-config validator. The post-deploy scrape-health check is its gate.)

- [ ] **Step 6: Commit**

```bash
git add ansible/roles/containers/home-assistant/templates/configuration.yaml.j2 \
        ansible/roles/containers/prometheus/templates/prometheus.yml.j2 \
        ansible/inventory/host_vars/daniel-server.yml
git commit -m "feat(ha): export metrics to Prometheus (hass_* via /api/prometheus, dedicated token)"
```

---

## Deploy & verify (operator follow-up — not part of subagent execution)

Both roles must deploy (HA gains the endpoint; Prometheus gains the job + network):

1. `uv run ansible-playbook ansible/deploy.yml --tags "home-assistant,prometheus"` (HA recreates ~120s; Prometheus recreates on the config change).
2. `uv run python scripts/probe.py health home-assistant` → exit 0.
3. **Scrape health (the gate):** `uv run python scripts/probe.py targets` → the `home-assistant` job shows `health: up`.
   - `down` with a 401 → token/auth wrong (re-check `prometheus_ha_token`).
   - `down` with a connection/DNS error → the `apps` network change didn't take (re-check host_vars + that Prometheus recreated).
4. **Metrics present:** `uv run python scripts/probe.py metric 'hass_…'` (a known `hass_` series, e.g. an entity-state metric) → returns samples.
5. Optional: confirm the rendered `/home/ubuntu/server/containers/prometheus/prometheus.yml` contains the `home-assistant` job + target (without printing the token).

No Grafana dashboard (deferred — memory `ha-deferred-followups`).

## Notes for the executor

- If executed via subagent-driven-development: the implementer reports only; the controller runs the gate and commits explicit paths. The token is already in SOPS — the implementer never handles the raw value (only the `{{ prometheus_ha_token }}` reference).
