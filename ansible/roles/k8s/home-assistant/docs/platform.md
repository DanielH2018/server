# home-assistant — platform, auth and storage

Split out of the role's `CLAUDE.md` on 2026-08-15; the content is unchanged.
How HA itself is configured, authenticated and persisted — as opposed to what the
automations do.

## Auth, Marketplace and configuration.yaml
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
- **Marketplace installs community integrations.** Home Assistant ships it built in from
  2026.11, where it replaces HACS and migrates HACS's content on the first start. Do not add
  `DOCKER_MODS=linuxserver/mods:homeassistant-hacs` back: that mod downloads `hacs.zip`
  whenever `custom_components/hacs/` is absent, with no version check, so it reinstalls the
  integration the migration removed.
  **A Marketplace integration or card lands in** `/config/custom_components/` or `/config/www/`,
  which are PVC state — the repo has no init pattern that declares one, so every such package is
  a one-time dashboard action and survives only because the Longhorn PVC does. Record any such
  install in this file.
- **Two files reach `/config` from Secrets, not from git.** The `config-install` init container
  installs `secrets.yaml` from the `home-assistant-secrets` Secret, which
  `templates/config/secrets.yaml.j2` renders, and `google_service_account.json` from the
  `home-assistant-google-sa` Secret, which `templates/google-sa-secret.yaml.j2` renders from
  the SOPS key `google_assistant_service_account`. Both Secrets mount at `/sec` through one
  projected volume. `configuration.yaml` reads the second with `service_account: !include
  google_service_account.json`, the form HA documents, and the integration ignores the JSON's
  fields other than `client_email` and `private_key`. `project_id` comes from its own SOPS
  key, `google_assistant_project_id`, through `secrets.yaml`. The service account has a Secret
  of its own because the value is multi-line, and a Flux placeholder cannot carry a newline
  (target slice R2, PR 5). `validate_ha_config.py` loads the include as a placeholder, as it
  does a `!secret` value, because the repo holds no copy of the file.

- **`logger:` silences `pychromecast.controllers` at `critical` (2026-09-27, issue #2781).**
  pychromecast's homeassistant controller hands the Nest Hub Max HA's `external_url`, the
  receiver does not acknowledge the `connect` message, and `_connect_hass failed` raises
  `PyChromecastError` at the cast-status listener. Casting a dashboard still works, so the fault
  is cosmetic to HA and expensive to monitoring: each exception logs a traceback, and over the 7
  days to 2026-09-27 all 197 lines matching monitor-bridge's fatal-log pattern in this container
  were those tracebacks. The bursts reach 21-30/hour against a threshold of 20 per hour, which
  the `WorkloadLogErrors` rule in
  `ansible/roles/k8s/loki-homelab/files/rules/homelab-logs.yaml` keeps. They held the
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
  `_connect_hass`, with the line numbers of pychromecast 14.0.10. All 197 warnings and all 197
  listener exceptions are logged on `Thread-5`.
- **The live test cast shows why most casts escape it.** On 2026-09-30 at 07:50:56 the
  operator-approved test turned the Hub off, and `bedroom_display_show` re-cast at once.
  `SyncWorker_7` entered `_connect_hass` first and cleared the event. At 07:50:59 `Thread-5`
  ran `channel_connected` → `_connect_hass` and hit the early return
  `_hass_connecting_event not set`, so it went back to reading. At 07:51:00 it read
  `receiver_status connected: True`, and the Hub showed the dashboard. The deadlock fires only
  when `Thread-5` reaches `_connect_hass` with no HA-initiated cast already waiting, that is,
  when the socket thread sees the HA app on a new channel that HA did not cast. What starts that
  on the Hub is not established. A socket drop is a candidate, but the 09-21 10:15 burst began
  6 minutes after the 10:09 socket failure, not at it.
- **A second, separate failure: a `connect` lost across a socket reconnect.** On 2026-09-29
  at 14:23:36.520 the Hub's socket reset (`Connection reset by peer`), which runs
  `channel_disconnected` and clears the controller's status. At 14:23:36.552
  `bedroom_display_show` cast, and `SyncWorker_23` sent `connect` across the reconnect. At
  14:24:06, 30.03 s later, that cast failed with `Exception in _handle_signal_show_view` on
  `homeassistant.util.logging`. The traceback ends at the `raise` in `_connect_hass`, which only
  the thread that sent `connect` and waited can reach. So the worker, not `Thread-5`, held the
  wait, and no reply arrived. Why that reply was lost is not established. The record's message
  carries the cast user's `refresh_token` in plaintext. A cast that
  runs into a blocked `Thread-5` fails differently: it returns early, its callback later
  receives `False`, and it logs nothing.

  **HA does not write that token anywhere (issues #3015, #8203).** `logger.filters` in
  `files/configuration.yaml` drops any `homeassistant.util.logging` record that matches
  `refresh_token` before a handler sees it. So the record reaches neither the container's stdout,
  which `kubectl logs` reads unredacted, nor `system_log` and `/api/error_log`.
  `tests/test_logger_filter_drops_refresh_token.py` applies HA's own matching rule to the
  shipped config. The redaction layers stay as a backstop for a record the filter misses:
  Alloy's `stage.replace` in `roles/k8s/loki-homelab` strips the token on the way into Loki, and
  `redact_log_secrets` in `scripts/diagnostics/probe_lib/core.py` strips it out of `probe.py ha
  get` and `probe.py loki-query`. The filter does not reach copies written before it shipped:
  the pod's log file on daniel-box until kubelet rotates it, and Loki records stored before the
  Alloy stage until the 744h retention expires. Rotating the cast user's refresh token is what
  ends those; *Rotating the cast user's refresh token* below has the steps. `probe.py
  loki-query --json` streams the raw body and is NOT redacted.
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
`themeToApply` was `nest_dark`, because `files/ui-lovelace.yaml` pinned that theme to the view.
`themes` is `hass.themes`, which is null on the fresh `hass` the receiver builds for each
`connect`. The receiver's `_handleConnectMessage` sends `receiver_status` before that render
runs, so the throw does not block the reply. Each loop iteration resends `connect`, and the
per-cycle JS error count grows by about one. The frontend bug is upstream in
`home-assistant/frontend` and only cosmetic here. The operator unpinned the theme on
2026-09-30 (#2800), so `themeToApply` is empty and the throw no longer fires. The cast display
renders light as a result.

**Three candidate causes from #2800 are refuted.** The LAN hairpin through Cloudflare, the
receiver rejecting the served certificate, and CrowdSec or the CF-only-origin allowlist
refusing the device would each fail on every cast rather than in bursts. None of them would
deliver receiver-side JS errors into HA's log, and the receiver's reply does arrive: `Thread-5`
simply cannot read it while it waits.

**The loop runs for hours, and it blocks the dismiss (#8201).** On 2026-11-12 it ran in every
hour from 09:00 to 14:54, 57-109 timeouts an hour, not in the occasional 30 s stalls that #2800
and #3014 were accepted on. `media_player.turn_off` needs the blocked thread to read the quit
reply, so `bedroom_display_dismiss` failed four times that day and left the Hub on the
dashboard in a dark room.

**The repo ends the loop by quitting the app over a second connection.** The loop runs only
while the dashboard app is on the Hub, so a quit that lands stops it. `bedroom_display_dismiss`
retries every 5 minutes. When HA's own `turn_off` has not taken effect 15 s later, it runs
`shell_command.bedroom_display_quit_app`. That command runs `files/cast_quit_app.py` with the
image's own pychromecast. The script connects to the Hub with no Home Assistant controller, so
nothing on its connection can block in `_connect_hass`. It reads the running app itself and
quits only the dashboard, so HA's stale view of the Hub cannot stop user media. A failure exits
non-zero, which logs on `homeassistant.components.shell_command`, inside the runtime-error
alert's scope. The real fix is still upstream: `channel_connected` must not block the socket
thread. The operator chose on 2026-11-13 not to wait on it; the `DECIDED:` marker at
`shell_command:` in `files/configuration.yaml` records that.

**`external_url` is not the thing to change either.** HA's cast integration resolves the URL
itself: `hass_url = get_url(hass, require_ssl=True, prefer_external=True)` in
`homeassistant/components/cast/home_assistant_cast.py`, read from core `dev` on 2026-09-28.
`prefer_external=True` takes `external_url` whenever it is https, and ours is, so the only way
to hand the receiver `internal_url` is to remove or downgrade `external_url`. The working cast
path, the companion app and the Cloudflare route all depend on it. There is no
per-integration override.

**Only a failed cast still leaves a record.** #2781 silenced both records the connect-back
loop emits, because `Exception thrown when calling cast status listener` is logged on
`pychromecast.controllers` too, so a query for `_connect_hass failed` returns nothing whether
or not the loop is firing. The receiver's `Uncaught error from Chrome` records were the
channel that survived, and they came from the pinned theme's TypeError above. With the theme
unpinned they stop, so the loop itself now has no audible signal. That is accepted with #2800,
and the guard that kept the channel audible was retired with the pin.

A cast that timed out on a worker thread leaves no record either. Its
`_handle_signal_show_view` record carries the cast `refresh_token`, so the logger filter above
drops it. That was the operator's choice on 2026-11-12, over closing `kubectl logs`; the
`DECIDED:` marker at the filter in `configuration.yaml` records it. #8206 tracks a Loki rule on
pychromecast's `Heartbeat timeout` lines to replace the lost signal. This query still finds
records stored before the filter shipped, until the 744h retention expires:

```logql
{job="k8s", container="home-assistant"} |= "_handle_signal_show_view"
```

`probe.py loki-query` redacts the token and keeps the marker. `--json`, `logcli` and Grafana
Explore read the stored line, which carries the token for anything shipped before the Alloy
redaction stage.

### Rotating the cast user's refresh token

The token is not in SOPS. HA's cast integration creates a system user for the config entry and
mints the token itself (`homeassistant/components/cast/home_assistant_cast.py`,
`async_setup_ha_cast`). The token is a working HA API credential, so rotate it whenever a copy
leaks. The operator ran these steps on 2026-11-13, in this order.

1. **Delete the Google Cast config entry**, from Settings → Devices & services → Google Cast →
   ⋮ → Delete. Removing the entry deletes the cast system user, and its token with it
   (`async_remove_entry` calls `async_remove_user`). The old token is void from this point.
   Do not try Settings → People: HA refuses to change or delete a system user there ("System
   users cannot be updated").
2. **Add Google Cast again**, with `10.0.0.137` as its known host, in the add form or the
   entry's Configure dialog. HA is pod-networked, so it
   has no mDNS discovery and finds the Hub through the known host alone. Setup creates a new
   system user with a new token.
3. **Restart HA**, from Settings → System → Restart. Without this, the new entry discovers
   nothing and logs nothing. The deleted entry's discovery holds a process-wide lock that
   only an HA stop releases, and the new entry's `setup_internal_discovery` returns at once
   when it cannot take the lock (`homeassistant/components/cast/discovery.py`). On the restart
   HA logs `Stopping internal pychromecast discovery`.
4. **Check that the Hub is `media_player.bedroom_display` again**, and rename the entity ID back
   if HA chose a new one. Every display automation targets that ID. Run `probe.py ha state
   media_player.bedroom_display`, then `probe.py ha verify-entities` and `probe.py ha
   verify-automations`.

Casting stops between step 1 and step 4. Once the Hub is back, `bedroom_display_show`
re-casts the dashboard on its own in a lit room.

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

## Slow rollouts come from the image pull, not the volume

A cold pull of the Home Assistant image caused the October 2026 rollout outages (#4188). The
Deployment is `Recreate`, so the old pod stopped before the new pod's pull began, and HA was
down for the whole pull.

- **The 2026.10.0 bump, 2026-10-10.** HA was down for about 28 minutes. Loki holds
  `NodePublishVolume` lines at 04:02:41Z and 04:15:42Z, one for each pod that waited. Both of
  those pods unpublished at 04:30:12Z, and the pod that started at 04:30:13Z served.
- **The 2026.10.2 bump, 2026-10-17.** The main container read `PodInitializing` from 06:25Z to
  06:49:30Z. daniel-box received 722 MB more than in the preceding 26 minutes, which is the
  720 MB image at about 0.46 MB/s.
- **The longer part of that outage was the rollback, in a path HA no longer takes.** The stuck
  pods held the volume until 06:50Z, so `k8s/volume-revert` timed out on its detach wait. It
  had already scaled the Deployment to zero, and HA stayed at zero until 11:57Z. Flux owns this
  role, so no automated rollback runs here.
- **The volume was not the cause.** `config-install` writes to the volume, and it read
  `Completed` in both stuck pods on 2026-10-17. Longhorn logged no detach during either wait.

The `home-assistant-prepull` Kustomization moves the pull ahead of the restart. Rollouts with it
were down for about 2 minutes on 2026-10-25 and 2026-10-27, but neither was a large pull. On
2026-10-25 the Job lasted under 2 minutes and daniel-box received about 8 MB more than the
baseline. The 2026-10-27 rollout changed only the init image.

To read a large pull after its Pod events expire, use Prometheus:

- **The pull's length:** `kube_job_status_active{job_name=~"prepull-home-assistant-.*"}`.
- **The outage's length:** `probe_success{instance=~".*home-assistant.*"}`.
- **A pull that the pre-pull missed:** `kube_pod_container_status_waiting_reason` reads
  `PodInitializing` on the main container while
  `kube_pod_init_container_status_terminated_reason` reads `Completed`. A pod with init
  containers reports that reason during the main container's pull.

Read them soon after the rollout. Prometheus' `7GiB` retention size ends the window before its
`30d` retention time: on 2026-11-05 its oldest sample was 21.5 days old.
