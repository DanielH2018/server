# Alert-triggered diagnostic agent — design note and no-go (#4616)

**Recommendation: do not build it now.** Over the 13 days of alert history the probe could read,
1 of 17 incidents had a cause that an automatic first diagnosis could have found before the
operator did. The role would retire nothing, and on 2026-09-28 the operator asked for
complexity to stay flat. The note records the design so that a later reopening starts from it,
and it names the measurement that would reopen it.

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

`probe.py alerts` reads monitor-bridge's DOWN lines from the `loki-homelab` store. Loki keeps 744h
(`loki_homelab_retention` in `ansible/roles/k8s/loki-homelab/defaults/main.yml`), so **the
issue's 90-day question cannot be answered from Loki at all.** Inside those 31 days, the probe
hits Loki's 5000-line `max_entries_limit_per_query` and keeps the newest lines. The readable
window was 2026-09-30 11:01 UTC to 2026-10-12, about 13 days:

```text
$ uv run python scripts/diagnostics/probe.py alerts --days 30 --limit 30000 --json
(--limit 30000 is above Loki's max_entries_limit_per_query of 5000; using 5000. Narrow --days to see further back.)
(warning: hit --limit 5000 log lines on {container="monitor-bridge"} |= "DOWN" — this window is
cut off at its OLDEST end, so only 2026-09-29 18:18 UTC onwards is covered. Narrow --days.)
```

A per-day `count_over_time` over 30 days returned between 8 and 873 DOWN lines a day, with
no visible step at 2026-09-30. The 13 days are therefore a truncated sample, not a quiet
month. A 90-day figure below is an extrapolation from them.

### Episodes and incidents

The probe returned 210 episodes. 102 of them lasted one cycle. To group episodes into
incidents, the count merged any episode that started within 30 minutes of the running
incident's end. That gave 17 incidents.

Each incident was then put through the four filters that any alert-triggered run would need:

| Class | Incidents | Why a first diagnosis does not shorten it |
|---|---|---|
| Noise: one cycle, or cleared before a session could finish | 8 | Three `traefik_404` edge spikes, a one-cycle `release-staleness-check`, `gitops_status` and `cert`, and two `memory` episodes about a retired uid. Each cleared by itself. |
| The alert text already names the cause or the fix | 5 | `gitops_status` tells the operator to apply `ansible/k3s-bringup.yml` with the unapplied tags. `pvc_fullness` names the PVC that reports nothing. `swallowed_verdicts` names the cron whose push token Kuma rejects. |
| No WAN | 1 | 2026-10-03 21:29 and 2026-10-05 21:43, `wan_reachable` down for 1.2h and 1.7h, merged into one incident by the long `arr_queue` and `speedtest` episodes around them. A `claude -p` session cannot reach the Anthropic API without WAN, so the agent cannot run during the outage class that lasts longest. |
| A deploy is held or failed | 2 | Both Home Assistant outages (2026-10-10 04:10 and 2026-10-11 00:43) coincide with `gitops_status` reporting a held deploy. The hold message already tells the operator to revert the offending PR. The issue's own gate (no run while `hold_sha` is non-empty) suppresses both. |
| daniel-box itself was down | 1 | 2026-10-08 21:55: 24 checks fired at once with the k3s API refusing connections and the deployer last run 4334 minutes earlier. The agent runs on daniel-box, so it was down for the same outage and woke into the recovery burst. |

The one remaining incident is the candidate: `longhorn-backup-health` from
2026-10-09 10:40, which names stale volumes but not why their backups stopped. That alert recurs (69 episodes in the
window) and is a slow condition, not an outage, so a diagnosis minutes after the first page
saves little over the operator's next session.

**Result: about 1 incident a diagnosis could shorten per 13 days, so roughly 2
per 30 days and about 7 per 90 days by extrapolation.** None of them was an outage where minutes mattered.

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
| Every incident, no filters | about 39 | about $34 |
| After the four filters | about 2 | about $2 |

Money is not the binding cost. The binding cost is a new autonomous role: a unit, a timer, an
agent identity, a prompt, a contract, a Discord digest, an alive tile, and the tests that pin
each. The filters that make the spend small are also the code a reviewer has to keep correct.

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
- The incident has lasted at least two cycles, which removes the 102 one-cycle episodes.
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

- **Make the alert name the cause.** Five of the 17 incidents needed no diagnosis because the
  check's message already carried the fix. When an alert pages without naming its cause,
  improving that check's message helps every future occurrence for no new role.
- **Start the diagnosis on demand.** From the phone, a `claude-rc.service` session can dispatch
  `homelab-network-diagnostician` or run `probe.py monitors`. The operator already pays the
  wait for a human decision, so the automatic start only saves the minutes before the operator
  picks up the phone.

## When to reopen

Reopen this when the post-filter count reaches 4 or more incidents in 30 days whose alert text
does not name the cause, or when the operator expects to be away from the phone for days at a
time. Re-run the classification above first. Narrow `probe.py alerts --days` to get under the
5000-line cap, and walk the window in pieces.
