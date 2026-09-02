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

<!-- Generated from group_vars and the two hosts' containers_list; edit those. -->
--8<-- "assets/generated/fragments/lan-addresses.md"

| Thing | Value |
|---|---|
| WireGuard endpoint | `wireguard.daniel-hunter.com` on the daniel-box UDP port above → wg-easy on `daniel-box` (k8s) |
| wg-easy admin UI | `https://wg-easy.daniel-hunter.com` (behind Authelia) |
| WireGuard client subnet | `10.8.0.0/24` |
| Home LAN subnet | `10.0.0.0/24` |
| Service URLs | `https://<name>.local.daniel-hunter.com` → the k3s ingress VIP above |
| `.local` auth portal | `https://auth.local.daniel-hunter.com` (one_factor) |
| New-client DNS default | `10.0.0.243` (`default_dns: {{ dns_k8s_vip }}` on wg-easy's `containers_list` entry) |

### One ingress IP (since the migration completed, 2026-08-14)

This doc previously described a **two-IP split** — Docker services on `10.0.0.161` and
migrated ones on the k3s VIP. **That split is gone.** The k3s migration completed on
2026-08-14 and Docker was uninstalled from `daniel-server`, so there is no Docker edge on
`10.0.0.161` to reach. Pi-hole's `local.<domain>` wildcard now answers the cluster ingress
VIP for everything (`pihole_wildcard_ip = k3s_metallb_ingress_vip` in
`ansible/roles/k8s/pihole/templates/configmap.yaml.j2`).

Practical consequences:

- **Let Pi-hole resolve** and you need no per-service mapping at all — one wildcard covers
  every `.local` name.
- The hosts-file option in A3 and the `AllowedIPs` narrowing in A2 (the two places that name
  IPs by hand) now need **`10.0.0.240`** — plus `10.0.0.243` if you point at Pi-hole.
- The transitional **`-k8s` hostname suffixes are retired**; services answer on their plain
  names again. If a `<name>-k8s.local.…` URL is saved in a bookmark, drop the suffix.
- `daniel-pi` runs a second, **LAN-only** wg-easy on `51822/udp`. It is deliberately not
  forwarded and is not the endpoint above — don't point a remote client at it.

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
the right default **when away from home** — it covers the ingress VIP `10.0.0.240`, the DNS
VIP `10.0.0.243`, and anything that moves later, and costs no privacy since
the range is entirely private. `AllowedIPs` decides what enters the tunnel, so an IP
missing here leaks out via Mullvad's default route and fails with a **timeout**, not a DNS
error.

> **Do not use `10.0.0.0/24` on a client that is itself on the home LAN.** `wg-quick`
> installs that route at metric 0, which beats the connected route on the physical NIC
> (metric ~100). Every LAN destination — including the gateway `10.0.0.1`, and `10.0.0.240`
> which was directly reachable to begin with — then hairpins out to the WireGuard endpoint
> and back. On a LAN client, either bring the tunnel down (nothing needs it; the VIPs are
> directly reachable) or list single hosts: `AllowedIPs = 10.0.0.240/32, 10.0.0.243/32,
> 10.8.0.0/24`.

### A3. DNS — pick ONE (both keep general DNS on Mullvad → no leak)
- **Strict, any OS — hosts file.** Delete the `DNS =` line. Add to the OS hosts file
  (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts`). Every service is on the one
  ingress VIP now:
  ```
  10.0.0.240 auth.local.daniel-hunter.com    # REQUIRED (login redirect)
  10.0.0.240 homepage.local.daniel-hunter.com
  10.0.0.240 jellyfin.local.daniel-hunter.com
  # ...add the services you actually use
  ```
  Hosts files don't support wildcards, so list each name you use. They never emit a DNS
  query, so general DNS stays entirely on Mullvad — but that is also the trap: a name you
  **forget** to list emits no local query either. It falls straight through to Mullvad's
  resolver, which won't return a `10.0.0.x` answer, and the browser reports **"Server not
  found"**. A new or newly migrated service reaching you as a DNS failure is this, not an
  outage — check the file before you debug the homelab.
- **Linux convenience — split-DNS *(recommended)*.** Keep
  `DNS = 10.0.0.243, local.daniel-hunter.com`. With `systemd-resolved` + `wg-quick`, the
  trailing domain makes Pi-hole authoritative **only** for `*.local.daniel-hunter.com`;
  every other lookup stays on Mullvad's resolver. Pi-hole answers the whole
  `local.<domain>` space from one wildcard, so any new service just works — no client-side
  edit, ever. Prefer this over the hosts file unless your OS can't do split-DNS.

### A4. Import & run *(Claude/you)*
- Import the edited `.conf` into the standard **WireGuard** app (not the Mullvad app) as a
  separate tunnel.
- In the **Mullvad** app, enable **Settings → VPN settings → Local network sharing** — the
  kill switch otherwise blocks the personal tunnel from reaching `10.0.0.0/24`.
- Activate the WireGuard tunnel with Mullvad still connected.

### A5. Verify
- `wg show` lists both interfaces; the personal one shows a recent handshake.
- `curl -I https://homepage.local.daniel-hunter.com` → `200`/`302` (auth redirect), **not** `403`.
- `curl -I https://auth.local.daniel-hunter.com` → `200`. This passes only if `10.0.0.240`
  both resolves and routes, so it catches a stale hosts file or an `AllowedIPs` that still
  names the retired `10.0.0.161` edge.
- `https://am.i.mullvad.net` still shows a **Mullvad** exit → general traffic untouched.
- Browse the dashboard hard → no 403 (CrowdSec isn't in this path).

Distinguishing the two failure modes: **"Server not found"** is DNS (hosts file / split-DNS
— A3), a **timeout** is routing (`AllowedIPs` — A2). `dig +short <name> @10.0.0.243` from
the client separates them: an answer means Pi-hole is fine and the problem is on your end.

### A6. If Mullvad's firewall blocks it
Some Mullvad builds drop secondary-tunnel traffic even with local sharing on. If `.local`
is unreachable while Mullvad is up: re-check "Local network sharing"; test with lockdown
mode off; otherwise fall back to the toggle approach (Part B) on desktop too.

**Mullvad rejects port 53 to every resolver but its own, and enabling "Local network
sharing" does not change that.** The LAN-sharing rule exists, but it is ordered *below* the
DNS reject, and nftables stops at the first terminal match. From a real ruleset
(`sudo nft list ruleset`), in evaluation order:

```
oif "wg0-mullvad" udp dport 53 ip daddr 10.64.0.1 accept   # Mullvad's own resolver only
oif "wg0-mullvad" tcp dport 53 ip daddr 10.64.0.1 accept
udp dport 53 reject                                        # everything else
tcp dport 53 reject with tcp reset                         # -> "connection refused"
oif "wg0-mullvad" accept
ip daddr 10.0.0.0/8 accept                                 # local network sharing, too late
```

So LAN **HTTP** to `10.0.0.240:80` falls through to the `10.0.0.0/8` accept and works, while
LAN **DNS** to `10.0.0.243:53` is rejected two rules earlier and never reaches it. That
asymmetry is the whole confusion: the resolver looks dead while every other port on the same
host answers. `reject with tcp reset` is what produces `connection refused` rather than a
timeout.

The diagnostic that identifies it in one step is **to probe `:53` on a host the homelab
doesn't control** — the ISP gateway:

```bash
dig +short +time=3 @10.0.0.1 example.com     # refused too? → it's local, not the homelab
dig +short +time=3 @10.0.0.243 jellyfin.local.daniel-hunter.com
```

If *both* refuse, nothing server-side is wrong: no homelab config can make `10.0.0.1`
refuse. `connection refused` (rather than a timeout) confirms it further — that RST is
generated locally, by the client's own packet filter. Check with
`sudo nft list ruleset | grep -B2 -A2 'dport 53'`, and confirm by disconnecting Mullvad and
re-running the `dig`.

This is worth internalising: Pi-hole is a single-host service, so a `:53` failure that
reproduces against *multiple* IPs was never Pi-hole. Diagnose the client first.

Once identified, pick one:

1. **Toggle Mullvad off** while using the homelab. Everything resolves via Pi-hole with no
   config, migrated services included. Simplest, and the desktop equivalent of Part B.
2. **Hosts file** for the few names you always need. Works with Mullvad *up*, because a
   hosts entry emits no DNS query and so never meets the `:53` block. Costs a line per
   service — see A3.
3. **Mullvad → custom DNS = `10.0.0.243`.** Works with Mullvad up, but **avoid it**: all
   DNS then exits via the LAN to unbound, which recurses from the home IP while HTTP still
   exits via Mullvad. That correlation is exactly what A3's split-DNS design prevents.

**On the home LAN, don't run the tunnel at all.** The VIPs are directly reachable there, so `wg0` adds nothing and brings the A2 hairpin hazard with it.
Part A is for remote access.

---

## Part B — Mobile: one VPN at a time → toggle

iOS/Android permit only one active VPN tunnel, so Mullvad and the personal WG can't run
together.
1. In `wg-easy.daniel-hunter.com`, create a `phone` client and scan its QR into the
   WireGuard app. It already carries `DNS = 10.0.0.243` and full-tunnel
   `AllowedIPs = 0.0.0.0/0` — **leave as-is**.
2. Keep Mullvad on day-to-day. When you need the homelab, turn Mullvad **off** and the
   WireGuard (homelab) tunnel **on**; `.local` names resolve via Pi-hole and load.
3. While the homelab tunnel is on, that session egresses via home (not Mullvad) — the
   accepted per-platform trade-off. Switch back to Mullvad when done.

---

## Server-side networking (already handled — don't undo)
wg-easy is a **k8s workload on `daniel-box`** (`roles/k8s/wg-easy`, entry in
`host_vars/daniel-box.yml`), reached on `51820/udp` — the router forward was moved to this
node with the rest of the edge.

The old **Docker-bridge hairpin hazard is gone.** It applied when wg-easy was a container on
`daniel-server` sharing a bridge with the services behind host-published ports: the reply
returned across the bridge, bypassed the host's reverse-NAT, and conntrack dropped it. That
was why wg-easy was pinned to the `monitoring` network. Post-migration, a WG client's traffic
goes to the MetalLB VIPs and is routed by the cluster, so there is no bridge to hairpin over.

Practical upshot: **a service timing out over WG is now almost always `AllowedIPs` (A2)**,
not a bridge-placement problem. Don't go looking for the old `monitoring`-network rule — the
Pi's LAN-only wg-easy is the only Docker copy left, and it isn't on this path.

## Notes
- You still authenticate everywhere: WireGuard keypair + Authelia one_factor on `.local`.
  Nothing is exposed unauthenticated.
- The Mullvad `/32` whitelist on the homelab WAF stays as a fallback. Once the
  private path is habitual it can be removed (a WAF improvement) — coordinate with the
  homelab operator before doing so.
- This adds a private path; it changes nothing about the public path's security.
