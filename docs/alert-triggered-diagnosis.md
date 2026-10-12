# Alert-triggered diagnostic agent — design note and no-go (#4616)

**Recommendation: do not build it now.** Over the 31 days of alert history that Loki keeps,
11 of 70 incidents passed every gate an alert-triggered run needs and paged without naming
their cause. Those 11 come down to 6 causes. Six of the incidents were one recurring Home
Assistant log fault, which #2781 diagnosed and fixed. Two were service outages where a first
diagnosis could have saved time. The role would retire nothing, and on 2026-09-28 the operator
asked for complexity to stay flat. The note records the design so that a later reopening
starts from it, and it names the measurement that would reopen it.

Arming any such role is the operator's decision. This page decides nothing on its own.

## What was proposed

Nothing starts a Claude session from an event. Sessions start from the phone
(`claude-rc.service`), the daily Renovate timer, the weekly eval sweep and fan-out launches.
A DOWN from Uptime Kuma or monitor-bridge starts nothing. The proposal was an "outer loop"
agent (12-factor-agents, factor 11): an alert starts a bounded, read-only session, which posts
a first diagnosis to Discord before the operator opens a session.

The parts exist. `ansible/roles/setup/renovate_agent/` runs a bounded headless `claude -p`
from a systemd unit, under an Autonomous-role contract. `ansible/roles/setup/common/tasks/agent_user.yml`
builds an agent identity. `scripts/diagnostics/probe.py` is an allow-listed read-only
toolkit, and the `homelab-network-diagnostician` agent is a read-only diagnostic brief.

## The benefit, measured

### What the history covers

`probe.py alerts` reads the DOWN lines from two streams in the `loki-homelab` store:
monitor-bridge's checks, and the host crons that push to Kuma through syslog. Loki keeps 744h
(`loki_homelab_retention` in `ansible/roles/k8s/loki-homelab/defaults/main.yml`), so **the
issue's 90-day question cannot be answered from Loki at all.** A 90-day figure below is an
extrapolation from 31 days.

One `probe.py alerts --days 30` call does not reach 31 days either. Loki returns at most 5000
lines per query (`max_entries_limit_per_query`), and the probe keeps the newest lines:

```text
$ uv run python scripts/diagnostics/probe.py alerts --days 30 --limit 30000 --json
(--limit 30000 is above Loki's max_entries_limit_per_query of 5000; using 5000. Narrow --days to see further back.)
(warning: hit --limit 5000 log lines on {container="monitor-bridge"} |= "DOWN" — this window is
cut off at its OLDEST end, so only 2026-09-29 18:18 UTC onwards is covered. Narrow --days.)
```

To read the whole window, the count called `fetch_alert_streams` in
`scripts/diagnostics/probe_lib/alerts.py` 32 times on 2026-10-12, one day per call, moving its
`now` argument back a day each time. It removed duplicate lines at the day boundaries, and
then built episodes with the probe's own `alert_episodes`. 63 of the 64 stream-days fitted
under the cap. The syslog stream for the day ending about 2026-10-09 17:00 UTC still hit
5000 lines, which was the 2026-10-08 recovery burst, so that one day is counted short.

### Episodes and incidents

The walk returned 941 episodes from 2026-09-11 16:26 UTC on. 560 of them lasted one
cycle. To group episodes into incidents, the count merged any episode that started within
30 minutes of the running incident's end. That gave 70 incidents.

Each incident went through the gates the design below requires, in this order:

| Class | Incidents | Rule |
|---|---|---|
| Noise | 19 | Every episode lasted one cycle, or the incident cleared within 30 minutes with no episode past two cycles. |
| No WAN | 1 | `wan_reachable` fired (2026-10-03 to 2026-10-05). A `claude -p` session cannot reach the Anthropic API without WAN, so the agent cannot run during the outage class that lasts longest. |
| A deploy is held | 5 | `gitops_status` reported a held deploy. The hold message already tells the operator to revert the offending PR, and the `hold_sha` gate suppresses the run. |
| Monitoring or daniel-box was down | 5 | `loki_reachable`, `prometheus` or `gitops_alive` fired. These are the recovery bursts, such as 2026-10-08 21:55, when the k3s API refused connections and the agent's own host had been down. |
| The alert names its cause or fix | 29 | Every episode past one cycle came from a check whose message names its cause or fix. Examples: `gitops_status` names the playbook and tags to apply, `manifest-prune-check` names the stale file, `release-staleness-check` names the service, and `arr_queue` names the item and why it needs review. This class also holds `otel-sweep-watch`, which counts Claude session API errors rather than a service failure, and `speedtest`, whose cause is the ISP. |
| Candidate | 11 | An episode past one cycle came from a check whose message does not name the cause: `k8s_workloads`, `ha_heartbeat`, `cluster_targets`, `etcd-restore-drill-vm`, `shipper_dropped` or `traefik_404`. |

The first four rows are mechanical. The split between the last two depends on judgement about
which messages name their cause.

### The 11 candidates

The 11 candidates come down to 6 causes:

- **Six incidents are one fault.** On 2026-09-19, 2026-09-21 (twice) and 2026-09-23 (three
  times), `k8s_workloads` paged on about 22 fatal log lines an hour from Home Assistant.
  #2781 traced them to `pychromecast` stack traces and closed on 2026-09-27, and none recur after
  that. An agent would have run six times for one cause, because the storm limit is per
  incident.
- **Home Assistant down for 11 hours, from 2026-09-24 03:00 UTC.** Home Assistant had no
  available replica. The incident began about 80 minutes after the Renovate bump to
  Home Assistant 2026.9.3 (#2337) merged, and Home Assistant is eligible for auto-deploy.
  Of the 11 candidates, this is the clearest case where a first diagnosis could have saved
  time, though a read-only agent could only point at the bump.
- **The Pi and outbound network, 2026-09-26 13:42, 3.1 hours.** The four daniel-pi scrape
  targets went down, and the Discord and `ipify` lookups failed, while `wan_reachable` stayed up.
- **A failed etcd restore drill, 2026-09-11 16:26.** The drill's scratch server never became
  ready. A drill failure is not an outage.
- **Loki fatal log lines, 2026-09-11 20:16, 0.9 hours.**
- **The edge answering 404 for 94% of traffic, 2026-09-30 11:01.** The alert cites #1322 for
  the known cause.

**Result: in 31 days, 2 service outages (the 2026-09-24 and 2026-09-26 incidents) could have
had a useful first diagnosis.** Scaled to 90 days, that is about 6 outages, and about 32
incidents that pass every gate. Neither outage would have been fixed by the agent itself:
it is read-only, so the operator still acts on its diagnosis.

**The 13-day sample in the first version of this note understated the benefit.** That sample
held 17 incidents, and it misses the syslog stream before 2026-10-08. It recorded 1 candidate,
and the 31-day walk finds no candidate after 2026-09-30 that the sample missed. The rate is
higher in the second half of September, when #2781's fault was live.

## The cost, measured

The armed Renovate agent records `cost_usd` per session in `/var/lib/renovate-agent/runs.jsonl`:

```text
$ jq -s '[.[] | select(.result=="ran" or .result=="failed")] | {n: length,
    total: (map(.cost_usd // 0) | add), max: (map(.cost_usd // 0) | max),
    min: (map(.cost_usd//0)|min), turns_avg: ((map(.turns//0)|add)/length)}' \
    /var/lib/renovate-agent/runs.jsonl
{ "n": 7, "total": 6.1826, "max": 1.6138, "min": 0.4075, "turns_avg": 12.43 }
```

That is $0.88 a session on average, from $0.41 to $1.61. A Renovate session includes landing
waits and a diagnostic session has a different shape, so treat the figure as an order of
magnitude.

| Trigger policy | Runs per 30 days | Spend per 30 days |
|---|---|---|
| Every incident, no gates | about 68 | about $60 |
| After the mechanical gates (noise, WAN, hold, monitoring down) | about 39 | about $34 |
| Only checks whose message does not name the cause | about 11 | about $9 |

The last row needs a list of checks in the wrapper, which is one more thing to keep current.

Money is not the binding cost. The binding cost is a new autonomous role: a unit, a timer, an
agent identity, a prompt, a contract, a Discord digest, an alive tile, and the tests that pin
each. The gates that make the spend small are also code a reviewer has to keep correct.

## The design, if it is ever built

**Source: a pull timer, not a webhook.** Kuma's `discord` notification is its webhook
provider (`docs/uptime-kuma-autokuma-record.md`), so a second webhook could post to a host
listener. That adds an inbound port on daniel-box, triggered by text that includes alert
messages. A host timer that runs `probe.py monitors` and `probe.py alerts` every few minutes
needs no listener. It also matches the pull model of the GitOps deployer
(`docs/adr/0010-pull-based-gitops-over-argo-and-flux.md`). A hook inside monitor-bridge is
the worst choice: monitor-bridge runs in the cluster, and the session runs on the host.

**Storm limit: one lock, one run per incident.** The wrapper holds a lock for the
session. It keys each run on the incident start time, grouped with the same 30-minute merge
gap the count above used. It skips any incident it has already diagnosed, and it caps runs per
day.

**Gates before a session starts:**

- `wan_reachable` is up.
- The deployer's `hold_sha` is empty, and no tick or `deploy.sh` holds the deploy lock.
- The incident has lasted at least two cycles, which removes the 560 one-cycle episodes.
- The host has been up past monitor-bridge's startup grace, which removes the recovery burst.

**Read-only by construction, not by prompt.** A new profile in
`ansible/roles/setup/common/tasks/agent_user.yml`, with:

- no GitHub token at all, so the session cannot comment, push or file;
- a kubeconfig for the `homelab-readonly` ServiceAccount only, which holds `get list watch`
  and cannot read Secrets; `probe.py readonly-rbac` already asserts that it stays read-only;
- no SOPS age key, no sudo and no polkit rule;
- Claude Code settings that deny `Edit`, `Write` and `WebFetch`, because log lines are
  text anyone can write and the session must not be able to send what it reads off the host.

The wrapper, not the session, posts the result to Discord, as the Renovate agent's digest does.

**Abort valves:** `--max-budget-usd` as a runaway catch, a wrapper timeout and a systemd
`TimeoutStartSec` backstop, the same three the Renovate agent uses
(`docs/renovate-agent-bounds-and-digest.md`).

## What it would retire

Nothing. Kuma and monitor-bridge keep paging, because the diagnosis depends on their alerts.
The phone host keeps running, because the operator still has to act on the diagnosis. No
check's message gets shorter. Under the flat-complexity rule this alone decides the
recommendation.

## The zero-complexity alternatives

- **Make the alert name the cause.** 29 of the 70 incidents needed no diagnosis because the
  check's message already carried the cause or the fix. When an alert pages without naming its cause,
  improving that check's message helps every future occurrence for no new role.
- **Start the diagnosis on demand.** From the phone, a `claude-rc.service` session can dispatch
  `homelab-network-diagnostician` or run `probe.py monitors`. The operator already pays the
  wait for a human decision, so the automatic start only saves the minutes before the operator
  picks up the phone.

## When to reopen

Reopen this when 4 or more service outages in 30 days pass the gates above and page without
naming their cause, or when the operator expects to be away from the phone for days at a time.

Count causes, not incidents. The first version of this note set the threshold at 4 incidents,
and the 31-day walk measures 11. That count is inflated by one recurring fault that a single
diagnosis settled (#2781), and by a drill and two causes the alert already pointed to. The
measured rate of service outages is 2 in 31 days.

To re-run the count, walk the window one day at a time as described above, because one
30-day call stops at the 5000-line cap.
