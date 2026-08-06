# tempo — Trace backend for Claude Code spans

Grafana Tempo, the third signal in the Claude Code telemetry stack. Claude Code (on the host)
exports OTLP/gRPC to the `otel-collector` on `127.0.0.1:4317`; the collector fans metrics out to
Prometheus, event logs to Loki, and **spans to Tempo**. Read through Grafana's Tempo datasource —
Tempo has no UI of its own. See repo-root `CLAUDE.md` and `otel-collector/CLAUDE.md`.

## At a glance
- **Image:** `grafana/tempo:2.10.7` (pinned; Renovate-tracked via the generic docker-compose
  manager; Watchtower disabled) — **stay on 2.x**, see *3.x is a breaking change* below
- **Host:** daniel-server · **Web UI:** none (`port: false`, no Authelia)
- **Ports:** nothing published. `:4317` (OTLP gRPC ingest, from the collector) and `:3200`
  (query API + `/ready`, for Grafana and Kuma) are reachable only over the `monitoring` net.
- **Networks:** monitoring
- **Depends on:** nothing — Tempo is the sink. `otel-collector/meta/deps.yml` declares `tempo`
  so the producer starts after it.
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`, and
  `files/tempo-config.yaml`

## What lands here
Spans only, and only from Claude Code. The hierarchy is rooted at `claude_code.interaction`
(one per turn) with children `claude_code.llm_request`, `claude_code.tool` →
`claude_code.tool.execution` / `claude_code.tool.blocked_on_user`, `claude_code.hook`,
`claude_code.subagent.spawn`, `claude_code.bash.subprocess`, `claude_code.compaction` and
`claude_code.mcp.rpc`.

This is the only signal that **attributes a turn's wall-clock**: in the metrics and logs, time
spent parked on a permission prompt is indistinguishable from time spent running the tool.
`tool.blocked_on_user` separates them.

**Spans require two env vars on the client**, not one — `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`
(span emission is beta-gated) *and* `OTEL_TRACES_EXPORTER=otlp` (routing). Both are set in the
chezmoi-managed `~/.claude/settings.json`. Setting only the exporter yields silence, and env
changes only apply to **new** Claude Code sessions.

## Notable
- **3.x is a breaking change — do not blind-merge the Renovate major.** Tempo 3.0 completes the
  new ingest/write architecture: legacy ingesters are removed, `block_builder`/`live_store`
  replace them, and `ingester:` / `compactor:` are no longer valid config keys — 3.0.2 rejects
  this repo's config outright (`field ingester not found in type app.Config`). Retention also
  moved (`compaction.compaction.block-retention` → `compaction.block-retention`, under the
  backend-scheduler). Migrating needs the upstream 3.0 migration guide, not a tag bump.
  Verify any bump before deploying:
  `docker run --rm -v <cfg>:/etc/tempo/config.yaml:ro grafana/tempo:<tag> -config.file=/etc/tempo/config.yaml -config.verify=true`
  (the config must be mode 0644 for that check — see below).
- **Runs as uid 10001, not PUID 1000.** Not an LSIO image, so there is no PUID/PGID to set.
  Two consequences: the config is copied **0644** (not the repo's usual 0664) because 10001
  reads it only via the world bit, and the data volume must be 10001-writable.
- **No chown init container is needed.** `/var/tempo` ships *inside* the image owned by
  `10001:10001`, and Docker seeds a fresh named volume from the image directory including its
  ownership. (Upstream's `single-binary` example carries a `tempo-init` chown one-shot; that is
  for older tags — verified unnecessary on 2.10.7.)
- **`tempo_data` is a named volume on purpose** — trace blocks are bulky and regenerable, so
  they stay **out of Kopia's** `containers/` **bind-mount scope**, the same documented exception
  as loki's volume (`.claude/rules/docker.md`). Losing them costs history, not recoverability.
- **No Docker healthcheck — Kuma HTTP-probes instead.** The image is a distroless single Go
  binary (no shell), same as loki and otel-collector, so the `kuma()` label points an HTTP
  monitor at `http://tempo:3200/ready`. `/ready` 503s for a few seconds after a restart while
  the ingester warms up — brief PENDING in Kuma after a deploy is normal.
- **Retention is configured at 30d** (`block_retention: 720h`) — parsed and accepted by the
  pinned binary, but not yet observed enforcing (that takes 30 days). Deliberately shorter
  than the metrics side:
  spans are per-turn and far bulkier than counters, and trace debugging is retrospective by
  days. `max_block_duration: 5m` (vs the 30m default) makes a span queryable minutes after the
  turn that produced it, which is the difference between this being usable mid-session or not.
- **Spans carry content.** `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_TOOL_CONTENT` are on, so span
  attributes include tool arguments and output — treat `tempo_data` as being as sensitive as
  the Loki event stream and the transcripts themselves. Nothing is published off the
  `monitoring` net.
- **Config-change recreate is wired** — the config is bind-mounted `:ro` and read once at
  startup, so a config-only edit forces a recreate via
  `common_config_changed: "{{ tempo_cfg is changed }}"` (see `common/CLAUDE.md`).
- **Consumers:** Grafana's **Tempo** datasource (uid `tempo`), provisioned in the grafana role
  with `tracesToLogsV2` → Loki, paired with a `derivedFields` **TraceID** link on the Loki
  datasource for the reverse jump. Both directions match on the `trace_id` Claude Code stamps
  onto its log events.

## Editing
- Compose: `templates/docker-compose.yml.j2` · Tempo config: `files/tempo-config.yaml`
- Datasource: `../grafana/templates/provisioning/datasources.yml.j2`
- Collector traces pipeline: `../otel-collector/files/otel-collector-config.yaml`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "tempo"` (a config edit forces the
  recreate). After changing the collector's pipeline, redeploy `otel-collector` too.
