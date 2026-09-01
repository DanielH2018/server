# Recreate gap baseline

Measured 2026-08-16 with `uv run python scripts/dev/startup_baseline.py` against the live cluster.

The spec estimated a 15–45s `Recreate` gap from probe configuration, and proposed tuning
`terminationGracePeriodSeconds`, `minReadySeconds` and image pre-pull across ~35 roles on the
strength of it. **The measurement does not support that work.** Details below.

## Method, and why it differs from the plan

Plan Task 4 said: deploy five sampled services while polling, and record the gap. That cannot
run as written. A rollout only fires when a deploy changes a rendered manifest
(`manifests_render is changed`), and none of those services has a pending change — so the
deploys would perform no rollout and the polling would return a meaningless PASS against a
stable service. Producing five measurements would have meant five contrived edits.

Kubernetes already records the dominant half of the gap on every running pod: the time from the
current container starting to the pod reporting Ready. That needs no deploy, so it covers all
54 running workloads instead of a sample of five.

**What this does not measure: the termination half.** A pod that ignores SIGTERM burns its full
`terminationGracePeriodSeconds` before the new one starts, and only a real rollout reveals that.
Treat every number below as a lower bound on the gap.

## Results

| Statistic | Value |
|---|---|
| Workloads running | 54 |
| With a `readinessProbe` | 40 |
| Median start→ready (probed) | **11s** |
| p90 (probed) | **31s** |
| Max (probed) | **310s** (radarr) |
| Over 30s | 4 |
| No `readinessProbe` | 14 |

The slow tail, which is the whole story:

| Service | start→ready |
|---|---|
| radarr | 310s |
| sonarr | 250s |
| home-assistant | 41s |
| n8n | 31s |
| loki-homelab | 30s |

Everything else is 28s or less, and the bulk of the fleet sits at 10–22s.

`terminationGracePeriodSeconds` is the default 30s on 49 of 50 Deployments; only valheim sets it
(120s).

## What the numbers implicate

**Fleet-wide approach-B tuning is not worth doing.** Three findings kill it:

1. **The grace period is not the bottleneck.** It only contributes when an app ignores SIGTERM;
   a well-behaved app exits in a second or two and never approaches its 30s allowance. Lowering
   the setting across 35 roles would change nothing for the apps that already exit promptly, and
   for one that hangs it would swap a slow shutdown for a killed one. There is no measurement
   here showing any app burning its grace period — establishing that needs a real rollout per
   service, which is the same cost as the tuning it would justify.
2. **`minReadySeconds` is the wrong direction.** It *delays* a rollout being considered
   complete. It protects against a pod that reports Ready before it can serve; it does not
   shrink a gap.
3. **The real outliers are application startup, which no Kubernetes setting fixes.** radarr at
   310s and sonarr at 250s are 4–5 minutes of downtime per deploy — an order of magnitude worse
   than the spec's 15–45s estimate, and untouchable by grace periods, pre-pull, or probe tuning.
   These two are also on the "Postgres for durability, stays Recreate" list in the spec, so
   nothing in the current programme improves them.

**The estimate was right for the middle and wrong at the edges.** 15–45s describes the typical
holdout well (median 11s start→ready plus a short termination). It understates radarr and sonarr
by roughly an order of magnitude.

**A separate finding worth its own work: 14 running workloads have no `readinessProbe`,** so the
kubelet marks them Ready the instant the container starts. Their ~0s here is an absent
measurement, not a fast one. For a `Recreate` workload that mostly costs honesty in dashboards;
it becomes a correctness problem the moment any of them is converted to rolling, which is why
`ansible/tests/test_deploy_strategy.py` already fails a rolling Deployment behind a Service that
lacks one.

## Recommendation

Drop the fleet-wide tuning from approach B. Replace it with:

- **Nothing for the 35 typical holdouts.** An 11–22s gap on a homelab service, on the rare
  occasions its manifests change, does not justify 35 role edits and the risk of shortening a
  grace period on an app that needs it.
- **Investigate radarr and sonarr specifically** if their 4–5 minute gaps actually bother you.
  That is an application-startup question (database migrations, library scans on boot), not a
  Kubernetes one.
- **Add readinessProbes to the 14 workloads lacking them,** as a correctness fix rather than a
  speed one — and a prerequisite for ever converting any of them.
