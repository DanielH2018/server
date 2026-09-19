# pihole — LAN DNS resolver, running two instances

Pi-hole plus an unbound sidecar, deployed as two independent pods for deploy-time DNS
continuity. Coexisted with a Docker-era copy through the DNS cutover; that copy is retired.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "pihole"`
- **Images:** `pihole/pihole` (`pihole_k8s_image`), `klutchell/unbound`
  (`pihole_k8s_unbound_image`)
- **Route:** `pihole.<domain>` · `pihole.local.<domain>`, Authelia one_factor
- **Claims:** the claims a template loop declares (`{{ inst.claim }}`)
- **Auto-deploy:** denylisted (`k8s_autodeploy: false`) — platform — LAN DNS resolver; a failed
  deploy breaks name resolution fleet-wide, and host probes stay green through that kind of
  outage
<!-- /generated_from -->

- **The unbound sidecar** listens on `pihole_k8s_unbound_port` inside the pod; FTL owns `:53`
  in the shared network namespace.
- **The route is the web UI only** (port 80). LAN DNS itself is served on
  `pihole_k8s_lan_ip` (`dns_k8s_vip`), not through Traefik.
- **Persists:** two independent PVCs, `pihole_k8s_claim` / `pihole_k8s_claim_2`, one per pod
  — `/etc/pihole` is RWO/single-writer, so each instance gets its own rather than sharing.
  `longhorn-nobackup`: every byte is derivable — `gravity.db` from `pihole_k8s_adlists`,
  `pihole.toml` from the `FTLCONF` env, `pihole-FTL.db` is query history, not state.

## Notable
- **Blocklist state is declared, not volume-seeded.** `tasks/main.yml` reconciles
  `pihole_k8s_adlists`/`pihole_k8s_regex_deny` into `gravity.db` with idempotent
  INSERT/UPDATE, then rebuilds gravity only on a change — `pihole-FTL.db` moves on every DNS
  query, so a coexistence seed could never pass a quiescent-state verification.
- **Restarts are sequenced by hand, not the shared batch drain** — the two pods are restarted
  one at a time (`manifests_rollout: ''` plus explicit restart tasks) so a rollout never takes
  both Pi-holes down together, which is the whole reason a second instance exists.
- **LAN reverse-DNS is forwarded to the router, with `server=` rather than `rev-server=`.**
  `templates/config/pihole-dnsmasq.conf.j2` forwards the zone `lan_subnet` implies to
  `lan_router_ip`, so PTR lookups for DHCP clients return the names the router leased them.
  Pi-hole's own `rev-server=` is deliberately unused: it is a Pi-hole-only directive FTL's
  parser could reject at startup — on both instances at once, leaving no resolver to fetch a
  fix through — and it also forwards a whole TLD, which would hand the `.lan` names the
  `host-record=` lines answer for to the router. ENFORCED by
  `ansible/tests/services/test_pihole_lan_reverse_forwarding.py`.
- **`pihole_k8s_dns_cluster_ip` is a pinned ClusterIP**, immutable once bound — cluster DNS
  forwards there rather than to the LAN VIP, since the VIP is subject to
  `externalTrafficPolicy: Local`. Recreating the Service needs a new address chosen deliberately.

## Editing
Adlists/regex: `defaults/main.yml`. Deploy:
`uv run ansible-playbook ansible/deploy.yml --tags "pihole"`.
