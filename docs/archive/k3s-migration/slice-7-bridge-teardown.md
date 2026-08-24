# Slice-7 — Forward-bridge teardown (the slice-6 close-out's last act)

Planned 2026-08-10, post-flip (router 80/443 → VIP ~04:00 UTC, LTE-verified, ~1 day soak
green). Decisions BT1–BT5. The gate text ("delete `bridge_hostname` keys") predates a
coupling that grew after it was written: since B5, the macro's **native unsuffixed public
route — the one serving all internet traffic since the flip — renders inside
`{% if bridge_hostname %}`**. Deleting the keys naively would 404 every migrated service's
public name. So the teardown is a refactor of what the key MEANS, not a deletion.

## What the survey established

- Docker edge public traffic flatlined to a LAN-only baseline ~04:00 UTC 2026-08-10; the
  cluster's reverse-bridge router carries the Docker-hosted names. Soak: green.
- Three consumers of `bridge_hostname` (29 keys in daniel-box.yml):
  1. the Docker traefik forward bridge (`config.yml.j2` generated block) — DEAD since the
     flip for public traffic, but still carries LAN + host-shell traffic for unsuffixed
     names (wildcards still answer 10.0.0.161);
  2. the macro's ClientIP-gated bridge route (dead once the forward bridge goes);
  3. the macro's NATIVE unsuffixed public route (LIVE — the production path).
- Two wildcards still point at 10.0.0.161: the LAN dnsmasq `*.local.<domain>` (slice-6 D7
  deferred it here) and the hand-created Cloudflare grey-cloud `*.local.<domain>` record
  (host shells and any 1.1.1.1-fallback resolution use it).
- probe.py's `ha_host()` and every "unsuffixed name survives cutovers" assumption ride
  path (1) today and must ride the cluster edge after.

## Decisions

### BT1 — the key is renamed to what it now means: `unsuffixed_hostname`
The datum ("this service also answers on its pre-migration name") outlives the bridge.
The macro's native public route and a NEW unsuffixed `.local` route key off it; the two
bridge-specific routes (ClientIP-gated legs) are deleted. The `-k8s` names stay primary
in `hostname:` — every internal consumer (prometheus jobs, Kuma entities, probe pins,
monitor URLs) keeps resolving; draining the `-k8s` suffix is its own later phase, not
this one.

### BT2 — the unsuffixed `.local` names get cluster routes WITH Authelia
New in the macro: `Host(<unsuffixed>.local.<domain>)` joins the main route (Authelia per
the entry, same as the `-k8s` name). Previously absent because the LAN wildcard resolved
to daniel-server; after BT3 those names arrive at the cluster edge and must be gated by
the k8s portal — which already owns the public cookie domain, so no double-login.

### BT3 — both wildcards flip to the VIP, LAN first
LAN: `pihole_wildcard_ip` → `k3s_metallb_ingress_vip` in the shared dnsmasq template's
default and the k8s pihole ConfigMap's override; both Pi-holes redeploy. Docker-hosted
names ride the reverse bridge (the path public traffic has proven all day); k8s names
route natively. Cloudflare grey-cloud `*.local.<domain>` → 10.0.0.240 is the operator's
(hand-created record, hand-moved) — until it moves, host-shell/public-DNS resolution
still lands on the Docker edge, which is why BT4 waits for it.

### BT4 — the forward bridge is removed only after BT3 verifies
Order inside the change: wildcard flips verified (a Docker-hosted and a k8s name both
load from LAN via the VIP) → macro refactor + key rename lands and every k8s role with a
route redeploys → the Docker traefik forward-bridge block is deleted and traefik
redeploys. Rollback at any step is the reverse edit — the Docker edge stays fully
assembled until the last commit, same as the flip's own rollback posture.

### BT5 — test_strangler_bridge.py reworks to the end-state invariant
Slice-2 anticipated it ("slice 6 removes every bridge at once"): every public name is a
cluster route (native) or a reverse-bridge route; no `bridge_hostname` remains anywhere;
every `unsuffixed_hostname` has both a public and a `.local` cluster route; the Docker
traefik config renders no bridge block.

## Execution order

1. BT3-LAN: wildcard → VIP in both templates; deploy both Pi-holes; verify resolution +
   access for one Docker-hosted (`grafana.local`) and one k8s (`jellyfin.local`) name.
2. Operator: move the Cloudflare grey-cloud wildcard record → 10.0.0.240.
3. BT1+BT2: macro refactor, 29 key renames, k8s role redeploys; verify a public
   unsuffixed name and an unsuffixed `.local` name both serve from the cluster with
   Authelia intact (LTE spot-check for the public one).
4. BT4: delete the Docker forward-bridge block; redeploy Docker traefik; verify probe.py
   `ha` and monitor-bridge stay green (their paths now terminate at the cluster edge).
5. BT5: tests + docs; slice-6 close-out gate marked CLEARED (B2 item pending tonight).

## Execution record — 2026-08-10

Executed in plan order; all on daniel-box unless noted.

1. **BT3-LAN** ✓ — wildcard → VIP in `pihole-dnsmasq.conf.j2` (default) and the k8s
   pihole ConfigMap override; both Pi-holes deployed. `dig @10.0.0.243 sonarr.local.…`
   → 10.0.0.240; `grafana.local` (Docker-hosted, reverse bridge) → 302 to
   `auth.local.…`; `sonarr.local` (k8s native) → 302 to the k8s portal.
2. **Cloudflare wildcard** ✓ — operator moved grey-cloud `*.local.<domain>` → 10.0.0.240
   (2026-08-10 ~19:40 UTC). Verified via 1.1.1.1 and the host shell; a Docker-hosted
   name (reverse bridge) and a k8s name (native) both serve from the VIP. During the
   gap, host-shell resolution 404'd at the stripped Docker edge — probe.py's HA calls
   were the casualty and now pin the VIP (`dcad9bc1`).
3. **BT1+BT2** ✓ — macro refactor + `bridge_hostname` → `unsuffixed_hostname` rename
   (inventory + 20 route templates + 4 deployment templates + reverse-bridge + Docker
   config); full daniel-box deploy ok=834 changed=155 failed=0 (first run caught a
   missed rename in `reverse-bridge.yaml.j2`).
   **Trap found live: deleting the ClientIP-gated route looped livesync into a 429.**
   The macro's bridge route wasn't only the forward bridge's terminus — for a
   `public_native=false` service it is the *reverse* path's terminus too: the Docker
   edge's custom routers forward to the VIP expecting it. Restored as an
   `{% elif unsuffixed_hostname %}` return-leg branch (ClientIP-gated, carries
   `bridge_rate_limit` so livesync keeps its high-ceiling limiter). After the fix:
   public tokenless `/` → 404 (edge token gate), `.local` `/` → CouchDB 401 (the
   `bridge_probe_path: /` probe router, by design), `/_utils` → 302 Authelia, live
   replication 200/201s throughout.
   NB when verifying from daniel-box: its curls never match `ClientIP(10.0.0.161/32)`,
   so they exercise the reverse bridge + Docker edge like any LAN client — the
   return-leg can only be exercised by real bridge traffic from daniel-server.
4. **BT4** ✓ — Docker `config.yml.j2`: `bridged` narrowed to `bridge_custom_routers`
   owners (livesync); generic forward-bridge routers/services gone at next render.
   Deployed to daniel-server together with its Pi-hole wildcard flip (one-shot play).
5. **BT5** ✓ — `test_strangler_bridge.py` rewritten to the end-state invariants
   (no `bridge_hostname` anywhere; Docker edge renders no generic bridge routers;
   livesync residual chain intact; reverse bridge serves both name forms per host;
   every `unsuffixed_hostname` belongs to a routed service).
