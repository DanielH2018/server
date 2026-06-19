# Home Assistant — Holistic Review (2026-06-19)

Scope: the full HA setup as it exists in `ansible/roles/containers/home-assistant/` — infra
(compose/networking/security), automation triggers & logic, and cross-cutting resilience /
observability. Read against image `2026.6.3-ls221`, container healthy, `restarts=0`.

**Headline:** this is a genuinely well-engineered, heavily-documented single-room (bedroom) system.
The script-computes / caller-gates split, the single-source-of-truth values
(`sensor.bedroom_wake_start`, `fan.jinja`), and the one `script.bedroom_notify` funnel are all
the *right* patterns. Findings below are concentrated at the **seams** between those layers, plus
infra/resilience gaps that are inherent to the architecture rather than mistakes.

Findings are ranked by value. Each is tagged **[fixed]**, **[recommend]**, or **[note]**.

---

## 1. Correctness bugs (logic seams)

### 1.1 — Wake ramp is masked for alarms before ~05:15 **[fixed this pass]**
`script.bedroom_apply_natural` (`files/scripts.yaml`) is an ordered `choose:`. The **first**
exception is the deep-night nightlight, condition `sleep_mode on OR now().hour < 5`
(`scripts.yaml:77-84`). The **second** is the morning wake ramp, window `[wake_start, wake_start+15m)`
(`scripts.yaml:91-109`).

`sensor.bedroom_wake_start` = watch alarm − 15 min, and its availability guard admits any **alarm**
with local hour `3 ≤ h < 11` (`templates.yaml:19-23`). So the *window* can start as early as **02:45**
(alarm 03:00) and routinely lands before 05:00 for any alarm earlier than **05:15**.

Because the nightlight exception is evaluated **first** and its `now().hour < 5` clause is true during
those early windows, an early alarm gets the **3% amber nightlight instead of the 1%→50% wake ramp**.
The morning-reset `at: sensor.bedroom_wake_start` trigger still fires and clears overrides, but the
`apply_natural` call it makes picks the wrong branch. The in-code comment ("at wake time … the hour is
>= 5") silently assumes alarms ≥ 05:15, which the availability guard does **not** enforce.

**Fix applied:** the nightlight exception now yields to the wake window — condition becomes
`(sleep_mode OR hour<5) AND NOT in_wake_window`. The deep-night "got up to pee" behaviour is
unchanged (outside any wake window it still gives the dim nightlight); only the pre-05:00 wake window
now correctly runs the ramp. Surgical, reuses the exact `in_window` expression the next exception and
`bedroom_presence_on` already use.

### 1.3 — UPS low-battery `pierce` alert shadowed by the outage branch **[fixed this pass]**
Found by sweeping every `choose:` in the suite for the same shadowing class as 1.1. `ups_power_event`
(`automations.yaml`) ordered its branches **on-battery → low-battery → recovered**. `choose:` is
first-match-wins, so a single `OL → "OB LB"` transition (battery already weak when mains drops, both
NUT flags set in one step) matched the *routine* on-battery branch and **shadowed the low-battery
`pierce` branch** — you'd get the silent-in-DND outage buzz instead of the can't-miss "server may shut
down" alert, in exactly the scenario where it matters most.

**Fix applied:** reordered so newly-low-battery is checked **first** (it's strictly the more urgent
tier). Verified strictly safe across every transition: the normal `OL→OB→OB LB` sequence is unchanged
(on the `OL→OB` step `is_lb` is false, so the outage branch still fires; the LB pierce fires on the
later `OB→OB LB` step), and the simultaneous case now correctly pierces. Of the seven `choose:` blocks
in the suite, this was the **only** sibling of 1.1 — the other six are clean (mutually-exclusive
action strings or two-way `bad`/`ok`, `offline`/`recovery` splits).

### 1.2 — Whole alert system depends on two hard-coded notify entity names **[recommend]**
Every alert in the system funnels through `script.bedroom_notify`, which calls
`notify.mobile_app_pixel_9_pro` and `notify.pixel_watch_3` by literal name (`scripts.yaml:303,316`).
If the phone/watch is re-paired or renamed in the companion app, **the service name changes and every
alert silently stops** — including the alerts that would tell you something is wrong (the watchman
problem). There is no fallback and no heartbeat that would surface a dead pipeline.

Recommended (pick one, low effort):
- Add a `persistent_notification.create` fallback inside `bedroom_notify` for `pierce` alerts, so a
  can't-miss alert at least lands in the HA UI if the mobile push fails; **and/or**
- Route through a `notify` group (defined in `.storage` or `notify:` YAML) so the target is one
  decoupled indirection instead of a literal in two places; **and/or**
- A daily HA→Uptime-Kuma push heartbeat (see 4.1) catches a wedged HA even when push is dead.

Turnkey: append this to the end of `script.bedroom_notify`'s `sequence:` so a `pierce` alert also
lands in the HA UI even if the mobile push silently fails (zero change to the normal path):

```yaml
    - if: "{{ pierce | default(false) }}"
      then:
        - service: persistent_notification.create
          data:
            notification_id: "{{ tag }}"   # same tag coalesces, mirrors the mobile behaviour
            title: "{{ title }}"
            message: "{{ message }}"
```

---

## 2. Behaviour worth changing

### 2.1 — Adaptive Lighting self-turns-on the bedroom lights at every HA start/deploy **[implemented 2026-06-19]**
Shipped as `automation.bedroom_al_startup_suppress` (`files/automations.yaml`). **Verify the 20 s delay
outlasts AL's startup apply** on your box — if a restart shows AL re-on the lights *after* the
suppress fires, bump the delay. Fails safe (gated on empty room + no wake window → worst case leaves
lights on = status quo).
Known + logged (memory `ha-adaptive-lighting-self-on-startup`, AL's own log flags the bug): after
**every** HA restart/deploy (~every config edit, ~120 s) AL flips `light.bedroom_lights` on by itself.
With `min_brightness: 1` it's a brief low flash, but it's a real daily annoyance and — combined with
`bedroom_presence_on` / `bedroom_absence_off` — can leave the room lit if you're not there to let
absence-off catch it.

Concrete mitigation (ready to drop into `files/automations.yaml`), gated so it never fights a *wanted*
on-state:

```yaml
- id: bedroom_al_startup_suppress
  alias: Bedroom suppress AL self-on at startup
  description: AL turns the bedroom lights on by itself after every HA restart; turn them back off
    unless someone is actually in the room (or it's the wake window).
  mode: single
  trigger:
    - platform: homeassistant
      event: start
  action:
    - delay: "00:00:20"   # let AL finish its startup apply
    - if: >-
        {{ is_state('binary_sensor.aqara_fp300_presence', 'off')
           and states('sensor.bedroom_wake_start') in ['unknown', 'unavailable'] }}
      then:
        - service: light.turn_off
          target: {entity_id: light.bedroom_lights}
```

This is a behaviour change, so it's flagged **recommend** not auto-applied — confirm the 20 s delay
beats AL's startup apply on your box before trusting it.

### 2.2 — The dynamic wake hinges on the watch publishing `next_alarm` *early enough* **[note]**
`sensor.bedroom_wake_start` fires the morning ramp via `at: sensor.bedroom_wake_start`. If the Pixel
Watch only surfaces `sensor.pixel_watch_3_next_alarm` **after** `wake_start` has already passed (some
watch firmwares update next_alarm late, or only when the alarm is "armed"), the time trigger's target
is in the past and **the ramp silently never runs**. This is device-behaviour-dependent and hard to
defend against in HA. Worth a one-off check: confirm `next_alarm` populates the evening before, not at
alarm time. If it's flaky, a fixed-time fallback ramp (your old behaviour) as a floor is the safety net.

---

## 3. Resilience / single points of failure

### 3.1 — Git-managed automations depend on `.storage`-only entities **[note]**
`light.bedroom_lights` (a light **group**), `person.daniel`, `device_tracker.pixel_9_pro`, the
`notify.mobile_app_*` / `notify.pixel_watch_3` targets, and the Adaptive Lighting switches are
referenced all over the git-tracked YAML but **defined nowhere in git** — they exist only in HA's
`.storage` (Kopia-backed, not version-controlled). A bare-metal "rebuild from the repo" would bring up
HA with every automation referencing entities that don't exist yet. This is inherent to HA's UI-config
split and **not** a defect, but it means: (a) the Kopia restore of `.storage` is **load-bearing**, not
optional — verify it's actually in a restore drill; (b) the light **group** membership in particular
is invisible state that a UI accident could silently change. Consider defining `light.bedroom_lights`
as a YAML `light: platform: group` (templatable, git-tracked) so the one most-referenced entity is
reproducible from the repo. Lower priority; documented so it's a conscious choice.

### 3.2 — Cloud / single-host dependencies **[note]**
- The **fan** is the DREO `cloud_push` integration — *all* fan control dies if DREO cloud is down
  (and its parent-less echo is the reason `bedroom_fan_manual_detect` needs the expected-level dance).
  No local fallback exists; accept it or eventually move to a local-control fan.
- `sensor.pixel_9_pro_sleep_duration` (Google Sleep API) and the watch alarm are cloud-sourced; both
  already have graceful `float(0)` / availability fallbacks — good.
- HA, Mosquitto and Z2M all live on `daniel-server`; a host outage takes the whole smart home. Normal
  for a homelab, noted for completeness.

---

## 4. Observability gaps

### 4.1 — HA is monitored as a *container*, not as a *function* **[deferred → `ansible/PLANS.md` backlog]**
The autokuma label gives you up/down on the container, but a **wedged-but-running** HA (event loop
stuck, automations not firing, recorder locked) looks "up". The system already has the monitor-bridge
/ Uptime-Kuma push pattern. A trivial HA automation that pushes a Kuma push-monitor heartbeat
(daily, or on a 5-min `time_pattern`) would catch a silently-dead automation engine — the one failure
mode none of the existing alerts can self-report (they all need HA + push to be working). Pairs with
1.2.

Turnkey (needs one manual step + one secret):
1. In Uptime Kuma, create a **Push** monitor (heartbeat interval ~10 min, retries 0 — matches the
   `kuma-push-down-no-heartbeat` lesson); copy its push URL.
2. Store the token as a SOPS secret (e.g. `ha_kuma_heartbeat_url`) and surface it to HA — simplest is
   a templated `rest_command` in a new `files/`-style include, or hard-code the URL in a `rest_command:`
   block in `configuration.yaml.j2` since the token is the only secret. Then:

```yaml
# automations.yaml — proves the automation engine is alive, not just the container.
- id: ha_heartbeat
  alias: HA heartbeat to Kuma
  mode: single
  trigger:
    - platform: time_pattern
      minutes: "/5"
  action:
    - service: rest_command.ha_kuma_heartbeat   # GET <push-url>?status=up&msg=ok
```

An HA whose scheduler is wedged stops pushing → Kuma flips the monitor DOWN within the grace window,
and that alert path is *external* to HA, so it survives an HA that can no longer notify you itself.

### 4.2 — No "alerts are working" signal **[note]**
Same root as 1.2/4.1: if FCM tokens lapse, you lose every notification with zero indication. The Kuma
heartbeat (4.1) is the cleanest external check. Optional: a monthly "self-test" notify you eyeball.

---

## 5. Minor / polish **[note]**

- **`bedroom_fan_temperature` runs on every AirGradient temperature tick** (`automations.yaml:361`).
  Cheap (the deadband in `apply_fan` no-ops most calls and `input_number.set_value` only writes on a
  real change, so no recorder churn) — flagged only to confirm it's intentional, which it is.
- **Severe air-quality + battery + humidity thresholds are explicitly "starting points"** pending the
  ~2026-06-25 baseline pass (`configuration.yaml.j2:94-146`). Not a finding — a standing TODO already
  tracked in the config. Tune against observed baselines; the VOC/NOx *indices* especially drift.
- **`bedroom_absence_off` can clip the wake ramp** if you leave the bed (FP300 loses presence) for >1
  min mid-ramp; `bedroom_presence_on` re-lights on return. Accepted tradeoff, noted.
- **Recorder excludes** are well-chosen; UPS load/voltage + the `automation`/`script` domains remain
  the next candidates if the DB grows. No action now.

---

## 6. Infrastructure — reviewed, clean **[note]**
Second pass covered everything HA leans on. No findings; documenting what was checked so it's not
re-litigated:
- **Zigbee2MQTT** (`2.12.0`, pinned): `cap_drop ALL`, runs as `1000:1000`, network coordinator over
  `tcp://<slzb>:6638` (no host net/USB passthrough), **pinned network identity** (key/pan_id from
  SOPS/host_vars — can't regenerate and un-pair), availability tracking on, coordinator monitored at
  the infra level *and* in HA (`zigbee_bridge_offline`). Solid.
- **Mosquitto** (`:2`): `allow_anonymous false` + password file, **port 1883 not host-published**
  (reachable only on the `mqtt` isolation net), runs as `1000:1000`. Solid.
- **NUT / peanut**: HA's NUT integration logs in as the **read-only `[homeassistant]` upsd user** —
  no upsmon role, no instcmds, **cannot raise FSD** (clean least-privilege over the `ups` net). Host
  shutdown is correctly two-tier (container raises FSD → host-side `secondary` upsmon powers off), so
  a compromised container can't power-cut the host.
- **Backup consistency**: `containers/.kopiaignore` backs up `home-assistant_v2.db` and `.storage/`
  while excluding only `*.db-wal`/`*.db-shm`. That's the *right* call — a restore lands the recorder
  DB at its last SQLite checkpoint (consistent, at worst a few minutes stale), not a torn WAL state.
- **Runtime health**: live mem 824 MiB / 1.5 GiB (53%, matches the documented idle); `restarts=0`;
  HA logs carry only the documented-benign warnings (custom-integration "not tested", the SQLite
  unclean-shutdown WAL recovery, an upstream `rich` SyntaxWarning) — **no template/automation errors**.
- **Internet-facing security chain (verified end-to-end)**: HA is the one externally-reachable
  service with Authelia deliberately off, so I traced its full middleware chain. The shared `labels()`
  macro gives a `use_authelia:false` route only `rate-limit@file` per-router — but CrowdSec is applied
  **entrypoint-wide on `https`** (`crowdsec@file`, traefik static `traefik.yml.j2:40`) along with
  `default-headers@file`, so HA *is* behind the WAF + security headers regardless. Net layered defense:
  Cloudflare → Traefik TLS → CrowdSec WAF → default security headers → per-router rate-limit → HA's own
  login (TOTP + `ip_ban` after 5 fails). The docs' "still gets CrowdSec + rate-limiting" claim is
  **accurate**. No change.
- **Minor [note], no action**: `brandawg93/peanut:latest` is unpinned, but it's a non-critical UI —
  the actual shutdown logic is the host-side upsmon, independent of PeaNUT — so a breaking update
  degrades only the dashboard. The MQTT password is rendered in plaintext into Z2M's Kopia-backed
  `data/configuration.yaml`; inherent to Z2M and acceptable for an internal-only broker.

## Recommended order of operations
1. **[done]** 1.1 wake-window masking fix — deploy with `--tags home-assistant`, then set a 05:00-ish
   test alarm on the watch and confirm the ramp (not the nightlight) runs.
2. **1.2 / 4.1** notify resilience + Kuma heartbeat — highest safety-per-effort; closes the "silent
   dead pipeline" hole.
3. **2.1** AL startup-suppression automation — kills a known daily annoyance; verify the delay first.
4. **3.1** decide consciously whether to YAML-ify `light.bedroom_lights`; ensure `.storage` is in a
   tested Kopia restore.
5. Everything else is **note** — confirm-intentional, no change required.
