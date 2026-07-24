# otel-collector — OpenTelemetry collector for Claude Code telemetry

A single OpenTelemetry collector that gives Claude Code's OTLP export a sink and wires it
into the observability stack already running here. Claude Code (on the host) exports OTLP/gRPC
to `localhost:4317` (`settings.json`); this collector receives it, re-exposes the metrics for
Prometheus, and forwards the event logs to the existing Loki — rather than standing up the
portable `~/claude-otel` bundle, which ships its own Grafana/Loki/Prometheus and would collide
on 3000/9090/3100. **Metrics + event logs only — no prompt/response/tool content.** See
repo-root `CLAUDE.md`.

## At a glance
- **Image:** `otel/opentelemetry-collector-contrib:0.157.0` (pinned; Renovate-tracked via the
  generic docker-compose manager; Watchtower disabled)
- **Host:** daniel-server · **Web UI:** none (`port: false`, no Authelia)
- **Ports:** OTLP `127.0.0.1:4317` (gRPC) / `4318` (HTTP) — **host loopback only** (Claude Code
  runs on the host, not a container). `:8889` (Prometheus scrape) and `:13133` (health) are
  reachable only over the `monitoring` net.
- **Networks:** monitoring
- **Depends on:** prometheus, grafana (loki lives in the grafana compose) — deploy ordering via
  `meta/deps.yml` toposort, not compose `depends_on` (the collector queues its Loki export if
  Loki isn't up yet)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`, and
  `files/otel-collector-config.yaml`

## Notable
- **Loopback-only OTLP is deliberate.** 4317/4318 are published to `127.0.0.1` so nothing lands
  on the LAN. The receiver binds `0.0.0.0` *inside* the container, so `monitoring`-net peers can
  also push to it — accepted, the same unauthenticated same-net trust boundary as Loki's push
  API and the Prometheus scrape plane.
- **No Docker healthcheck — Kuma HTTP-probes instead** (same reason as loki: the otelcol-contrib
  image is a distroless single Go binary, no shell). The `health_check` extension listens on
  `:13133` and the `kuma()` label points an HTTP monitor at `http://otel-collector:13133/`.
  Prometheus also scrapes `:8889`, so Scrape Targets double-covers the collector's death.
- **Config choices** (`files/otel-collector-config.yaml`): `memory_limiter` is first in both
  pipelines so an OTLP burst is shed before hitting the 256M cgroup cap; `metric_expiration:
  168h` keeps idle cumulative counters from flickering to "No data" between usage bursts;
  `resource_to_telemetry_conversion` turns bounded OTLP resource attrs (model, terminal) into
  metric labels for the by-model breakdown.
- **Config-change recreate is wired** — the config is bind-mounted `:ro` and read once at
  startup, so a config-only edit forces a recreate via `common_config_changed:
  "{{ otel_collector_cfg is changed }}"` (see `common/CLAUDE.md`). No persistent volume →
  **nothing in Kopia scope** (the config is regenerable from this role).
- **Consumers:** the "Claude Code — Usage & Observability" Grafana board
  (`grafana/files/dashboards/AI/claude-code.json`) and homelab-mcp's `claude_code_usage` /
  `claude_code_events` tools read this stream (Prometheus + the Loki OTLP logs).

## Editing
- Compose: `templates/docker-compose.yml.j2` · Collector config: `files/otel-collector-config.yaml`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "otel-collector"` (a config edit
  forces the recreate)
