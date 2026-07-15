# traefik — Reverse proxy, TLS termination & CrowdSec WAF

Edge router for the whole homelab. See repo-root `CLAUDE.md` for shared conventions.
**This role bundles two containers:** `traefik` (version-pinned, Renovate-managed) and the
CrowdSec agent (`crowdsecurity/crowdsec:latest`, `watchtower.enable=false` — it health-gates traefik's
boot, so image updates are deliberate manual pulls: `deploy.yml --tags traefik -e common_pull=always`,
since a plain redeploy never re-pulls a tag already present locally). The separate `crowdsec` role is only the
Metabase dashboard.

## At a glance
- **Host:** daniel-server
- **Networks:** proxy · **Authelia:** N/A (provides the forward-auth entrypoint)
- **Depends on:** nothing — **everything else depends on this** (deployed first).
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- TLS via Cloudflare DNS-01; routes services at `<hostname>.<domain>` from their labels.
- CrowdSec bouncer/WAF: `crowdsec-acquis.yaml`, `crowdsec-profiles.yaml`,
  `crowdsec-whitelist.yaml`, Discord alerts, home-IP allowlist updater.
- **CrowdSec AppSec (inline L7 WAF, 2026-07-14):** the bouncer runs BOTH modes — `stream`
  (reactive ban-list: bans an IP after a pattern of log lines) AND the **AppSec Component**, which
  inspects each request INLINE before the backend, so a first-hit malicious payload (a CVE exploit
  string, SQLi/SSTI, path traversal) is blocked, not merely banned-after-the-fact. Config: the
  `appsec` acquisition source in `crowdsec-acquis.yaml.j2` (`crowdsec:7422`, `appsec_config:
  crowdsecurity/appsec-default`) + `crowdsecAppsecEnabled`/`crowdsecAppsecHost` on the bouncer
  middleware (`config.yml.j2`). Rulesets = `crowdsecurity/appsec-virtual-patching` (CVE signatures) +
  `crowdsecurity/appsec-generic-rules` (generic attack vectors), installed via the crowdsec
  `COLLECTIONS` env + re-asserted by the "Ensure CrowdSec dependency collections" deploy task —
  deliberately NOT the full OWASP CRS (`appsec-crs`), which is false-positive-prone. **Fails OPEN**
  (`crowdsecAppsecUnreachableBlock: false`): an appsec-broker/crowdsec hiccup keeps the edge serving
  (defers to the ban-list) rather than 500ing the fleet — the add-on must not become a new edge SPOF.
  **Because it fails open, a silently-broken appsec engine has no other signal** — so `appsec-verify.sh`
  (every 15 min, below) asserts the live agent has ≥1 enabled `cscli appsec-configs` + ≥1 inband
  `cscli appsec-rules` loaded and pages monitor-bridge's "CrowdSec AppSec" monitor on failure. Manual
  verify: `docker exec crowdsec cscli appsec-configs list` (non-empty) + `metrics` shows an `appsec`
  acquisition source.
- **Host crons (state-file → monitor-bridge):** `crowdsec-update-home-allowlist.sh` (every 5 min),
  `docker-user-verify.sh` (every 15 min), `appsec-verify.sh` (every 15 min, asserts the inline WAF is
  actually loaded — the fail-open blind spot), and `cloudflare-ip-drift.sh` (weekly) — the last diffs
  the hardcoded `cloudflare_ips` (`group_vars/all.yml`, which gates trustedIPs + the DOCKER-USER DROP)
  against Cloudflare's published ranges and pages the "Cloudflare IP Drift" monitor on a mismatch,
  since a stale list silently DROPs a client on a newly-added CF range at the edge.
- **Validating the origin-lock after a reboot: read the state file / Kuma, NOT `journalctl`/
  `systemctl` on `docker-user-seed.service`.** That seed oneshot logs nothing on success and is
  `RemainAfterExit=yes`, so any later deploy that re-renders the unit + `daemon_reload`s resets its
  tracked state without re-running it — leaving `journalctl -b -u docker-user-seed.service` blank and
  `systemctl show` `inactive`/`ConditionResult=no` even though it engaged correctly at boot (the
  frequent traefik-role deploy cadence guarantees this within a day of any boot). The DURABLE proof
  the chain is applied is `/var/lib/docker-user-rules/state.json` (world-readable, rewritten every
  15 min by `docker-user-verify.sh` — `ok:true` = the live `iptables DOCKER-USER` chain asserts the
  terminal :80/:443 DROP) and its **DOCKER-USER Origin Lock** Kuma monitor. `iptables -nvL
  DOCKER-USER` (needs root) is the ground truth if you have a shell.
- **Bouncer registration is rotation-safe (2026-07-03, exercised live):** the deploy probes
  LAPI with the configured `crowdsec_bouncer_api_key` and deletes + re-adds `traefik-bouncer`
  on mismatch (`cscli bouncers add` is create-only — without this, a rotated key leaves LAPI
  on the old hash while traefik hot-reloads the new one, and the plugin fails OPEN: silent
  WAF bypass). The auto-created `traefik-bouncer@<bridge-ip>` rows LAPI accumulates (~1 per
  traefik recreate, sharing the parent's key hash) **cannot be pruned individually** — cscli
  refuses, "delete parent instead" — so accumulation between rotations is cosmetic and
  accepted; the rotation-path parent delete cascades the whole set. Rotation runbook:
  `docs/secret-rotation.md` (`assisted`). **Not an anomaly: a `traefik-bouncer@::1` row with
  `Type: Wget` is the deploy-time rotation-guard probe** (`tasks/main.yml` "Probe LAPI with the
  configured bouncer key" `wget`s `localhost:8080/v1/decisions` from *inside* the crowdsec
  container). It looks exactly like a bouncer-key leak used from within the container, so a future
  `cscli bouncers list` audit shouldn't chase it as a compromise indicator — it shares the same
  known key and is cascaded away by the same parent delete.
- Ships **systemd units** (`traefik-init.service`, `docker-user-rules.service`) and
  logrotate — this role does more than run a container.
- The `labels()` macro imported by every other service's compose lives in the repo-level
  shared templates (`ansible/templates/traefik.yml.j2`) — NOT this role's
  `templates/traefik.yml.j2`, which is Traefik's *static config* (entrypoints, providers).
- **Wildcard default cert covers hand-rolled / path-bypass routers — don't re-flag a missing
  `certresolver` on them (review Network-L2, false positive).** `config.yml.j2`'s default TLS store
  sets a `defaultGeneratedCert` via the cloudflare resolver with SANs `*.<domain>` + `*.local.<domain>`.
  The secondary routers some services hand-roll for a path (`n8n-webhook`, `healthchecks-ping`,
  `karakeep-api`) carry `tls=true` with NO `certresolver` — that's fine: TLS cert selection is by
  **SNI (hostname), before path routing**, so they serve the same valid LE wildcard the co-hosted
  main router already provisions (verified: all three serve `CN=daniel-hunter.com`, issuer Let's
  Encrypt). Adding `certresolver` to them would only trigger redundant per-host ACME requests for
  zero gain. A NEW hand-rolled router on a NEW host not under the wildcard would still need one.

- **Dynamic config (`config.yml.j2`) is bind-mounted via its PARENT DIRECTORY
  (`./data/dynamic:/dynamic:ro`, `providers.file.directory: /dynamic`), not as a single
  file.** Ansible's `template` module writes via tmp+rename, so a re-render swaps in a
  new inode; a single-file bind mount (the old `./data/config.yml:/config.yml:ro` +
  `filename:`) stays pinned to the OLD inode, so Traefik's file-provider `watch: true`
  never fires and even a full re-render is invisible until the container is recreated.
  A directory mount follows directory entries, so renames within it are visible and
  watch actually works — config.yml edits now apply live, no recreate needed (unlike
  `traefik.yml`, still read only at boot, still in the `common_config_changed` OR).

## Editing
- Compose: `templates/docker-compose.yml.j2` · Static cfg: `templates/traefik.yml.j2` · Dynamic cfg: `templates/config.yml.j2` (renders to `data/dynamic/config.yml`)
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "traefik"`
