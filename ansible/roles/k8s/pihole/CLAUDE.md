# pihole — LAN DNS resolver, running two instances

Pi-hole plus an unbound sidecar, deployed as two independent pods for deploy-time DNS
continuity. Coexisted with a Docker-era copy through the DNS cutover; that copy is retired.

## At a glance
- **Images:** `pihole_k8s_image`, sidecar `pihole_k8s_unbound_image` — unbound listens on
  `pihole_k8s_unbound_port` inside the pod, FTL owns `:53` in the shared network namespace.
- **Route:** `pihole.<domain>` · Authelia · port 80 (web UI). LAN DNS itself is served on
  `pihole_k8s_lan_ip` (`dns_k8s_vip`), not through Traefik.
- **Persists:** two independent PVCs, `pihole_k8s_claim` / `pihole_k8s_claim_2`, one per pod
  — `/etc/pihole` is RWO/single-writer, so each instance gets its own rather than sharing.
  `longhorn-nobackup`: every byte is derivable — `gravity.db` from `pihole_k8s_adlists`,
  `pihole.toml` from the `FTLCONF` env, `pihole-FTL.db` is query history, not state.
- **Deploy tag:** `--tags "pihole"`. Denylisted from auto-deploy — platform: a failed deploy
  breaks name resolution fleet-wide, and host probes stay green straight through that outage.

## Notable
- **Blocklist state is declared, not volume-seeded.** `tasks/main.yml` reconciles
  `pihole_k8s_adlists`/`pihole_k8s_regex_deny` into `gravity.db` with idempotent
  INSERT/UPDATE, then rebuilds gravity only on a change — `pihole-FTL.db` moves on every DNS
  query, so a coexistence seed could never pass a quiescent-state verification.
- **Restarts are sequenced by hand, not the shared batch drain** — the two pods are restarted
  one at a time (`manifests_rollout: ''` plus explicit restart tasks) so a rollout never takes
  both Pi-holes down together, which is the whole reason a second instance exists.
- **`pihole_k8s_dns_cluster_ip` is a pinned ClusterIP**, immutable once bound — cluster DNS
  forwards there rather than to the LAN VIP, since the VIP is subject to
  `externalTrafficPolicy: Local`. Recreating the Service needs a new address chosen deliberately.

## Editing
Adlists/regex: `defaults/main.yml`. Deploy:
`uv run ansible-playbook ansible/deploy.yml --tags "pihole"`.
