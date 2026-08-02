# Private homelab access over WireGuard (alongside Mullvad)

Reach the homelab without tripping its CrowdSec WAF, while keeping Mullvad as the
everyday VPN. Route **only** homelab-bound traffic through the personal WireGuard
tunnel (wg-easy) and access services via their internal `.local` names — never via
the public Cloudflare path. No privacy/security compromise.

## Why this works

- **Public path** = Mullvad exit → Cloudflare → Traefik → CrowdSec. Traefik trusts
  Cloudflare's `X-Forwarded-For`, so CrowdSec judges your (rotating, shared-datacenter)
  Mullvad exit IP and auto-bans burst browsing → 403s.
- **Private path** = personal WireGuard → `<svc>.local.daniel-hunter.com` → Traefik
  directly. Bypasses Cloudflare **and** CrowdSec, arrives from a stable private IP
  (which CrowdSec whitelists by default), and is still authenticated (WireGuard keys +
  Authelia one_factor on `.local`).

The personal tunnel's handshake to the public endpoint still rides **over** Mullvad on
desktop, so your real ISP IP is never exposed to anything.

## Homelab-side facts (already configured — do not change)

| Thing | Value |
|---|---|
| WireGuard endpoint | `wireguard.daniel-hunter.com:51820/udp` |
| wg-easy admin UI | `https://wg-easy.daniel-hunter.com` (behind Authelia) |
| Server / Pi-hole IP | `10.0.0.161` — Docker-hosted services |
| k3s ingress VIP | `10.0.0.240` — MetalLB, k3s-hosted services |
| WireGuard client subnet | `10.8.0.0/24` |
| Home LAN subnet | `10.0.0.0/24` |
| Service URLs | `https://<name>.local.daniel-hunter.com` — resolve to **either** IP above, see below |
| `.local` auth portal | `https://auth.local.daniel-hunter.com` (one_factor) |
| New-client DNS default | `10.0.0.161` (server sets `WG_DEFAULT_DNS`) |

### Two IPs, not one

`.local` names no longer all point at `10.0.0.161`. Services migrated to the k3s cluster
answer on the MetalLB ingress VIP `10.0.0.240` instead — a different host entirely
(`daniel-box`), running its own Traefik. Pi-hole serves both correctly (a per-name
`address=` override beats the `local.<domain>` wildcard), so **the split is invisible if
you let Pi-hole resolve.** It only bites the hosts-file option in A3, and the
`AllowedIPs` narrowing in A2 — both of which name IPs by hand.

Migrating a service changes its IP, so don't hardcode this mapping anywhere you'd have
to remember to update. The authoritative list is generated, not hand-kept: k3s names come
from `daniel-box`'s `containers_list` (see `filter_by_platform('k8s')` in
`ansible/roles/containers/pihole/templates/dnsmasq.yml.j2`).

**Docker (`10.0.0.161`)** — each is `<name>.local.daniel-hunter.com`: `homepage`,
`jellyfin`, `sonarr`, `radarr`, `prowlarr`, `bazarr`, `tdarr`, `karakeep`, `freshrss`,
`qbittorrent`, `n8n`, `home-assistant`, `code-server`, `portainer`, `grafana`,
`prometheus`, `uptime-kuma`, `glances`, `scrutiny`, `healthchecks`, `pihole`, `peanut`,
`kopia`, `wg-easy`, `speedtest`, `bento-pdf`, `livesync`, `crowdsec`, `traefik`, `zigbee2mqtt`,
plus `auth` (the login portal, **required**) and `www` (littlelink).

**k3s (`10.0.0.240`)** — migrated services carry a `-k8s` suffix so both copies can run
side by side during the migration: `auth-k8s` (the cluster's own login portal) and
`bento-pdf-k8s`. Slice 2 moves the bulk of the list above onto this IP.

---

## Part A — Desktop: Mullvad + personal WG at the same time (split tunnel)

### A1. Get a client config *(you, in a browser)*
1. While Mullvad is up, open `https://wg-easy.daniel-hunter.com` and log in.
2. Create a client named `desktop-split` and **download its `.conf`**.
3. Hand the `.conf` to your local Claude for editing.

### A2. Edit the `.conf` into a split tunnel *(Claude)*
Change **only** `AllowedIPs` (and the DNS line per A3); leave keys/Address untouched:
```ini
[Interface]
PrivateKey = <unchanged>
Address    = 10.8.0.x/24            # as issued
# DNS line — see A3

[Peer]
PublicKey           = <unchanged>
PresharedKey        = <unchanged>
Endpoint            = wireguard.daniel-hunter.com:51820
AllowedIPs          = 10.0.0.0/24, 10.8.0.0/24     # was 0.0.0.0/0, ::/0
PersistentKeepalive = 25
```
The `AllowedIPs` narrowing is what makes it a split tunnel: only the home LAN and WG peers
route into this tunnel; everything else keeps Mullvad's default route. The whole `/24` is
the right default now that services live on two IPs (`10.0.0.161` and the k3s VIP
`10.0.0.240`) — it also survives the next migration, and costs no privacy since the range
is entirely private. Listing single hosts (`10.0.0.161/32, 10.0.0.240/32`) works too, but a
service that later moves to a third IP then fails with a **timeout**, not a DNS error:
`AllowedIPs` decides what enters the tunnel, so an IP missing here leaks out via Mullvad's
default route and never reaches home.

### A3. DNS — pick ONE (both keep general DNS on Mullvad → no leak)
- **Strict, any OS — hosts file.** Delete the `DNS =` line. Add to the OS hosts file
  (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts`). **Mind the two IPs** —
  Docker services take `10.0.0.161`, k3s services take `10.0.0.240`:
  ```
  10.0.0.161 auth.local.daniel-hunter.com    # REQUIRED (login redirect)
  10.0.0.161 homepage.local.daniel-hunter.com
  10.0.0.161 jellyfin.local.daniel-hunter.com
  # ...add the Docker services you actually use

  10.0.0.240 auth-k8s.local.daniel-hunter.com      # k3s login portal
  10.0.0.240 bento-pdf-k8s.local.daniel-hunter.com
  ```
  Hosts files don't support wildcards, so list each name you use. They never emit a DNS
  query, so general DNS stays entirely on Mullvad — but that is also the trap: a name you
  **forget** to list emits no local query either. It falls straight through to Mullvad's
  resolver, which won't return a `10.0.0.x` answer, and the browser reports **"Server not
  found"**. A new or newly-migrated service reaching you as a DNS failure is this, not an
  outage — check the file before you debug the homelab.
- **Linux convenience — split-DNS *(recommended)*.** Keep
  `DNS = 10.0.0.161, local.daniel-hunter.com`. With `systemd-resolved` + `wg-quick`, the
  trailing domain makes Pi-hole authoritative **only** for `*.local.daniel-hunter.com`;
  every other lookup stays on Mullvad's resolver. Pi-hole already knows both IPs and every
  name, so new and migrated services just work — no client-side edit, ever. Prefer this
  over the hosts file unless your OS can't do split-DNS; slice 2 moves ~30 more services
  onto the k3s VIP, and each one is a hosts-file line you'd otherwise have to fix by hand.

### A4. Import & run *(Claude/you)*
- Import the edited `.conf` into the standard **WireGuard** app (not the Mullvad app) as a
  separate tunnel.
- In the **Mullvad** app, enable **Settings → VPN settings → Local network sharing** — the
  kill switch otherwise blocks the personal tunnel from reaching `10.0.0.0/24`.
- Activate the WireGuard tunnel with Mullvad still connected.

### A5. Verify
- `wg show` lists both interfaces; the personal one shows a recent handshake.
- `curl -I https://homepage.local.daniel-hunter.com` → `200`/`302` (auth redirect), **not** `403`.
- `curl -I https://auth-k8s.local.daniel-hunter.com` → `200`. Checks the *other* IP: this
  one only passes if `10.0.0.240` both resolves and routes, so it catches a stale hosts
  file or an `AllowedIPs` that still names `10.0.0.161` alone.
- `https://am.i.mullvad.net` still shows a **Mullvad** exit → general traffic untouched.
- Browse the dashboard hard → no 403 (CrowdSec isn't in this path).

Distinguishing the two failure modes: **"Server not found"** is DNS (hosts file / split-DNS
— A3), a **timeout** is routing (`AllowedIPs` — A2). `dig +short <name> @10.0.0.161` from
the client separates them: an answer means Pi-hole is fine and the problem is on your end.

### A6. If Mullvad's firewall blocks it
Some Mullvad builds drop secondary-tunnel traffic even with local sharing on. If `.local`
is unreachable while Mullvad is up: re-check "Local network sharing"; test with lockdown
mode off; otherwise fall back to the toggle approach (Part B) on desktop too.

---

## Part B — Mobile: one VPN at a time → toggle

iOS/Android permit only one active VPN tunnel, so Mullvad and the personal WG can't run
together.
1. In `wg-easy.daniel-hunter.com`, create a `phone` client and scan its QR into the
   WireGuard app. It already carries `DNS = 10.0.0.161` and full-tunnel
   `AllowedIPs = 0.0.0.0/0` — **leave as-is**.
2. Keep Mullvad on day-to-day. When you need the homelab, turn Mullvad **off** and the
   WireGuard (homelab) tunnel **on**; `.local` names resolve via Pi-hole and load.
3. While the homelab tunnel is on, that session egresses via home (not Mullvad) — the
   accepted per-platform trade-off. Switch back to Mullvad when done.

---

## Server-side networking (already handled — don't undo)
wg-easy runs on the **`monitoring`** Docker network, deliberately **not** `apps` or `proxy`.
It must not share a bridge with the containers behind the host-published ports WG clients
reach — Traefik 80/443 → `apps`, Pi-hole 53 → `apps`, Portainer 9000 → `proxy`,
Jellyfin DLNA → `media`. A WG client hitting `10.0.0.161:<port>` is DNAT'd to that
container; if it's on the **same** bridge as wg-easy, the reply returns straight across the
bridge, bypasses the host's reverse-NAT, and conntrack drops it → the client times out
(looks exactly like a firewall block, but it isn't — the 80/443 allow-list in
`docker-user-rules.sh.j2` already permits `10.0.0.0/8` + `172.16.0.0/12`). `monitoring` is
cross-bridge from all of those, so every host-published service returns symmetrically.
**If wg-easy is ever moved back onto `apps`/`proxy`, `.local` and Portainer access over WG
breaks.** (Set in `host_vars/daniel-server.yml`.)

This whole hairpin concern is specific to `10.0.0.161`. Traffic to the k3s VIP
(`10.0.0.240`) leaves the server for `daniel-box` over the LAN, so no Docker bridge is
involved and there's nothing to hairpin — a k3s service timing out over WG is `AllowedIPs`
(A2), not this.

## Notes
- You still authenticate everywhere: WireGuard keypair + Authelia one_factor on `.local`.
  Nothing is exposed unauthenticated.
- The Mullvad `/32` whitelist on the homelab WAF stays as a fallback for now. Once the
  private path is habitual it can be removed (a WAF improvement) — coordinate with the
  homelab operator before doing so.
- This adds a private path; it changes nothing about the public path's security.
