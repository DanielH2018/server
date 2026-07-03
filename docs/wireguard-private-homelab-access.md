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
| Server / Pi-hole IP | `10.0.0.161` |
| WireGuard client subnet | `10.8.0.0/24` |
| Home LAN subnet | `10.0.0.0/24` |
| Service URLs | `https://<name>.local.daniel-hunter.com` — **all** resolve to `10.0.0.161` |
| `.local` auth portal | `https://auth.local.daniel-hunter.com` (one_factor) |
| New-client DNS default | `10.0.0.161` (server sets `WG_DEFAULT_DNS`) |

`.local` service names (each is `<name>.local.daniel-hunter.com`): `homepage`,
`jellyfin`, `sonarr`, `radarr`, `prowlarr`, `bazarr`, `tdarr`, `karakeep`, `freshrss`,
`qbittorrent`, `n8n`, `home-assistant`, `code-server`, `portainer`, `grafana`,
`prometheus`, `uptime-kuma`, `glances`, `scrutiny`, `healthchecks`, `pihole`, `peanut`,
`kopia`, `wg-easy`, `speedtest`, `bento-pdf`, `livesync`, `crowdsec`, `zigbee2mqtt`,
plus `auth` (the login portal, **required**) and `www` (littlelink).

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
AllowedIPs          = 10.0.0.161/32, 10.8.0.0/24   # was 0.0.0.0/0, ::/0
PersistentKeepalive = 25
```
The `AllowedIPs` narrowing is what makes it a split tunnel: only the server (Traefik +
Pi-hole) and WG peers route into this tunnel; everything else keeps Mullvad's default
route. Use `10.0.0.0/24` instead of `/32` if you also want to reach other LAN devices.

### A3. DNS — pick ONE (both keep general DNS on Mullvad → no leak)
- **Strict, any OS — hosts file.** Delete the `DNS =` line. Add to the OS hosts file
  (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts`), all → `10.0.0.161`:
  ```
  10.0.0.161 auth.local.daniel-hunter.com    # REQUIRED (login redirect)
  10.0.0.161 homepage.local.daniel-hunter.com
  10.0.0.161 jellyfin.local.daniel-hunter.com
  # ...add the services you actually use
  ```
  Hosts files don't support wildcards, so list each name you use. They never emit a DNS
  query, so general DNS stays entirely on Mullvad.
- **Linux convenience — split-DNS.** Keep `DNS = 10.0.0.161, local.daniel-hunter.com`.
  With `systemd-resolved` + `wg-quick`, the trailing domain makes Pi-hole authoritative
  **only** for `*.local.daniel-hunter.com`; every other lookup stays on Mullvad's resolver.

### A4. Import & run *(Claude/you)*
- Import the edited `.conf` into the standard **WireGuard** app (not the Mullvad app) as a
  separate tunnel.
- In the **Mullvad** app, enable **Settings → VPN settings → Local network sharing** — the
  kill switch otherwise blocks the personal tunnel from reaching `10.0.0.0/24`.
- Activate the WireGuard tunnel with Mullvad still connected.

### A5. Verify
- `wg show` lists both interfaces; the personal one shows a recent handshake.
- `curl -I https://homepage.local.daniel-hunter.com` → `200`/`302` (auth redirect), **not** `403`.
- `https://am.i.mullvad.net` still shows a **Mullvad** exit → general traffic untouched.
- Browse the dashboard hard → no 403 (CrowdSec isn't in this path).

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

## Notes
- You still authenticate everywhere: WireGuard keypair + Authelia one_factor on `.local`.
  Nothing is exposed unauthenticated.
- The Mullvad `/32` whitelist on the homelab WAF stays as a fallback for now. Once the
  private path is habitual it can be removed (a WAF improvement) — coordinate with the
  homelab operator before doing so.
- This adds a private path; it changes nothing about the public path's security.
