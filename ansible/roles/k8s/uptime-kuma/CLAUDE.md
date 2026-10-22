# uptime-kuma — status monitoring, with AutoKuma reconciling monitors from templates

Uptime Kuma plus an AutoKuma sidecar that creates monitors and notifications from this role's
rendered declarations. `docs/uptime-kuma-autokuma-record.md` carries the incident and source
reading behind each rule below.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=uptime-kuma` with exit 5
- **Images:** `louislam/uptime-kuma` (`uptime_kuma_k8s_image`), `ghcr.io/bigboot/autokuma`
  (`uptime_kuma_k8s_autokuma_image`), `ghcr.io/bigboot/kuma` (`uptime_kuma_k8s_cli_image`),
  `python` (`uptime_kuma_k8s_status_page_sync_image`)
- **Route:** `uptime-kuma.<domain>` · `uptime-kuma.local.<domain>`, Authelia one_factor
- **Claims:** `uptime-kuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes)),
  `autokuma-data` (no backup (listed in k3s_longhorn_nobackup_volumes))
<!-- /generated_from -->

- **Both claims are in the no-backup tier** — monitors and notifications regenerate from the
  rendered static-monitors Secret; status history is kept nowhere.

## Traps

- **Check a field exists on the pinned monitor variant before believing it is live.** AutoKuma's
  serde model drops an unknown field silently — that is how 2.0.0 discarded `resendInterval` on
  all 50 push tiles behind a correct template. ENFORCED:
  `ansible/tests/services/test_kuma_static_monitors.py::test_autokuma_pin_carries_resend_interval_on_push_monitors`.
- **`resendInterval` counts DOWN beats, not minutes** — `uptime_kuma_k8s_push_resend_down_beats`, 90, six
  hours for a bridge-fed tile and something else at any other cadence.
- **Every notification declaration carries `applyExisting`**, the key Kuma forces into the stored
  config: one key short of what AutoKuma compares means a rewrite on every sync pass. Never chase
  such a loop with AutoKuma's debug diff, which writes the webhook and SMTP password into Loki.
- **Do not add a readinessProbe to the autokuma sidecar, or restore its startupProbe** — either
  holds the whole pod `Ready = false` through the first reconcile. The allowance is
  `livenessProbe.initialDelaySeconds: 300`.
- **The pod's `hostAliases` come from the shared `ingress_host_aliases()` macro**
  (`ansible/templates/ingress-host-aliases.yml.j2`), which blackbox-exporter calls too, so both
  pods pin every ingress name to the ingress VIP. Change the pinned names there.
- **A push tile's `interval` is a deadline, not a poll rate, so recovery time is set on the
  producer**: a `kuma-check-<name>` timer's script exits 1 after a `down` push and
  `Restart=on-failure` reruns it. The `DECIDED:` marker above the drift tiles in
  `templates/static-monitors.yaml.j2` keeps those deadlines. ENFORCED:
  `ansible/tests/setup/test_kuma_check_timer.py`.
- **Both declaration checks the Ansible deploy ran have a Flux form (#5712).** The index
  check is `tests/test_declaration_index_flux_form.py`, which runs the assert's `that:` over
  the Secret the renderer emits. The push-token check's is the
  `_push_token` refusal in `scripts/secrets_mgmt/cluster_vars.py`, because the render
  carries placeholder tokens and only `cluster_vars.py write` reads the real ones. The
  `autokuma-claims` staging-directory absent arm moved to
  `ansible/roles/setup/k8s_host_files/tasks/uptime-kuma.yml`.

## One Discord template for every monitor, fed by `description` and tags

The `discord` notification is Kuma's **webhook** provider, not its `discord` one — only that
provider posts `files/discord-message.liquid` verbatim, embed included, and `email.json` is the
same convention in plain text. Both read `heartbeatJSON.msg`, `monitorJSON.description` and the
`severity` and `runbook` tags, so **a tile whose name does not say what it means declares a
`description`** in `static-monitors.yaml.j2`: what it reads, what a DOWN means, where to look.

- **A Liquid body ships inside a Tera `raw` block**, because AutoKuma runs every entity through
  Tera first and Tera claims Liquid's delimiters; the first deploy without the wrapper detached
  every alert from every monitor behind green tiles. ENFORCED:
  `ansible/tests/services/test_kuma_entities_parse_for_autokuma.py`.
- **`ON_DELETE=delete` stays** (decided 2026-09-19, #2076; the `DECIDED:` marker sits at the
  variable in `templates/deployment.yaml.j2`), so an unparseable entity is a deleted one and the
  guards on the declarations stand between a typo and a fleet wipe.
- **A monitor names a tag by the entity's AutoKuma id** — `tag-severity`, the filename minus
  `.json` — never the display name. Removing one takes two deploys: references first.
- **`webhookAdditionalHeaders` carries `Content-Type: application/json`**, the id stays `discord`,
  and the name stays `Homelab Alerts`, which Kuma prints in its failure line. The loki-homelab rule `KumaNotificationDropped` filters on that line's text and names no notification.

## The status page's groups are synced by a CronJob, not by AutoKuma

`kuma-status-page-sync` (every 15 min) owns the `publicGroupList` of the page Homepage's
`uptimekuma` widget reads. The page OBJECT is hand-created, and stays that way.

- **Do not declare it as an AutoKuma `status_page` entity**, though the type exists: AutoKuma
  has no update arm for one, so a drifted page writes nothing — and under
  `ON_DELETE=delete` a declaration that went away would delete it. ENFORCED:
  `ansible/roles/k8s/uptime-kuma/tests/test_status_page_groups.py::test_no_status_page_is_declared_as_an_autokuma_entity`.
- **A tile aimed at a path-only route uses a hostname no IngressRoute claims**, because Traefik
  ranks routers by rule length — hence `edge-selfcheck.local.<domain>`.
- **A new monitor needs a group rule** (`test_every_declared_monitor_lands_in_a_named_group`);
  a bridge row has one.
- **No tile probes a service's bare ingress URL.** blackbox-exporter's `blackbox-ingress` job
  probes one per routed `containers_list` entry, and the `ingress-probes` rules alert on it
  (`docs/uptime-kuma-autokuma-record.md`, *The ingress tiles moved to blackbox-exporter*). A
  tile here probes something else: a path, a VIP, a ClusterIP or another status.

## The weekly reboot's maintenance window is reconciled over the API

`kuma-maintenance-sync` (hourly) declares ONE Kuma maintenance — `Weekly system restart` — over
every monitor, so the Sunday reboot stops paging Discord for the tiles it takes down (#2802).
The operator accepted the cost on 2026-09-28: an outage in the window is silent.

- **Derived from the reboot cron, never written twice** — the four `weekly_reboot_*` values in
  `group_vars/all.yml` drive both the cron and this window: `25 7 * * 0`, 50 minutes. The lead
  and the recovery allowance sit beside them as
  `ansible/inventory/group_vars/all.yml:weekly_reboot_mute_lead_minutes` and
  `ansible/inventory/group_vars/all.yml:weekly_reboot_mute_recovery_minutes`, because
  Alertmanager's `weekly-reboot` mute in the observability role reads the same six values.
  ENFORCED:
  `ansible/tests/services/test_alertmanager_reboot_mute.py::test_the_mute_equals_kumas_window_at_the_committed_schedule`.
- **The timezone cannot be spelled `UTC`.** The clock is the host's, and kuma-client's own list
  carries neither `UTC` nor `Etc/UTC`, so `uptime_kuma_k8s_maintenance_window_timezone` is
  `Atlantic/Reykjavik`. In `tz` the window opens five hours late.
- **The sync must not run inside the window it declares**, because an `edit` restarts the
  window's own cron job and can lift the suppression mid-window. ENFORCED:
  `ansible/roles/k8s/uptime-kuma/tests/test_maintenance_window.py::test_the_sync_never_runs_inside_the_window_it_declares`,
  which also fails if either side stops reading those values.
- **The 07:20 run drops an already-DOWN monitor from the window; 08:20 restores it**, as
  Kuma pages `MAINTENANCE -> DOWN` (#3506). ENFORCED:
  `ansible/roles/k8s/uptime-kuma/tests/test_maintenance_window.py::test_exactly_one_sync_run_a_week_leaves_down_monitors_out`.
