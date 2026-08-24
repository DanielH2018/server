# Backup consolidation — Longhorn becomes the only B2 consumer

Operator decision 2026-08-10, resolving the third transaction-cap event in nine days: rather
than raising the B2 caps so kopia and Longhorn can coexist, kopia RETIRES and Longhorn owns
the backup plane. This supersedes design decision 1's "kopia shrinks to a residual set" —
the residual set is now empty. Kopia is already stopped and removed (today's containment);
this plan makes that permanent and safe.

## What kopia still uniquely protected (measured today)

| Data | Size | Fate |
|---|---|---|
| terraria worlds (`containers/terraria/config`) | 36 M | **The one irreplaceable item.** Terraria moves to k8s NOW (BL1) — its worlds land on a backed-up Longhorn PVC. |
| Docker Authelia DB (TOTP) | 500 K | Gap accepted: Docker-edge logins only, forked from the k8s twin since slice 1, retires at Phase E. Worst case = re-enroll TOTP for the shrinking Docker edge. |
| grafana.db (Docker Grafana) | 120 M | Gap accepted: retires in Phase D's monitoring consolidation; the cluster dashboards are Ansible-rendered ConfigMaps, already in git. |
| traefik `acme.json` | small | Regenerable via Let's Encrypt (rate limits are the only cost). |
| scrutiny influxdb | — | Already kopia-EXCLUDED (kopiaignore); no change. |
| wg-easy config / pi-peers | — | Already seeded to the cluster PVC (Longhorn-backed) at B5; on-disk copy is legacy. |

Everything else under `containers/` is templated or regenerable.

## Decisions

- **BL1 — terraria moves to k8s immediately**, ahead of its Phase C/D slot, because it holds
  the only irreplaceable kopia-only data. Straight port: Longhorn PVC (`longhorn`, backed
  up) seeded from `containers/terraria/config`, TCP 7777 published the wg-easy way
  (Service with the node-IP externalIP — the router forwards to daniel-box already).
  terraria-stats does NOT move (it reads Docker Loki; it re-points in Phase D's Loki step).
- **BL2 — kopia retires now, not at the join.** Role to archive, inventory entry out
  (18 → 17), its five host crons removed, `backup_controller_host` dissolves, kopiaignore
  gone. The six monitor-bridge backup checks (backup, verify, content_verify, maintenance,
  restore_drill, b2_usage/b2_trend) retire with it — their successor plane is
  `longhorn-backup-health.sh`, which already pushes the k3s Longhorn Backup monitor.
  `b2_reachable` stays (Longhorn still needs B2) — its credentials move off kopia's key to
  a dedicated probe key at the next rotation.
- **BL3 (amended by the operator, 2026-08-10) — the kopia B2 repo is deleted once the
  cutover verifies**, not after a retention window: "once everything is cut over to
  Longhorn, I don't feel the need to keep Kopia's backup anymore." Concretely: after the
  first Longhorn-only nightly completes and its restore points list clean, the kopia
  prefix in the bucket goes, and `kopia_password` + the pinned-tier registry entry retire
  with it (the DANGER runbook entry becomes historical).
- **BL4 — re-arm tonight is Longhorn-only.** After the 00:00 UTC cap reset: restore
  `backupTargetURL`, confirm `available: true` with no 403. No kopia to restart, which
  halves the account's transaction consumers — the cap question resolves without raising
  it unless Longhorn alone proves too hungry (the 08-08 analysis says it doesn't:
  incrementals are cheap; full-upload days and storms were the spikes).

## Execution order

**BL1 EXECUTED 2026-08-10 13:45 UTC:** role built, Docker copy stopped (60 s save flush),
staged sudo copy seeded (5 files, identical digest), pod 1/1 with the DBoys world loaded,
TCP 7777 answering on the node IP. The public join port works again for the first time
since B5 — the router's forward finally lands on a listener.

1. BL1: build `roles/k8s/terraria`, seed (force, against the stopped Docker copy — brief
   game-server downtime), deploy, verify TCP 7777 from the LAN, retire the Docker entry.
2. BL2: kopia role dissolve commit (archive, crons, checks, count 18 → 17 — terraria's
   move makes it 17 → 16 in the same window).
3. Kuma: the eight retired backup-check monitors get deleted from the live Kuma (they are
   push monitors that will never beat again; `on_delete=keep` means label removal does
   nothing — deletion is explicit, via kuma-cli through an in-cluster one-shot).
4. Tonight/next session: BL4 re-arm; watch the first full Longhorn-only nightly, then the
   B2 transaction probe readout the morning after.

## Interim exposure, stated plainly

Between kopia's stop (already effective) and each item's own retirement: Docker-Authelia
TOTP, grafana.db and acme.json have NO backup. All are recoverable by re-enrollment,
re-render, or re-issue; none holds user data. Terraria's exposure ends the moment BL1's
seed verifies.
