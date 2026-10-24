---
id: "0026"
title: "One alert plane on Prometheus and Alertmanager; Uptime Kuma retired"
status: Accepted
date: 2026-10-24
governs: []
---

# ADR-0026: One alert plane on Prometheus and Alertmanager; Uptime Kuma retired

## Status

Accepted. The operator accepted it on 2026-10-24. Slice M6 step C-0 (the
[M6 plan](../target-architecture/monitoring/m6-retire-kuma.md)) wrote this record, and PR
C-1 required that acceptance before it could merge.

## Context

Two systems page today. Prometheus and Alertmanager carry the rules that slices M1 to M5 add.
Uptime Kuma carries one tile per check, and the bridge's checks and about 37 other producers
push a verdict to each tile through a token-keyed URL. Each check pages from one of the two
planes, never both (M1 convention 5).

The [target architecture](../target-architecture/design.md) (*Monitoring*) traces the
monitoring bucket's failures to three classes that Kuma causes:

- **D1.** The Kuma database drifts from git.
- **D2.** Verdicts travel by token-keyed push into a second stateful system.
  [#1803](https://github.com/DanielH2018/server/issues/1803) recorded 602 rejected pushes and
  35 false DOWNs.
- **D3.** Plumbing fans out per check
  ([#3319](https://github.com/DanielH2018/server/issues/3319)).

[#2076](https://github.com/DanielH2018/server/issues/2076) deleted all 107 Kuma monitors and
kept the tool, and the classes returned. At `70591c307` the `uptime-kuma` role is 3,655 lines
with four images and two claims, and 78 push-token secrets belong to M6 to retire. These
figures are context for the decision at the time, not live settings.

Much of `monitor-bridge` duplicates Alertmanager built-ins. Its gates are `inhibit_rules`,
its startup grace and the streaks of the rows that read Prometheus are `for:` and
`keep_firing_for:`, and its resend interval is `repeat_interval`.

## Decision

Every check pages through Alertmanager, and Uptime Kuma is retired.

- **Rules live in git.** `promtool test rules` and pint test them. Alertmanager sends them to
  Discord, email and Home Assistant mobile. A `Watchdog` rule always fires to
  Healthchecks.io, so an Alertmanager outage pages off-site.
- **Producers follow one recipe each.** A check that reads Prometheus becomes a rule. A check
  that reads Loki becomes a LogQL rule on the `loki-homelab` ruler. A host producer writes a
  node_exporter textfile metric. An ingress probe becomes a `blackbox_exporter` module. A
  CronJob producer is read through kube-state-metrics. A check that only the bridge can read
  stays in the bridge, which becomes an exporter on `/metrics`.
- **The readers switch.** `probe.py health <tag>` reads firing alerts from the alert plane,
  `probe.py monitors` lists `ALERTS`, and homepage shows the alert plane.
- **Kuma is deleted.** That covers the `uptime-kuma` role, AutoKuma, `probe.py kuma-drift`,
  the push path and the push-token secrets, with their rotation-registry rows. A census row
  then holds every mention of `kuma` to a frozen allowlist.
- **The cutover keeps one pager per check.** Each cutover splits into one PR that removes the
  Kuma pager and one that drops `shadow` from the rules. A temporary rule, `CheckWithoutPager`,
  pages a tile that has had no pager for two hours.
- **The eight `kuma-check-*` timers keep their names.** The operator ruled on 2026-10-24 to
  keep them, so C-3b does not rename them and shrinks to the task-file rename. The census
  allowlist gains the `kuma-check-` prefix, which the M6 plan's C-3b and C-6 PRs apply.
- **The exit criterion is functional.** No template, task, inventory key, script or skill
  reads Kuma, and no push-token secret remains.

## Alternatives not taken

- **`Gatus`.** The target architecture rejects it. It removes D1 and keeps D2 and D3.
- **Harden Kuma.** The target architecture rejects it. It guards D1 and nothing else.
- **A CronJob that exits non-zero on a bad verdict**, so that kube-state-metrics' failure
  series carry it with no ConfigMap. The readers exit 0 on a bad verdict on purpose, and
  configarr's gate Job would hold a revision on a failing sync. The CronJob recipe reports a
  verdict through a ConfigMap and exports metadata only.
- **A `Pushgateway` or a hostPath textfile for those verdicts.** The operator's ruling rejects
  both. A hostPath would also pin the pods to daniel-box and need a root-owned directory.
- **Annotating the CronJob or its Jobs to carry a verdict.** A `patch` grant on CronJobs lets
  the reader rewrite its own image, and `resourceNames` cannot list the Jobs' generated names.
- **One PR per cutover.** It gives a check two pagers during any interleaved deploy, because
  `observability` ships the live rules from master's tip as soon as the commit merges.
- **Keep the bridge row fields until C-1.** The 17 remaining bridge tiles would go DOWN 1200
  seconds after A6-7 stops their pushes, so A6-7 removes the loop and A6-8 deletes the fields.
- **A fixture tag in a scratch namespace for the health-gate drill.** `probe.py health`
  renders the tag's role, and a scratch tag has no role. It would also need the soak to
  exclude a `drill` label, which M6 Open question 4 rules out. The drill uses littlelink.
- **A merge gate on C-3b.** It depends on a session reading `gitops_state.py` at the right
  moment, and it still misses an apply that runs late. C-3b removes the legacy units in both
  arms of the task file instead.

## Consequences

- **Kuma's history is gone.** C-1 is reversible only until the operator deletes the two
  claims after C-2b. After C-5, restoring Kuma means new tokens, which is a rebuild and not a
  revert. C-1 therefore waits until Phase A has run a week with no Kuma page.
- **There is a no-pager window per batch.** About 15 batches each open a span between the
  Kuma-side PR's deploy and the PR that drops `shadow`. One window is open at a time, and
  `CheckWithoutPager` pages a tile that stays unpaged.
- **Alertmanager becomes the only pager.** The `Watchdog` to Healthchecks.io path covers its
  outage. Kuma's own liveness pager, `KumaDown`, exists only until C-1a deletes it.
- **A health gate that reads alerts fails closed.** After a Prometheus restart, a long-firing
  alert with a short `for:` can read as new, so the gate fails on it and names the alert.
  [#5333](https://github.com/DanielH2018/server/issues/5333) tracks the unmeasured part.
- **The bridge shrinks by about half and stays.** It keeps the checks that only it can read,
  under a size cap, and its tests shrink with it.
- **To reverse,** write a superseding ADR. A Phase A batch reverts by its two PRs while its
  token PR has not landed. Phase C reverts as the M6 plan's *Rollback* section orders.

## Open questions

The M6 plan leaves these open. Each is the operator's or another slice's to settle.

- **C-4b waits for D6.** `narrow_lib/filters.py` refuses any deleted filter plugin. If the
  operator wants C-4b sooner, the narrowing needs its own PR.
- **The Pi's AutoKuma labels.** They are inert once Kuma is gone, and H5 drops them when it
  rewrites the Pi compose files.
- **`ALERTS_FOR_STATE` across a Prometheus restart.** The before and after reading is owed.
- **Registry garbage collection.** D5's wave D registry PR retires its tile and token, and
  C-1 waits for that PR. The fallback if the operator wants Kuma gone first is a B6 PR.

## Governs

No `# DECIDED:` marker references this record yet. The M6 PRs that add the `swallowed_verdicts`
exclusion and the gate skip add the markers and fill the `governs:` list in the same PR.
