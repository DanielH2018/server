# claude-otel — Claude Code telemetry stack (k3s, daniel-box)

Loki + Prometheus + Tempo + OTel Collector + Grafana in the `observability` namespace.
Claude Code runs on the **host**, not in a container, and exports to `127.0.0.1:4317` —
a `hostPort` bound to loopback, so the collector never becomes LAN-reachable. See
`defaults/main.yml` for why that IP is not a MetalLB VIP.

## The trap: an idle stack is indistinguishable from a broken one

Verified 2026-08-05. The stack ran 47h with every pod Ready, zero export failures, and the
Kuma heartbeat green — while carrying **one metric name, no prompt/tool logs, and zero spans**.
Nothing was wrong. Two behaviours combine to produce that appearance:

1. **Telemetry config is read once, at session start.** Editing the `env` block in
   `settings.json` does nothing to a session already running. This stack's keys landed
   2026-08-03 12:38; a session whose process started 2026-08-02 18:41 exported nothing for
   its entire life. `/proc/<pid>/environ` is not a reliable check either — Claude Code
   configures its own SDK rather than exporting that block, so a correctly-configured
   session showed just one OTEL var there
   (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`, itself contradicting the
   `cumulative` in settings). Compare process start time against the settings mtime instead.
2. **An idle session emits only `claude_code.session.count`.** It is a cumulative counter
   re-sent every `OTEL_METRIC_EXPORT_INTERVAL`, so `otelcol_receiver_accepted_metric_points`
   climbs steadily and the pipeline looks busy while carrying nothing of substance.

So: **do not diagnose this stack from counters alone.** Rising metric points prove the
transport works, not that anything useful is flowing. `telemetry-health.sh` deliberately
checks reachability and export *failures* — it cannot detect "nobody is using it", and a
no-data alarm would fire every time daniel-box sits idle.

## Verifying the pipeline in one command

A one-shot headless session exercises every signal end to end:

```bash
claude -p "Run the bash command: echo otel-probe-marker. Then reply with only the word: done" \
  --allowedTools Bash --model claude-haiku-4-5-20251001
```

Then read the three backends. ClusterIPs change on recreate, so resolve them first:

```bash
kubectl -n observability get svc -o custom-columns=NAME:.metadata.name,IP:.spec.clusterIP
```

```bash
# Metrics — expect 4 names, not 1
curl -s http://<otel-collector>:8889/metrics | grep -v '^#' | sed 's/{.*//' | sort -u

# Logs — expect user_prompt / assistant_response / tool_decision / tool_result / api_request.
# The `query=` key and an explicit `start` are both required; omitting either returns empty.
curl -s -G http://<loki>:3100/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="claude-code"}' \
  --data-urlencode 'limit=300' --data-urlencode 'start=<unix-nanoseconds>' \
  | grep -o '"event_name":"[a-z_]*"' | sort | uniq -c

# Traces — expect a claude_code.interaction root span
curl -s -G http://<tempo>:3200/api/search --data-urlencode 'tags=' --data-urlencode 'limit=5'
```

Healthy output after one probe session: metric names `session_count`, `token_usage_tokens`,
`cost_usage_USD`, `active_time_seconds`; the five content event types above, with
`user_prompt` carrying the prompt verbatim (`OTEL_LOG_USER_PROMPTS=1`); and one
`claude_code.interaction` trace.

## Content logging is on

`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`, `OTEL_LOG_TOOL_DETAILS`, and
`OTEL_LOG_TOOL_CONTENT` are all `1` — prompts, responses, and tool output land in Loki
**verbatim**. That is the intended configuration, and it is the reason the OTLP port is
bound to loopback rather than published. Treat Loki's retention window as holding the same
sensitivity as the transcripts themselves.
