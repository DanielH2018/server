# littlelink — public link page (remote node)

## At a glance
- **Image:** `ghcr.io/techno-tim/littlelink-server` (digest-pinned; multi-arch index, arm64 verified 2026-08-30)
- **Host:** `daniel-cloud` (Oracle A1, arm64) — **not** the cluster
- **Route:** `www.<domain>` (the `hostname` key in `containers_list` overrides the role name)
- **Port:** 3000 · **Networks:** `proxy` · **Depends on:** traefik
- **Config:** none — everything is environment variables in the compose template
- **State:** none. Nothing to back up.

## Notable
- This is the **only public thing an outsider would notice missing** when the house is
  down, which is why it moved here. It sits outside whichever auth boundary the node
  settles on — a landing page that asks for a login is not a landing page.
- Cloudflare Pages would give it 100% uptime with no box at all. It lives here instead
  because the node exists anyway; Pages stays available as a later move.
- Read-only rootfs with a tmpfs `/tmp`. `docker diff` showed zero rootfs writes, so the
  tmpfs is defensive (Node's `os.tmpdir`) rather than required.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy (from daniel-box, once the node is in `hosts.ini`):
  `uv run ansible-playbook ansible/deploy.yml --tags "littlelink" -e target=daniel-cloud`
