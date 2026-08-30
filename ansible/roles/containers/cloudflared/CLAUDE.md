# cloudflared — the remote node's only inbound path

## At a glance
- **Image:** `cloudflare/cloudflared` (digest-pinned; multi-arch index, arm64 verified 2026-08-30)
- **Host:** `daniel-cloud` (Oracle A1, arm64)
- **Route:** none — it *is* the routing. No published ports, no Traefik labels.
- **Networks:** `proxy` · **Depends on:** nothing; it is the root of this host's ordering
- **Config:** `templates/config/config.yml.j2` (ingress map) and `credentials.json.j2` (the tunnel credential)
- **Secrets:** `cloudflared_tunnel_id`, `cloudflared_tunnel_credentials`

## Notable
- **This node opens no inbound port.** cloudflared dials out to Cloudflare; the security list
  allows SSH from the home WAN IP and nothing else. That is the property the whole auth
  decision was made for — do not "temporarily" open 80/443 to debug something.
- **The ingress map is generated from `containers_list`**, not hand-maintained. Every entry
  on the host that declares a `port` gets a rule, keyed on
  `{{ hostname | default(name) }}.{{ domain }}` and pointing at `http://{{ name }}:{{ port }}`.
  Adding a service to `host_vars/daniel-cloud.yml` therefore routes it with no second list to
  update. **cloudflared's own entry deliberately has no `port`** — one there would point the
  tunnel at itself.
- **Locally-managed tunnel, not `--token`.** A token-based tunnel pulls its routing from the
  Cloudflare dashboard, which puts the hostname-to-service map somewhere nobody diffs and no
  deploy reproduces. The cost of the local config is that the credentials JSON has to live in
  SOPS.
- **Why not Traefik here.** Traefik's `certresolver=cloudflare` uses the DNS-01 challenge,
  which needs a Cloudflare token that can *edit DNS records*. On a machine on someone else's
  hypervisor that is a credential whose compromise hijacks the zone. A tunnel credential is
  scoped to the tunnel and revocable without touching DNS. Dropping Traefik also removes ACME,
  `acme.json` and cert renewal as failure modes.
- **What moved to Cloudflare and is NOT in this repo:** WAF rate limiting (replacing
  `rate-limit@file`, and better placed — it sheds load at the edge rather than on a 2-OCPU
  box), a response-header transform rule (replacing `csp-karakeep@file`), and two Access
  policies: **none on `www`**, which is a deliberately public landing page, and a **bypass on
  Karakeep's `/api/*`**, without which the browser extension and mobile app are locked out
  while the web UI keeps working.
- The catch-all `http_status:404` rule at the end of the ingress list is mandatory —
  cloudflared refuses to start without one.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Ingress map: `templates/config/config.yml.j2` — but prefer changing `containers_list`, since
  the map is derived from it.
- Both config files are bind-mounted, so `tasks/main.yml` passes `common_config_changed`.
  Without it a newly added service would sit in `config.yml` with no route serving it.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "cloudflared" -e target=daniel-cloud`
