### PR B-1, with B-1d and B-1e

PR B-1, `probe.py health` reads the alert plane: [#5339](https://github.com/DanielH2018/server/pull/5339).
It merged on 2026-10-16 at `ba681c934`, after C7's amendment for Open question 4 and the slice
index's M6 split landed in #5323. On master the gate passed homepage (Ansible-owned, referenced to
`applied_at`), speedtest (`owner: flux`, referenced to its Kustomization's revision) and
netpol-baseline (no route, so the probe half is skipped), each with 0 gated alerts.

PR B-1d, the drill rule: [#5406](https://github.com/DanielH2018/server/pull/5406). It merged on
2026-10-17 at `c9943f04e` and settled with `observability`. `probe.py health littlelink` then
exited 1 and named `HealthGateDrill{severity=critical} firing since 2026-10-17T14:11:13Z`. In the
15 minutes after the rule went live, Alertmanager sent no Discord or email notification, and its
webhook rate matched the two hours before. PR B-1e, the drill's removal:
[#5411](https://github.com/DanielH2018/server/pull/5411). It merged at `f8e28584f` and settled with
`observability`, and `probe.py health littlelink` exited 0 again. littlelink's `applied_at` read
`2026-10-16T23:03:57Z` at all three reads, so the drill holds. D5's flux PR for littlelink no
longer waits on M6 (Open question 12).

C7's content-digest reference: [#5369](https://github.com/DanielH2018/server/pull/5369). It amends
C1, C6 and C7, and the operator accepted the amendment on 2026-10-17. It merged at `2226b7b61` and
settled with `observability`. Every flux tag's `gotk_resource_info` carries `content_digest`, and
peanut's reference stayed at `2026-10-17T14:00:54Z` while three later commits landed.
