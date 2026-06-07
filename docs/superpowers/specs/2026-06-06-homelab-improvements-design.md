# Homelab Improvements — Design (2026-06-06)

Five non-security enhancements selected from a server review. Each is independent and
small; they share one implementation plan but touch disjoint files.

| # | Item | Net effect |
|---|------|-----------|
| 1 | Grafana provisioning as code | Datasources + seed dashboards become reproducible/version-controlled |
| 2 | Container/proxy alerts via monitor-bridge | Alert on signals Prometheus already scrapes but nothing acts on |
| 3 | toposort unit tests | Lock in correctness of the deploy-ordering resolver |
| 5 | Renovate (pinned images only) | Automated PRs for version-pinned images Watchtower can't update |
| 7 | Polish | Root README + remove redundant speedtest logging block |

(#4 — GitHub Actions CI — was explicitly out of scope. Test enforcement is handled by a
local `prek` hook instead; see #3.)

---

## #1 — Grafana provisioning as code

**Problem.** Everything in the stack is IaC except Grafana's datasources and dashboards,
which live only inside the `./data` bind mount. They're backed up by Kopia (no data-loss
risk) but are not reproducible, reviewable, or version-controlled.

**Approach.** Provision datasources and a curated set of dashboards via Grafana's native
file-provisioning, bind-mounted read-only.

**New files**
- `ansible/roles/containers/grafana/templates/provisioning/datasources.yml.j2`
  - Prometheus — `uid: prometheus`, `url: http://prometheus:9090`, `isDefault: true`
  - Loki — `uid: loki`, `url: http://loki:3100`
- `ansible/roles/containers/grafana/templates/provisioning/dashboards.yml.j2`
  - One file provider pointing at `/var/lib/grafana/dashboards`, `foldersFromFilesStructure: true`,
    `allowUiUpdates: true` (dashboards seeded from files but remain editable in the UI; the
    file only re-seeds when its `version` is bumped). Datasources set `editable: true`.
- `ansible/roles/containers/grafana/files/dashboards/node-exporter-full.json` (grafana.com 1860)
- `ansible/roles/containers/grafana/files/dashboards/cadvisor.json` (grafana.com 14282, "Cadvisor exporter"; swap for an equivalent if it fails to render against current cAdvisor metrics)
- `ansible/roles/containers/grafana/files/dashboards/traefik.json` (Traefik official, 17346)
  - All three: datasource references pinned to uids `prometheus` / `loki` (strip the
    `__inputs` / `${DS_*}` templating so they resolve against the provisioned datasources).

**Edits**
- `grafana/templates/docker-compose.yml.j2` — add three read-only bind mounts:
  - `./provisioning/datasources.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro`
  - `./provisioning/dashboards.yml:/etc/grafana/provisioning/dashboards/dashboards.yml:ro`
  - `./dashboards:/var/lib/grafana/dashboards:ro`
- `grafana/tasks/main.yml` — template the two provisioning YAMLs and copy the dashboard
  JSON dir, mirroring how `loki-config.yaml` / `promtail-config.yaml` are already deployed.

**Notes.** Provisioned datasources use fixed uids, so if Prometheus/Loki were already added
by hand they're adopted by uid rather than duplicated. Dashboards are managed (read-only in
the UI), which is the intended trade-off for reproducibility.

---

## #2 — Container/proxy alerts via monitor-bridge

**Problem.** Prometheus scrapes `node-exporter`, `cadvisor`, `traefik`, `crowdsec`, but
there are no alert rules. Per-service "is it up" is already covered by AutoKuma monitors;
the gap is container-level and proxy-level signals.

**Approach.** Add four checks to `monitor-bridge/files/check.py` following the existing
`(ok, msg)` → Uptime Kuma push-monitor pattern. Each names the offender.

**New pure helper**
- `prom_vector(promql)` — instant query returning `[(labels: dict, value: float), ...]`,
  so checks can name *which* container/target/route is failing (the existing `prom_scalar`
  discards labels). Unit-tested like the other parsing helpers.

**New checks**
| Check | Query (env-tunable) | Down when |
|-------|--------------------|-----------|
| `check_restarts` | `changes(container_start_time_seconds{name!=""}[RESTART_WINDOW])` | any container `> RESTART_MAX` (default window `15m`, max `3`) |
| `check_oom` | `increase(container_oom_events_total{name!=""}[OOM_WINDOW]) by (name)` | any container `>= 1`; message names it |
| `check_targets_down` | `up == 0` | any target down; message names job/instance |
| `check_traefik_5xx` | `sum(rate(traefik_service_requests_total{code=~"5.."}[5m])) / sum(rate(traefik_service_requests_total[5m]))` | ratio `> TRAEFIK_5XX_PCT` (default 5%) **and** total rate `>= TRAEFIK_MIN_RPS` (volume floor; low traffic never trips) |

**Decision — OOM dedup (approved).** Remove the OOM clause from `check_mem`; rename its
push monitor `Memory / OOM` → `Memory`. OOM now lives only in `check_oom`, which names the
container. Single source of truth. `check_mem`'s test is updated to drop the OOM assertion.

**Wiring**
- `monitor-bridge/templates/docker-compose.yml.j2`:
  - new env: `RESTART_WINDOW=15m`, `RESTART_MAX=3`, `TRAEFIK_5XX_PCT=5`, `TRAEFIK_MIN_RPS=0.05`,
    and 4 push-token vars
  - 4 new AutoKuma `push` monitor labels via the `kuma(...)` macro:
    `monitor-bridge-restarts` (Container Restarts), `monitor-bridge-oom` (Container OOM),
    `monitor-bridge-targets` (Scrape Targets), `monitor-bridge-traefik` (Traefik 5xx)
- `ansible/vars/secrets.yml` (via `sops`): 4 new push tokens
  `monitor_bridge_restarts_push_token`, `monitor_bridge_oom_push_token`,
  `monitor_bridge_targets_push_token`, `monitor_bridge_traefik_push_token`
- `CHECKS` list in `check.py` extended with the four new entries

**Tests.** Extend `monitor-bridge/files/test_check.py`: threshold boundaries, empty-result
graceful skip (returns OK / no false alert), offender-name extraction via `prom_vector`,
the 5xx volume floor (high ratio but sub-floor traffic ⇒ OK), and `check_mem` no longer
references OOM.

---

## #3 — toposort unit tests + enforcement

**Problem.** `ansible/filter_plugins/toposort.py` (the four filters that order and scope
*every* deploy) has no tests, while the comparable `monitor-bridge` Python does.

**New file**
- `ansible/filter_plugins/test_toposort.py` (pytest):
  - `toposort_containers` — linear chain, diamond, stable tie-ordering (ties keep original
    list order), cycle ⇒ raises `AnsibleFilterError`, deps absent from list are ignored
  - `build_dep_map` — reads `meta/deps.yml` fixtures from a `tmp_path`; full (`all`) vs
    tagged-closure modes; missing/malformed deps.yml ⇒ empty list
  - `dep_closure` — returns transitive deps, excludes the directly-requested nodes
  - `expand_with_deps` — includes unmet (not-running) deps, always includes requested,
    skips already-running deps, returns topo order

**Decision — enforcement (approved).** Add a `prek` local hook (mirroring
`validate-compose-templates`) that runs pytest over both `ansible/filter_plugins/` and
`monitor-bridge/files/`. The existing `test_check.py` is currently never run; this fixes
that too.

**Edit**
- `prek.toml` — new local hook `id: pytest`, `language: python`,
  `additional_dependencies: ["pytest", "ansible-core", "pyyaml"]` (ansible-core needed
  because the filter module imports `ansible.errors`), `pass_filenames: false`,
  `files` scoped to the two test dirs.

---

## #5 — Renovate (pinned images only)

**Problem.** Watchtower auto-updates mutable `:latest` tags, but version-pinned images
never update and silently drift: `influxdb:2.2`, `meilisearch:v1.37.0`, `cadvisor:v0.53.0`,
`alpine-chrome:124`, `couchdb:3`, `uptime-kuma:2`.

**New file**
- `.github/renovate.json` (`.github` is allowlisted by `.gitignore`):
  - `extends`: `["config:recommended", ":dependencyDashboard"]`
  - `customManagers` (regex) over
    `^ansible/roles/containers/.*/templates/docker-compose\.yml\.j2$`, matching
    `image:\s*(?<depName>...):(?<currentValue>...)`, `datasourceTemplate: docker`
  - `packageRules`: disable updates when `currentValue` is `latest` (and the non-semver
    mutable tags like `release`, `jvm-stable`, `master-web` — Renovate skips these
    naturally as non-versioned); group minor/patch; weekly schedule; `labels: ["dependencies"]`

**Out-of-band step.** User installs the Renovate GitHub App on the repo once. Documented in
the README and noted at hand-off; cannot be automated from here.

---

## #7 — Polish

**README.md** (new, repo root, ~1 page, architecture-focused):
- Overview + the two hosts
- Network-segmentation diagram (mermaid): `proxy` / `apps` / `media` / `monitoring` /
  `lifecycle` / `kopia` / `homepage_private`, and where the cross-cutting services sit
- Authelia SSO flow (Traefik forward-auth)
- The toposort deploy-dependency system (deps.yml → build_dep_map → toposort → closure)
- SOPS/age secrets, observability (Prometheus/Grafana/Loki/Uptime-Kuma/monitor-bridge),
  Kopia backups, update strategy (Watchtower for `:latest` + Renovate for pinned)
- Common commands
- **`.gitignore` edit:** add `!/README.md` to the allowlist (the `/*` deny rule blocks it otherwise)

**speedtest** — remove the redundant per-compose `logging:` block from
`speedtest/templates/docker-compose.yml.j2`; global `daemon.json` rotation (10m × 3)
supersedes it.

---

## Testing summary

- `prek run --all-files` green (yaml, ansible-lint, gitleaks, template validation, **new pytest hook**)
- New pytest suites pass: `test_toposort.py`, extended `test_check.py`
- `ansible-playbook ansible/deploy.yml --tags grafana --check` parses; rendered compose is valid YAML
- `ansible-playbook ansible/deploy.yml --tags monitor-bridge --check` parses
- Manual post-deploy: Grafana shows two datasources + three dashboards; four new Uptime
  Kuma push monitors appear and heartbeat

## Out of scope

- GitHub Actions CI (#4), Alertmanager (push-to-Kuma is the chosen alerting surface),
  pinning `:latest` images (Watchtower trade-off accepted), exporting existing hand-built
  Grafana dashboards (seeding community boards instead).

---

## Implementation notes / deviations

What actually shipped differs from the design above in places — mostly fixes that surfaced
during post-deploy verification. Recorded here so the spec matches reality.

### #1 Grafana
- **Datasource uids were adopted, not invented.** The design said uid `prometheus`/`loki`;
  in practice Grafana already had hand-made datasources (`Prometheus`=`EGdsQqhVk`,
  `loki`=`bf4q19tuivta8e`) that 9 existing dashboards reference. Imposing new uids crashed
  Grafana (`data source not found`) / would have orphaned those dashboards, so provisioning
  **adopts the existing uids** and matches the existing names exactly (`loki` stays
  lowercase — Grafana keys update-vs-insert on `orgId+name`).
- **Added `jsonData.timeInterval: "1m"`** to the Prometheus datasource. The node job scrapes
  every 1m; without telling Grafana, it defaulted to 15s and computed `$__rate_interval ≈ 1m`,
  so every `rate()`/`increase()` panel was empty over short time ranges. This was *the* fix
  for "CPU Basic / Network / Pressure show No data."
- **Seed dashboards are generated, with deviations** (`scripts/fetch_grafana_dashboards.py`):
  template variables get baked working defaults (single-select query vars → first live value,
  e.g. `node=daniel-server`; `includeAll` vars → `All`) so panels render on first load without
  manual dropdown selection; and 7 always-empty panels are dropped (no NIC link-speed, battery,
  or fan sensors on this host; systemd collector not enabled).

### #2 monitor-bridge
- OOM dedup done as designed (`check_mem` → `Memory`, new named `check_oom`).

### #3 tests
- Tests live in **`ansible/tests/`**, not `ansible/filter_plugins/` — Ansible imports every
  `.py` under `filter_plugins/` (recursively) as a plugin, so a pytest file there logs a load
  warning on every deploy. `conftest.py` puts `filter_plugins/` on `sys.path`.

### New fixes not in the original design (found during verification)
- **Prometheus node relabel** (`prometheus.yml.j2`): the existing `metric_relabel_configs`
  sourced `instance` from the `nodename` label, which only exists on `node_uname_info` — so it
  **stripped `instance` off every other node metric** and broke all node dashboards. Replaced
  with a static `instance: daniel-server`. Also enabled the `processes`/`interrupts`/`tcpstat`
  node-exporter collectors (back several Node Exporter Full panels).
- **CrowdSec dashboards**: were empty because the CrowdSec engine (in the `traefik` role) was
  on `proxy` only, so Prometheus (on `monitoring`) couldn't scrape `crowdsec:6060`. Joined the
  engine to `monitoring`. (Metrics were already bound `0.0.0.0:6060`.)

### Not captured as code (Grafana DB only, Kopia-backed)
These were applied to the live Grafana via its API and are **not** in the repo (the affected
dashboards are pre-existing, hand-made, never provisioned):
- Loki dashboards (System Logs, Docker App Logs): removed `| logfmt` (parse errors on
  non-logfmt logs), converted the template-variable datasource from the legacy string form to
  the object form (it was mis-resolving to the default Prometheus datasource), set valid
  defaults (`app=syslog`, `container=sonarr`).
- Deleted the redundant 16-panel "Node Exporter" dashboard (kept the 31-panel provisioned
  "Node Exporter Full").
