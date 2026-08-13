# otel-collector — Claude Code telemetry forwarder (since slice-7 D7)

Since D7 (2026-08-10) this is a pure **forwarder**: Claude Code (on the host) exports
OTLP/gRPC to `localhost:4317`; this collector receives it and chains ALL THREE pipelines
(metrics, logs, traces) to the **cluster claude-otel collector** over its ClientIP-gated
ingest route (`claude-otel-ingest-k8s.local.<domain>`, write-only — see
`k8s/claude-otel/templates/ingest-ingressroute.yaml.j2`). Nothing lands in the Docker
monitoring stack anymore: the Docker tempo retired with this change (its only feeder), the
Docker prometheus dropped its `otel-collector` scrape job, and the Docker loki's Claude
stream ended — which was the Phase D.2 step-4 gate. The container itself dissolves at the
Phase F join, when a collector DaemonSet gives daniel-server its own loopback hostPort and
the ingest route is deleted.

**Why a container survives as "just a forwarder":** the `localhost:4317` endpoint is
identical in both hosts' chezmoi-managed `settings.json`, and a host shell resolves -k8s
names to the wrong Traefik (split-horizon) while containers resolve them via Pi-hole. The
forwarder keeps the client contract and the DNS path correct at once, with zero settings
divergence between hosts.

**Content flows through here verbatim** (prompts, responses, tool output —
`OTEL_LOG_USER_PROMPTS` etc. are `1`), TLS-encrypted in transit to the cluster, stored
only in the loopback-only claude-otel stack. Read-side sensitivity now lives entirely on
daniel-box; see `k8s/claude-otel/CLAUDE.md`.

## At a glance
- **Image:** `otel/opentelemetry-collector-contrib:0.157.0` (pinned; Renovate-tracked;
  Watchtower disabled)
- **Host:** daniel-server · **Web UI:** none (`port: false`, no Authelia)
- **Ports:** OTLP `127.0.0.1:4317` (gRPC only) — **host loopback only**. `:13133` (health)
  reachable over `monitoring` for ad-hoc checks; nothing probes it (the Kuma tile was
  deleted at D7 — docker-fleet covers container liveness, the cluster's telemetry-health
  cron covers the stack it feeds).
- **Networks:** monitoring · **Depends on:** nothing local (`meta/deps.yml` is empty —
  the exporter queues until the cluster answers)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`, and
  `templates/otel-collector-config.yaml.j2`

## Notable
- **The traces pipeline must exist for spans to be accepted at all** — an `otlp` receiver
  only registers the gRPC TraceService when some pipeline consumes traces (learned
  2026-08-06, when spans were silently refused).
- `memory_limiter` stays first in every pipeline (shed an OTLP burst before the 256M
  cgroup cap OOM-kills the process).
- **Config-change recreate is wired** — the config is bind-mounted `:ro` and read once at
  startup (`common_config_changed: "{{ otel_collector_cfg is changed }}"`). No persistent
  volume; the config is regenerable from this role.
- **Consumers moved with the stream:** the live Claude Code board is the cluster
  claude-otel Grafana's; the Docker AI/claude-code board was removed at D7 (pre-D7 metric
  history stays queryable in the Docker prometheus TSDB until retention ages it out).
  homelab-mcp's `claude_code_usage`/`claude_code_events` tools read the Docker stores and
  are dark for post-D7 data until the Phase G homelab-mcp redesign.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Collector config: `templates/otel-collector-config.yaml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "otel-collector"` (a config
  edit forces the recreate)
