# home-assistant — platform, auth and storage

Split out of the role's `CLAUDE.md` on 2026-08-15; the content is unchanged.
How HA itself is configured, authenticated and persisted — as opposed to what the
automations do.

## Auth, HACS and configuration.yaml
- **Auth: HA's own login, NOT Authelia.** `use_authelia: false` is deliberate —
  Authelia forward-auth breaks the HA companion mobile app, webhooks, and long-lived
  API tokens (none can complete the portal login flow). The route still gets Traefik
  TLS + CrowdSec + per-router rate-limiting; harden the gate inside HA. If you ever want
  Authelia on the *web UI only*, you'd need per-path bypass rules for `/api/`, `/auth/`,
  and the webhook paths.
  - **`ip_ban_enabled: true` + `login_attempts_threshold: 5`** (in `configuration.yaml`'s
    `http:`) auto-ban an IP after 5 failed logins (→ `config/ip_bans.yaml`; delete a line to
    unban). Bans the REAL client IP because the CF→Traefik→HA chain forwards X-Forwarded-For
    (Traefik `forwardedHeaders.trustedIPs=cloudflare_ips` + HA `use_x_forwarded_for`). Only
    failed PASSWORD logins count — tokens/app/webhooks unaffected.
    **The ban applies to every request, not just logins, and that reaches infrastructure.** HA's
    ban middleware keys on the peer address, so an unauthenticated burst from inside the cluster
    bans an INTERNAL ip. On 2026-08-23 five ad-hoc `curl` calls from daniel-box banned
    `10.42.0.1`, the node's pod-network gateway — which is also where kubelet probes come from —
    and HA then 403'd its own probes into a crash loop. The probes are immune now (they exec curl
    to `127.0.0.1`; see `deployment.yaml.j2`), and monitor-bridge's HA monitor has an `ip_ban` arm
    so a ban is visible rather than silent. Note the counter has no decay: those five failures
    accumulated over 21 minutes.
  - **TOTP/MFA: enrolled (2026-06-18).** This route is internet-facing (Cloudflare-proxied
    `home-assistant.<domain>`), so MFA is the compensating control for Authelia-off; `ip_ban` is
    defense-in-depth on top. If MFA is ever reset/lost, re-enrol: HA → Profile → Multi-factor
    Authentication → TOTP (and keep the recovery code from enrolment).
- **HACS preinstalled** via `DOCKER_MODS=linuxserver/mods:homeassistant-hacs`
  (LSIO Docker mod that drops the Home Assistant Community Store into `/config`).
  **The mod installs HACS itself and nothing else.** A HACS integration or card lands in
  `/config/custom_components/` or `/config/www/`, which are PVC state — the repo has no init
  pattern that declares one, so every HACS package here is a one-time dashboard action and
  survives only because the Longhorn PVC does. Record any such install in this file.

### Browser Mod does not extend the cast display (investigated 2026-09-10, issue #1454)

`thomasloven/hass-browser_mod` was proposed to make the Nest Hub Max cast dashboard
interactive. Rejected, and the reasons are worth keeping because the proposal reads plausible:

- **There is no YAML surface to configure.** Browser Mod 2.0 removed the `browser_mod:` block;
  it is config-flow only ([upstream README](https://github.com/thomasloven/hass-browser_mod)).
  `validate_ha_config.py` does no HA schema validation, so writing that key into
  `files/configuration.yaml` would pass prek, pass CI and deploy green while doing nothing.
- **Browser Mod adds itself to the dashboard resources**, and a dashboard carrying a custom
  resource is what breaks casting. `home-assistant/core#159553` (open, filed 2025-12-21) has
  the cast receiver rendering "Configuration error" for dashboards with custom cards from core
  2025.12.3 onward; this instance runs 2026.6.3. Installing it risks the working `nest_dark`
  cast view for no gain.
- **A cast receiver does not register as a Browser Mod browser.**
  `thomasloven/hass-browser_mod#408` is the "Register CAST device" toggle leaving
  `Last connected: Never`, unresolved.
- **Two of the three wanted capabilities already exist.** `cast.show_lovelace_view` drives the
  view from an automation today (`files/automations/display.yaml`, `bedroom_display_show`), and
  `media_player.bedroom_display` is a valid target for the `tts.speak` / `tts.cloud_say`
  services. Only a **popup overlay** genuinely needs Browser Mod, and that is the one thing the
  cast receiver cannot render.
- **`configuration.yaml` ships verbatim** from `files/configuration.yaml` — the ConfigMap
  (`roles/k8s/home-assistant`) carries it with `lookup('file')`, and an init container
  installs it into `/config` at pod start. It sets `use_x_forwarded_for: true` +
  `trusted_proxies` covering every hop of the bridge chain (`172.16.0.0/12`, the pod CIDR
  `10.42.0.0/16`, and daniel-server's LAN IP — the bridge egresses as it) so HA honors
  Traefik's `X-Forwarded-For` (without it HA rejects the proxied request with
  "400 Bad Request"). The manifests task rollout-restarts HA when the rendered config changes,
  so an edit takes effect on the next deploy. **Note:** HA may rewrite parts of its
  own config via the UI, but this file is the Ansible source of truth and is
  overwritten on deploy — keep UI-managed config (integrations, etc.) in the areas HA
  stores separately (`.storage/`, the recorder DB…), which are NOT templated.
  - **Smart-plug entity_id renames (2026-06-25, NOT templated — survives deploy, NOT a Z2M re-pair).**
    The 3 room plugs paired with raw Z2M IEEE entity_ids (e.g. `switch.0xffffb40e06088788`); renamed
    in the **HA entity registry** to `switch.air_purifier` / `switch.airgradient_lamp` /
    `switch.behind_bed` (+ all their sub-entities `<domain>.<slug>_<suffix>`). Entity-id renames are
    **WebSocket-only** (`config/entity_registry/update` with `new_entity_id`) — the REST API / probe
    can't. The registry lives in `.storage`, so this persists across deploys but a **Zigbee re-pair
    re-mints the IEEE ids** → re-apply (the one-off WS loop reuses `probe.py`'s token/IP/WS helpers).
    `switch.desk_surge_protector_strip` already had a clean id. `automation.bedroom_air_purifier_presence`
    references the renamed `switch.air_purifier`.

## Storage and networking
- **All persistent state is the `home-assistant-config` Longhorn PVC → `/config`** (`longhorn`
  class → nightly B2 backup; Kopia stopped covering this at the cutover): the SQLite
  recorder DB, `.storage/`, secrets, automations, and the templated `configuration.yaml`.
  **The "could not validate that the sqlite3 database was shutdown cleanly" warning on every boot is
  benign and not worth chasing with a longer shutdown grace** — a timed `docker stop` hit the full
  grace and exited 137 (SIGKILL) at both 30s and 90s, so HA under the LSIO/s6 image is effectively
  hung on shutdown (HA core / the dreo cloud_push integration never finishes stopping). SQLite WAL
  auto-recovers. Tested + reverted 2026-06-18 under Docker; the same holds in k8s, where the knob
  is `terminationGracePeriodSeconds` and raising it only slows every rollout.
- **Pod networking, not host** (was bridge networking under Docker — same consequence). Cloud/
  API-based integrations work fine. **Local device discovery** (mDNS/SSDP, Bluetooth, Zigbee/Z-Wave
  USB dongles) generally needs host networking and/or device passthrough — which is incompatible
  with the ingress-routed setup here. It has never been needed: the Zigbee coordinator is
  network-attached (SLZB-06M over TCP), which is also what let the whole smart-home stack move
  hosts at slice 5. Revisit only if you add local hardware.
