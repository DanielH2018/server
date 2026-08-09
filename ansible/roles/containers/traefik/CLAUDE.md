# traefik — Reverse proxy, TLS termination & CrowdSec WAF

Edge router for the whole homelab. See repo-root `CLAUDE.md` for shared conventions.
**This role bundles two containers:** `traefik` (version-pinned, Renovate-managed) and the
CrowdSec agent (`crowdsecurity/crowdsec:latest`, `watchtower.enable=false` — it health-gates traefik's
boot, so image updates are deliberate manual pulls: `deploy.yml --tags traefik -e common_pull=always`,
since a plain redeploy never re-pulls a tag already present locally).

**Since slice-6 B2 (2026-08-09) this container is an AGENT, not an engine.** `DISABLE_LOCAL_API`
+ `LOCAL_API_URL` point it at the single LAPI in the k3s cluster (`roles/k8s/crowdsec`), so it
holds no decisions and no bouncer registrations — it parses this host's logs (auth.log, the
Docker Authelia, traefik's access log) and still serves the local AppSec listener on :7422 that
this edge's bouncer plugin calls inline. Decisions, allowlists, the Discord notifier and the
Metabase dashboard all live in the cluster; the old `crowdsec` role (dashboard only) is archived
at `roles/containers/archive/crowdsec`. Plan and rationale:
`docs/k3s-migration/slice-6-edge-cutover.md`.

## At a glance
- **Host:** daniel-server
- **Networks:** proxy · **Authelia:** N/A (provides the forward-auth entrypoint)
- **Depends on:** nothing — **everything else depends on this** (deployed first).
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- TLS via Cloudflare DNS-01; routes services at `<hostname>.<domain>` from their labels.
- CrowdSec bouncer/WAF: `crowdsec-acquis.yaml` (this host's log sources + the AppSec listener)
  and `crowdsec-whitelist.yaml` / `crowdsec-trusted-remote-whitelist.yaml` — the whitelist files
  are shared: the cluster roles render these same `files/` into their agents' parsers, so an edit
  here reaches both stacks. `crowdsec-profiles.yaml` and the Discord notifier are still deployed
  by this role but are LAPI-side config, so the cluster engine's copies are the live ones. The
  home-IP allowlist updater moved to daniel-box at B2 (`cscli allowlists` is LAPI-machine-only);
  this role now carries absent-state tombstones that remove its cron, script and state dir.
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
- **Host crons (state-file → monitor-bridge):**
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
- **This edge's bouncer registers on the CLUSTER LAPI, declaratively (slice-6 B2).** Its identity
  is `dockertraefik`, created from the engine's `BOUNCER_KEY_dockertraefik` env
  (`roles/k8s/crowdsec/templates/config-secret.yaml.j2`) with the key
  `crowdsec_bouncer_docker_traefik_key`; `crowdsec_bouncer_api_key` is LEGACY (it authenticated the
  retired local LAPI, kept only for the B2 rollback window). The old probe/delete/re-add rotation
  dance is gone with that LAPI — what remains is one deploy task that probes the cluster LAPI and
  **fails the deploy** on a rejected key, because re-registration never UPDATES an existing key and
  the plugin fails OPEN on auth errors (silent WAF bypass). Rotation runbook:
  `docs/secret-rotation.md` (`assisted`). Inspect with
  `kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers list` on daniel-box —
  `cscli` against the local container answers for an agent with no LAPI and will refuse.
- **Three bouncer-plugin traps, all found live at B2 and all silent-ish:** with
  `crowdsecLapiScheme: https` the plugin requires an explicit
  `crowdsecLapiTLSCertificateAuthorityFile` (it does NOT use system CAs) or the middleware fails to
  construct — and an invalid middleware 404s EVERY router on the entrypoint, so the whole edge goes
  dark; `crowdsecAppsecScheme` silently DEFAULTS TO THE LAPI SCHEME, which pointed https at the
  plaintext local :7422 listener and killed the inline WAF (fail-open, so the only symptom was a log
  line); and plugin config does not reliably hot-reload through the directory-mounted dynamic
  config — **restart traefik after any bouncer config change**.
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

- **Strangler-bridge routers render from ANOTHER HOST's inventory — a `bridge_hostname` addition
  needs a traefik redeploy on daniel-server, or the new route simply doesn't exist.**
  `config.yml.j2` builds its k8s bridge routers by iterating `hostvars['daniel-box'].containers_list
  | selectattr('bridge_hostname', 'defined')`. So when a service migrates to the cluster and gains a
  `bridge_hostname` in *daniel-box's* host_vars, nothing about *daniel-server's* own inventory
  changed — no deploy of the migrated service touches this host, and the GitOps run only redeploys
  roles whose tags it maps from the diff. Until someone runs `deploy.yml --tags traefik` on
  daniel-server, the edge keeps serving the OLD route set and the new `<bridge_hostname>.<domain>`
  404s (found live at slice-5 B2: `zigbee2mqtt.local.<domain>` 404'd after the z2m cutover while
  the pod was healthy). Cutover checklists must pair "add `bridge_hostname`" with "redeploy traefik
  on daniel-server" as one step. (Thanks to the directory-mounted dynamic config below, the
  re-render applies live — no container recreate.)

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
