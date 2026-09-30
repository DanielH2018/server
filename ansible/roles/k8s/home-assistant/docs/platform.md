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
    unban). HA bans the client it resolves from X-Forwarded-For, which is the real client on
    both routes. On the Cloudflare-proxied route Traefik's `cloudflare-realip` Middleware sets
    XFF to CF-Connecting-IP and Traefik appends the Cloudflare edge, so HA receives
    `client, edge`. HA skips the edge because `http.trusted_proxies` lists the Cloudflare ranges
    beside the pod CIDR. Until 2026-09-28 it listed the pod CIDR alone, so HA resolved the EDGE
    as the client and a ban could lock out everyone behind that edge.
    `ansible/tests/services/test_ha_trusted_proxies_cover_cloudflare.py` keeps the literal list
    equal to `cloudflare_ips`. Only failed PASSWORD logins count — tokens/app/webhooks
    unaffected.
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

- **`logger:` silences `pychromecast.controllers` at `critical` (2026-09-27, issue #2781).**
  pychromecast's homeassistant controller hands the Nest Hub Max HA's `external_url`, the
  receiver does not acknowledge the `connect` message, and `_connect_hass failed` raises
  `PyChromecastError` at the cast-status listener. Casting a dashboard still works, so the fault
  is cosmetic to HA and expensive to monitoring: each exception logs a traceback, and over the 7
  days to 2026-09-27 all 197 lines matching monitor-bridge's fatal-log pattern in this container
  were those tracebacks. The bursts reach 21-30/hour against `LOG_ERROR_MAX=20`, which held the
  composite `k8s_workloads` tile red 11 times in 14 days and masked real workload failures while
  red. The connect-back itself is unfixed.

### The cast connect-back failure is episodic: pychromecast blocks its own reader thread (issues #2800, #2845)

Measured 2026-09-28 from Loki over the 7 days to 2026-09-27, and on 2026-09-30 from Loki over
10 days plus one live test cast at debug level. Every time below is the container's
`America/Chicago` clock, which is what the log line prints; HA's API and `git %ci` are UTC, so
do not compare the two without converting.

**The cause is a self-deadlock in pychromecast 14.0.10, not the network and not the
receiver.** pychromecast's `SocketClient` is the one thread that reads every message from the
Hub (`Thread-5` in HA's log). When it sees the HA Lovelace app on a new channel, it calls
`HomeAssistantController.channel_connected()` on that same thread. That calls `get_status()`,
and while the controller is not yet connected, `get_status()` calls `_connect_hass()`.
`_connect_hass()` sends `connect`, then blocks for `DEFAULT_HASS_CONNECT_TIMEOUT = 30` seconds
until `_hass_connecting_event` is set. Only the receiver's `receiver_status` reply sets that
event, and only `Thread-5` can read the reply, so the wait always runs out. `Thread-5` then
logs `_connect_hass failed` and raises `PyChromecastError` into
`Exception thrown when calling cast status listener`.

- **All 197 failures in the window take that path.** Every traceback reads
  `socket_client.py` `new_cast_status` → `receiver.py` `_process_get_status` →
  `homeassistant.py` `channel_connected` → `get_status` → `_send_connected_message` →
  `_connect_hass`, with the line numbers of pychromecast 14.0.10. Every warning is logged on
  `Thread-5`.
- **The live test cast shows why most casts escape it.** On 2026-09-30 at 07:50:56 the
  operator-approved test turned the Hub off, and `bedroom_display_show` re-cast at once.
  `SyncWorker_7` entered `_connect_hass` first and cleared the event. At 07:50:59 `Thread-5`
  ran `channel_connected` → `_connect_hass` and hit the early return
  `_hass_connecting_event not set`, so it went back to reading. At 07:51:00 it read
  `receiver_status connected: True`, and the Hub showed the dashboard. The deadlock fires only
  when `Thread-5` reaches `_connect_hass` with no HA-initiated cast already waiting. That
  happens when HA re-joins an HA app already running on the Hub without casting, for example
  after a socket drop.
- **A blocked `Thread-5` also fails a concurrent cast.** On 2026-09-29 at 14:23:36 a
  `Connection reset by peer` on the Hub's socket was followed within 20 ms by
  `bedroom_display_show` casting. At 14:24:06, 30.03 s later, the cast failed with
  `Exception in _handle_signal_show_view` on `homeassistant.util.logging`. That record is not
  silenced, and its message carries the cast user's `refresh_token` in plaintext.
- **The fault is episodic.** The 197 warnings fall in 11 hour-buckets across 4 of the 7 days
  (09-21, 09-22, 09-23, 09-26), and 3 days have none. The longest clean gap inside the window
  is about 56 h, so a quiet day or two is not evidence the fault has cleared. Inside a burst,
  consecutive warnings sit 30.05 s apart: each timeout is followed at once by another. Why the
  loop re-enters was not established.

**The receiver JS exception is a symptom of the loop.** The 1570
`frontend.js.modern.<build>` `Uncaught error from Chrome 150.0.0.0 on unknown OS` records in
the same 11 buckets are all one exception, `TypeError: Cannot read properties of null
(reading 'themes')`. Every one carries the same stack: `hui-view-container`
`_setUpMediaQuery` → `media_query` `listener` → `_applyTheme` → `applyThemesOnElement`, at
`src/common/dom/apply_themes_on_element.ts:117`. That line reads `themes.themes[themeToApply]`.
`themeToApply` is `nest_dark`, because `files/ui-lovelace.yaml` pins that theme to the view.
`themes` is `hass.themes`, which is null on the fresh `hass` the receiver builds for each
`connect`. The receiver's `_handleConnectMessage` sends `receiver_status` before that render
runs, so the throw does not block the reply. Each loop iteration resends `connect`, and the
per-cycle JS error count grows by about one. The frontend bug is real, but it is upstream in
`home-assistant/frontend` and only cosmetic here.

**Three candidate causes from #2800 are refuted.** The LAN hairpin through Cloudflare, the
receiver rejecting the served certificate, and CrowdSec or the CF-only-origin allowlist
refusing the device would each fail on every cast rather than in bursts. None of them would
deliver receiver-side JS errors into HA's log, and the receiver's reply does arrive: `Thread-5`
simply cannot read it while it waits.

**Nothing in this repo's config fixes it.** The fix belongs in
`home-assistant-libs/pychromecast`: `channel_connected` must not block the socket thread. It
could send `get_status` without waiting for the connect ack, or `_connect_hass` could refuse to
wait when called on the socket thread. Issue #3014 tracks filing that report upstream and
carries the draft.

**`external_url` is not the thing to change either.** HA's cast integration resolves the URL
itself: `hass_url = get_url(hass, require_ssl=True, prefer_external=True)` in
`homeassistant/components/cast/home_assistant_cast.py`, read from core `dev` on 2026-09-28.
`prefer_external=True` takes `external_url` whenever it is https, and ours is, so the only way
to hand the receiver `internal_url` is to remove or downgrade `external_url`. The working cast
path, the companion app and the Cloudflare route all depend on it. There is no
per-integration override.

**Verify with the `frontend.js` channel, never with `_connect_hass failed`.** #2781 silenced
both records the fault emits, because `Exception thrown when calling cast status listener` is
logged on `pychromecast.controllers` too. A query for `_connect_hass failed` therefore returns
nothing on a cluster where the fault is firing, which is why issue #2800's own verify-by was
unfalsifiable. The query that still works:

```logql
{job="k8s", container="home-assistant"} |= "Uncaught error from Chrome"
```

It fires only while the loop keeps resending `connect` to a receiver showing the `nest_dark`
view, so it misses a single blocked cycle. The 2026-09-29 failure left no `frontend.js` record.
Query `|= "_handle_signal_show_view"` as well to catch a cast that collided with a blocked
`Thread-5`, and redact its output: the record prints the cast `refresh_token`.

Nothing in `files/configuration.yaml` silences `frontend.js`, and
`ansible/tests/services/test_ha_cast_verify_signal_is_not_silenced.py` fails if a future
`logger:` entry takes that channel away as quietly as #2781 took the first one.

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
  `media_player.bedroom_display` is a `media_player`, so it is a valid *target* for a TTS or a
  `media_player.play_media` call. **No TTS provider is configured on this instance** — the
  `tts.speak` / `tts.cloud_say` services are registered, but `probe.py ha get states` returned no
  `tts.*` entity for `tts.speak` to speak through (checked 2026-09-10). Speech to the Hub needs a
  provider added first; that is separate work from Browser Mod, which would not supply one. Only a
  **popup overlay** genuinely needs Browser Mod, and that is the one thing the cast receiver
  cannot render.
- **`configuration.yaml` ships verbatim** from `files/configuration.yaml` — the ConfigMap
  (`roles/k8s/home-assistant`) carries it with `lookup('file')`, and an init container
  installs it into `/config` at pod start. It sets `use_x_forwarded_for: true` +
  `trusted_proxies` holding the pod CIDR `10.42.0.0/16` and the Cloudflare ranges, so HA
  honors Traefik's `X-Forwarded-For` (without the pod CIDR HA rejects the proxied request with
  "400 Bad Request") and skips the Cloudflare edge in it. The whole pod CIDR stays trusted
  because Traefik's pod IP changes on every restart; `configuration.yaml`'s `DECIDED: the whole
  pod CIDR` comment has the trade-off. The manifests task rollout-restarts HA when the rendered config changes,
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
