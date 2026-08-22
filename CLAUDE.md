# Server Homelab — Claude Code Context

## Project Overview
Docker + k3s homelab managed with Ansible. ~50 containerized services deployed across three hosts via infrastructure-as-code, the k3s migration **completed 2026-08-14** — `docs/k3s-migration/` is now a historical record of executed work, not a plan. Docker survives only on `daniel-pi`. (Exact count: `grep -c '^  - name:' ansible/inventory/host_vars/*.yml` — don't hand-maintain a precise number here.)

**Hosts:**
- `daniel-box` — k3s server / control-plane node (Traefik edge, Authelia+OIDC, Pi-hole DNS, Longhorn storage, most workloads since 2026-08 migration). Ansible runs on this host.
- `daniel-server` — k3s agent node (Intel XE graphics, LVM storage, UPS hardware + the
  `nut_host` shutdown chain; Docker uninstalled 2026-08-14 — the migration's end state)
- `daniel-pi` — Raspberry Pi, the **only** remaining Docker host (LAN-only: wg-easy, glances,
  dozzle, autoheal, docker-proxy). Driven remotely over SSH with `-e target=daniel-pi`.

**Key technologies:** k3s (Kubernetes), Ansible, Docker Compose (Pi only), Traefik (reverse proxy), Cloudflare DNS, Authelia (SSO), SOPS/age (secret encryption), Longhorn (storage), CrowdSec (WAF)

## Directory Structure
```
ansible/          # Ansible playbooks, roles, inventory, templates  ← EDIT HERE
  roles/k8s/        # One role per k3s workload (rendered manifests) — where most services live
  roles/containers/ # One role per Docker service (the Pi's) + the shared `common` role
    archive/        # Roles retired by the k3s migration, kept for reference
scripts/          # Python helper scripts
docs/             # Runbooks, design specs, security notes
  k3s-migration/    # Historical record of the completed Docker → k3s migration
```

> **`containers/` is not a directory in this repo** — it is untracked and rendered by Ansible onto the *target host* at `/home/<user>/server/containers/<svc>/docker-compose.yml`. Post-migration it exists only on `daniel-pi`; neither cluster node has one. It is still read-only: edits are overwritten on the next deploy, so always modify `ansible/roles/containers/*/templates/` instead. (The `block-protected-edits` hook enforces this.)

> **`roles/containers/` is now only the Pi.** Every role there is a Docker service live on `daniel-pi` — `autoheal`, `docker-proxy`, `dozzle`, `glances`, `wg-easy` — plus the shared `common` deploy path and `archive/`. A service's config lives in the role that deploys it, on both trees: **if a k3s workload reads it, it is under `roles/k8s/<name>/`**, not across the tree boundary. (Until 2026-08-14 eleven roles here were config-only sources for a k8s counterpart; they moved into it. To revive one as a Docker service, take its Compose plumbing from git history.)
>
> **Where a k8s role's non-manifest config goes.** `roles/k8s/<name>/templates/` is for **manifests only** — `validate_k8s_manifests.py` renders every `*.j2` there and parses it as YAML. App config a manifest embeds via `lookup()` goes one level down in **`templates/config/`** (CouchDB's `local.ini`, HA's `configuration.yaml`, homepage's `custom.css` — none of them YAML manifests), and static assets go in `files/`. `Dockerfile*` is exempt and may sit in `templates/` directly; the validator skips it by name.

## Where to Look (task → start here)
Route to the source of truth by what you're doing, before reading linearly:

| If you're… | Start here |
|---|---|
| Adding / changing a service (k3s — the default) | `## Adding a New Service` below · a sibling role in `ansible/roles/k8s/` |
| Adding / changing a Docker service (the Pi only) | `## Adding a New Service` → *Adding a Docker service* · `/new-container` skill |
| Deploying or redeploying a service | `/deploy` skill · `## Common Commands` |
| Checking a k8s manifest change without deploying it | `## Common Commands` → *Checking a k8s change without deploying it* (`--dry-run` vs `--check` — they check different things) |
| Running or testing a GitOps tick without waiting 30 min | `./scripts/gitops_tick.sh` · `ansible/roles/setup/gitops_deploy/CLAUDE.md` → *Triggering a tick by hand*. A real tick, not a rehearsal — there is no dry-run mode. |
| Adding / rotating a secret | `/add-secret` skill · `docs/secret-rotation.md` · `## Secrets Management` |
| A Bash or `kubectl` command keeps prompting, or you need the full permission tables | `## Shell Commands — Shape Them to Auto-Approve` below (summary) · `docs/claude-shell-permissions.md` (full detail) |
| Editing HA automations / lighting / fans | `ansible/roles/k8s/home-assistant/CLAUDE.md` (config and workload both live there; it routes to `docs/` for per-topic behaviour) · `/ha-edit-automation` |
| Reviewing the homelab for gaps | `/homelab-review` skill (per-domain reviewer agents) |
| Chasing a reliability / monitoring "gap" | The role's `CLAUDE.md` + monitor-bridge `check.py` **first** — mature setup, most are handled |
| A config edit won't restart the pod (k3s) | A ConfigMap/Secret change alone doesn't roll a Deployment. The general mechanism is the central rollout-restart at `roles/k8s/manifests/tasks/main.yml:112`, which fires when a role's rendered manifests change. A role whose pod depends on a file the manifests *don't* carry adds its own `checksum/<thing>` pod annotation instead — e.g. `checksum/check-script` in `roles/k8s/monitor-bridge/templates/deployment.yaml.j2`. |
| A config edit won't recreate the container (Docker) | `ansible/roles/containers/common/CLAUDE.md` (config-change wiring) |
| A host can't decrypt secrets | `## Secrets Management` → *Onboarding a host to SOPS* |
| Adding / changing a cron that changes state | that role's `CLAUDE.md` *Autonomous-role contract* |

## Adding a New Service

**Default to k3s.** A new service belongs in `ansible/roles/k8s/<name>/` unless it must run
on `daniel-pi` (LAN-only utilities, WireGuard) — `daniel-server` and `daniel-box` have no
Docker at all, so a Compose role there deploys nothing.

1. Create `ansible/roles/k8s/<name>/tasks/main.yml` plus the manifest templates it needs
   (`deployment.yaml.j2`, `service.yaml.j2`, `ingressroute.yaml.j2`, `pvc.yaml.j2`).
   Copy the shape from a close sibling — `roles/k8s/freshrss` for a plain web app,
   `roles/k8s/sonarr` for one on the media volume.
2. Add the service to `containers_list` in `ansible/inventory/host_vars/daniel-box.yml` with
   `platform: k8s`. **Position matters**: that play has no toposort and runs in list order,
   so place the entry *after* `traefik` (which installs the CRDs its IngressRoute needs) and
   after `authelia` if the route uses the `authelia` middleware.
3. Secrets: add to `ansible/vars/secrets.yml` (`sops ansible/vars/secrets.yml`), reference as
   `{{ variable_name }}` in a `secret.yaml.j2`. Note `kubectl apply` leaves *stale* Secret
   keys behind — removing a key from the manifest does not remove it live.
4. Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "<name>"`.

### Adding a Docker service (daniel-pi only)
1. Create `ansible/roles/containers/<name>/tasks/main.yml`
2. Add a `docker-compose.yml.j2` template in `ansible/roles/containers/<name>/templates/`.
   Use the shared macros in `ansible/templates/` rather than hand-rolling boilerplate:
   `traefik.yml.j2` (`labels`), `autokuma.yml.j2` (`kuma`), `healthcheck.yml.j2`
   (`healthcheck`), `networks.yml.j2` (`service_networks()` / `external_networks()` —
   the per-service and top-level `networks:` blocks), and `resources.yml.j2`
   (`resources(cpu_limit, mem_limit, cpu_res, mem_res)` — the `deploy.resources` caps).
   The `/new-container` skill has the canonical skeleton.
3. Add the service to `containers_list` in `ansible/inventory/host_vars/daniel-pi.yml`
   (`name`, `port` if web-facing, `use_authelia`, `networks`). Deploy tags derive from
   `name` automatically — `deploy.yml` needs no edit.
4. Add any secrets to `ansible/vars/secrets.yml` (edit with `sops ansible/vars/secrets.yml`)
5. Reference secrets via `{{ variable_name }}` in templates
6. **If the service bind-mounts an Ansible-templated config file:** `register:` each config
   task with a `<role>_`-prefixed name and pass `common_config_changed: "{{ <reg> is changed }}"`
   (OR several) on the `common`/`docker_deploy` include. Deploys are idempotent (`recreate: auto`
   by default), so without this an edit to that config won't recreate the container. See
   `ansible/roles/containers/common/CLAUDE.md`.

## Common Commands
Run ansible through `uv run` so it uses the repo's pinned env (`ansible-core` + the
`community.docker` deps `requests`/`docker` — see **Python & Tests**). Bare `ansible-playbook`
(the uv-tool shim) lacks those module deps and deploys will fail.
Deploy through `scripts/deploy.sh`. It takes `/var/lock/server-git-tree.lock` — the same
lock `gitops-deploy.service` (30-min timer) and the weekly secret-rotate cron hold — so a
deploy can't interleave with the automated pipeline or with another Claude session. The
lock guards the local git tree every deploy reads its templates from (gitops-deploy
rewrites it with a `git pull` mid-run), so a `-e target=daniel-pi` deploy takes it too. Exit
**75** means the lock stayed busy and *nothing was deployed*; it is not a playbook failure.
`--check` runs unlocked. Exit **2** means a `--tags` value matched no service and *nothing
was deployed* — Ansible itself exits 0 on an unmatched tag, so the wrapper checks the tags
against `containers_list` first (`scripts/deploy_tags.py`); `--list-services` prints every
valid value and `--skip-tag-check` bypasses. Exit **4** means the tree is behind
`origin/master` and *nothing was deployed* — a stale tree renders stale templates and reverts
live config while every repo-side check still reads green (`scripts/deploy_staleness.py`);
`--skip-staleness-check` bypasses it. That check runs ahead of `--check` and `--dry-run` too,
because a green dry run against a stale tree is itself the misleading signal; being *ahead* of
master is normal branch work and is never refused. `--dry-run` validates the k8s manifests against
the live API server without applying them, and runs unlocked because it mutates nothing —
see *Checking a k8s change without deploying it* below. The bare `ansible-playbook` forms
below still work and are what the wrapper runs, but they have neither the lock, the tag
check, nor the staleness check; use them only when you deliberately want that.

```bash
# Deploy a specific container
./scripts/deploy.sh --tags "<service-name>"

# Target the Pi from the server (NB: -e target=, NOT --limit — the play's hosts:
# defaults to the local hostname, so --limit daniel-pi matches zero hosts)
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" -e target=daniel-pi

# Deploy all containers
uv run ansible-playbook ansible/deploy.yml

# Dry run
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --check

# Validate a k8s change against the live API server without applying it
./scripts/deploy.sh --tags "<service-name>" --dry-run

# Config-only: render dirs/templates/host config WITHOUT touching the container
# (every container-role task is block-tagged config/deploy/cron; tags union in
# Ansible, so scope with --skip-tags. --skip-tags config is NOT supported — the
# registered config-change facts feed docker_deploy's recreate decision.)
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --skip-tags deploy

# Edit encrypted secrets
sops ansible/vars/secrets.yml

# List the services --dry-run refuses to cover (k8s_dry_run_unsupported)
grep -A20 "^k8s_dry_run_unsupported:" ansible/inventory/group_vars/all.yml

# Trigger a GitOps tick now instead of waiting for the 30-min timer (daniel-box only).
# Runs the identical code path the timer runs — there is no dry-run mode.
./scripts/gitops_tick.sh

# Initial server setup — first-host bring-up ORDER (uv → SOPS onboarding → this) is in ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml
```

### Checking a k8s change without deploying it
Three modes, and they check genuinely different things — reaching for the wrong one is how a
manifest bug reaches production.

| Mode | What sees the manifests | Catches |
|---|---|---|
| `prek run --all-files` | nothing (renders locally, then parses and schema-checks) | Jinja indent bugs, invalid YAML, duplicate keys, **undefined fields and wrong types** — everything but CRDs, which have no upstream schema |
| `--check` | nothing — the apply is **skipped**, so no API server is involved | task-level wiring; not the manifests themselves |
| `--dry-run` | the **live API server**, via `kubectl apply --dry-run=server` | what prek catches, plus **CRD** schemas, CRD-ordering mistakes and admission rejections |

`--dry-run` renders to a temp dir, applies with `--dry-run=server`, and discards the temp dir.
Nothing is staged, applied, patched or rolled. It does **not** catch scheduling, PVC binding,
probe or rollout behaviour — those need a real deploy.

Two limits worth knowing before you trust a green dry run:
- **It refuses the roles named in `k8s_dry_run_unsupported`** (count it with the grep two
  sections above — don't hand-maintain the number here; it read "~17" against a real 15 for two
  commits). Roles that mutate outside `roles/k8s/manifests` (sidecar
  ConfigMaps built with `kubectl create`, netpol-probe Jobs, `exec -i` into a live pod) would
  half-apply, so `deploy.yml` fails fast and names them. `k8s_dry_run_unsupported` in
  `group_vars/all.yml` is the list; `ansible/tests/test_k8s_dry_run.py` re-derives it from the
  role sources so it cannot drift.
- **A brand-new service is only half-checked.** `seed-volume` is skipped (it is a dependency of
  25 roles and mutates), and nothing at admission verifies that a referenced PVC exists — so
  the Deployment validates while the volume is never proven provisionable.

## Shell Commands — Shape Them to Auto-Approve
Write exploratory commands so they auto-approve; expect a prompt for the rest.

- **Auto-approves:** read-only single commands and pipelines (`grep … | sort | head`), read-only
  stages sequenced with `;`, `&&`, `||` or newlines, write-free redirects, and those same
  read-only commands run over `ssh daniel-server` / `ssh daniel-pi`.
- **Forces a prompt:** command substitution — `$(…)`, backticks, `${…}` — rejected outright;
  shell control flow (`for`/`while` loops, `if/then/else/fi`); anything that writes or execs
  (`> file`, `tee`, `sed -i`, subshells `(…)`, backgrounding `&`).
- Restructure rather than loop: one `grep`/`find`/`awk` usually replaces the control flow.

- **`./scripts/gitops_tick.sh` is allow-listed but not guaranteed.** It is a write (it triggers
  a real deploy), so the auto-mode classifier judges it on its own and denied it once in seven
  runs on identical text. Measured 2026-08-22. A denial here is the classifier, not a broken
  script or a missing polkit rule — re-run it, and check `last_run` before assuming nothing
  happened. `Bash()` allow rules are suspended while `autoMode.classifyAllShell` is on, so the
  allow-list entry helps only outside auto mode.

Full tables, hook wiring and measurement history: `docs/claude-shell-permissions.md`.

### `kubectl` — what actually decides
- The **classifier judges the whole command text** in a normal session
  (`autoMode.classifyAllShell: true`), which suspends every `Bash()` allow rule — the per-verb
  allow-list decides nothing.
- Plain `kubectl` authenticates as a **read-only ServiceAccount**, so RBAC refuses every write
  verb. `sudo` is in `permissions.deny` (so `sudo k3s kubectl` is blocked, not prompted), and
  `kubectl delete` is denied outright.
- Therefore **Ansible is the only write path to this cluster** — prefer
  `uv run ansible-playbook … --tags <svc>`.

Per-verb tiers, the RBAC evidence and the rule-matching measurements: `docs/claude-shell-permissions.md`.

## Claude Tooling in This Repo (`.claude/`)
- **`scripts/probe.py`** — read-only homelab diagnostics, allow-listed (no prompt). Resolves the
  live container IP via `docker inspect`, so prefer it over curling bridge IPs (which change on
  recreate): `uv run python scripts/probe.py <targets | metric '<promql>' | loki-query '<logql>' |
  alerts | monitors | kuma-drift | scrutiny | pi <path> | cert <host> | health <svc> |
  ha <state|automation|get> …>`.
  `alerts [--days N --check X]` reconstructs DOWN alert history from Loki (Kuma keeps only
  current state) — one row per firing episode; the same view is the "Alert History" Grafana
  board (Infrastructure folder). It reads **two** streams: monitor-bridge's container log, and
  the `{job="syslog"}` `status=down` lines the host crons emit, which push Kuma directly and so
  have no other durable record. Until 2026-08-22 it read only the first, and the whole
  backup/drift plane left no episode anywhere. `monitors` answers "what is down"; **`kuma-drift`
  answers "what is missing"**, which `monitors` structurally cannot — it counts the exporter's
  own set, so a monitor that is gone rather than down leaves the ratio at N/N up (a fenced-off
  push tile read green for a day on 2026-08-20). `kuma-drift` diffs that set against
  `static-monitors.yaml.j2` and treats a push monitor inside its own interval after a Kuma
  restart as pending, since Kuma exports a monitor only once it has beaten. `health <svc>` is a k8s post-deploy gate: it exits 0 only
  when the Deployment **or DaemonSet** is fully rolled out (observed generation caught up, every
  replica updated + ready + available) **and** no container restarted in the last 180s. An
  unreadable restart time counts as recent, so it fails closed. Both halves matter — readiness
  flips a Deployment to Available before a bad liveness probe starts killing it, so a rollout check
  alone reports green on a crashlooping pod. `--docker` inspects the Pi's container over ssh
  instead; that was the only mode until 2026-08-16, which is why it died with
  `FileNotFoundError: 'docker'` on both cluster nodes for the two days after the Docker
  retirement. `ha …`
  reads live Home Assistant state (authed with the SOPS `claude_ha_token`); `ha automation
  <id-or-alias>` resolves the alias-slug≠id trap. See the home-assistant role's CLAUDE.md.
- **block-protected-edits** (PreToolUse) — *denies* direct edits to (a) anything under
  `containers/` (edit the `ansible/roles/containers/<svc>/templates/` source instead) and
  (b) SOPS-encrypted files like `ansible/vars/secrets.yml` (use `sops` / the `/add-secret` skill).
- **validate-compose** (PostToolUse) — re-renders all compose templates after you edit a
  `docker-compose.yml.j2`, an `ansible/templates/*.j2` macro, or `host_vars`/`group_vars/all.yml`;
  fails on malformed YAML (catches Jinja indent bugs `ansible-lint` misses) and on an
  un-escaped `$` in a `command`/`entrypoint`/`healthcheck.test` (Compose interpolates a lone
  `$VAR`/`$(…)` at parse time — shell `$` must be doubled `$$`; legit `${VAR-…}` in
  `environment:` is not flagged).
- **permission auditing** — no longer lives here. A `log-permission` hook used to count tool calls
  and prompts into `.claude/logs/permissions.json` for `audit-permissions.py` to read; Claude Code's
  own OTEL `tool_decision` events carry that now, and name the deciding authority (`config` rule,
  `hook`, `user`) instead of leaving it inferred. Both hosts' Claude Code exports OTLP to their
  local node's hostPort (127.0.0.1:4317); since Phase F (2026-08-13) the cluster claude-otel
  collector is a DaemonSet with a loopback hostPort on every node, so both hosts reach their
  own node's collector directly — the Docker forwarder is dissolved/archived. The reader is the
  `claude-permission-audit` plugin
  (`/audit-permissions`), installed globally rather than vendored per-repo.
- **session-health** (SessionStart) — on opening a session here, prints a banner of any unhealthy/
  restarting containers + down Prometheus targets (silent when all-green; read-only, timeout-bounded).
- **homelab-network-diagnostician** agent — connectivity/DNS/Traefik/WireGuard/CrowdSec triage (read-only).
- **home-assistant-engineer** agent — read+write HA engineer (automations/scenes/scripts/macros)
  that knows the copy-not-template + tested-macro conventions and the verification traps; pairs
  with the `ha-edit-automation` / `ha-deploy` / `ha-verify-state` / `z2m-device-setting` skills.
- **`/add-secret`** skill — guided SOPS add → `secret_rotation.py sync` → commit.

## Review & Memory Hygiene (making judgment cumulative)
Two rules keep the review→memory loop from compounding noise (adapted from harness-engineering's
feedback + MLD discipline):
- **Corroborate before you promote.** A single review run's "learning" is a *candidate*, not a fact.
  Don't write a new auto-memory entry (or a don't-re-flag verdict) off one run's say-so — an
  uncorroborated learning that then gets auto-injected every session reinforces itself as an
  instruction even if it was wrong. Promote a candidate to durable memory only when a **second
  independent occurrence** confirms it, or you've checked it against real evidence (a diff, a log, a
  passing test, live `probe.py` state). Until then it stays a run-local note, not a memory file.
- **Escalate a recurring finding to the smallest durable owner — don't just grow the ledger.** When
  the same correction lands 2–3 times, move it *down* this ladder instead of filing another
  don't-re-flag verdict: run-local note → memory fact → a CLAUDE.md rule → an executable check (a
  pytest guard, a prek hook, a `validate-compose`/`auto-approve` rule). A rule a machine enforces
  beats a paragraph an agent has to remember. Before adding a don't-re-flag verdict, ask: is this a
  *class* (are there sibling instances the same principle governs), and should it be a lint instead?

## Parallel Claude Sessions
Several sessions work this repo at once, each in its own `.claude/worktrees/<name>` checkout.

- **One worktree per session, one PR per session.** Need a second piece of work? Open a
  second worktree rather than stacking it on the first.
- **Name the worktree the branch slug you want.** `EnterWorktree` derives the branch from
  the worktree name (prefixing `worktree-`), so `containers-role-cleanup` gets a readable
  branch and `cleanup` doesn't. Accept the prefix; renaming the branch afterwards only
  detaches it from the worktree git has registered.
- **The SessionStart banner lists the other live sessions** and the paths each has changed
  vs `origin/master`. It's derived from `git worktree list` and `/proc`, not from anything a
  session declares, so check it before editing a file several sessions are near (`CLAUDE.md`,
  `group_vars/`, a shared role) rather than assuming you're alone.
- **Deploys serialize on a lock** — use `./scripts/deploy.sh`, see *Common Commands*.
- **`ExitWorktree` refuses to remove a squash-merged or rebase-merged worktree**, reporting
  "N commits on <branch>" — both land the content on master under new SHAs, so the tool
  cannot see that the work survived. Do **not** pass `discard_changes` to argue with it: the
  branch reads identically to one holding real unlanded work. Verify by content instead —
  `git merge-tree --write-tree origin/master <branch>` equals `git rev-parse
  origin/master^{tree}` when the branch has nothing left to give — then leave the tree for
  the pruner.
- **`uv run python scripts/prune_worktrees.py`** reports which worktrees are finished with;
  `--prune` removes the merged, clean, unlocked ones. It applies the same content check, so
  it collects what `ExitWorktree` refused. A lock held by a *running* session is never
  overridden; a lock whose process is gone is ignored, because Claude Code doesn't release
  the lock when a session ends — which is why a worktree this session still holds stays
  `keep` until the session exits.

## Secrets Management
- Secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS + age
- `ansible/.sops.yaml` (tracked — public keys only) lists the age recipients new/updated
  secrets are encrypted to, and auto-encrypts any `.yml`/`.yaml` in `vars/` or `secrets/`
  directories (SOPS searches upward from the file, so this lives at `ansible/`, not root)
- At runtime, `community.sops.sops_decrypt` lookup decrypts values
- **Rotation tracking:** `ansible/secret_rotation.yml` (plaintext registry — names/dates/tiers,
  no values) + `scripts/secret_rotation.py` (`sync`/`audit`/`rotate`). A daily server cron pushes
  the "Secret Rotation" Kuma monitor; due-dates are staggered. After adding a secret, run
  `uv run python scripts/secret_rotation.py sync` and commit. Runbook + the DANGER `pinned`
  procedures (kopia repo password, authelia storage key): `docs/secret-rotation.md`.
- **Never commit plaintext secrets** (private age keys never leave `~/.config/sops/age/keys.txt`;
  `.gitignore` blocks `keys.txt`/`*.agekey`/`*.key` and gitleaks scans every commit)
- **Onboarding a host to SOPS** (it can't decrypt yet, so `initial_setup.yml`/`deploy.yml`
  fail at their secret-load pre_task): run `uv run ansible-playbook ansible/bootstrap.yml --limit <host>`
  on it (no secret dependency — generates the host's own key, prints its public key), add that
  pubkey to `ansible/.sops.yaml`, `sops updatekeys ansible/vars/secrets.yml` on a host that can already
  decrypt, commit + push, then `git pull` on the new host. Multi-recipient is OR — any listed
  key decrypts the whole file. See `ansible/bootstrap.yml` header for the full flow.

## Ansible Conventions
- All tasks must be **idempotent** — rerunning should be side-effect-free
- Use specific modules (`ansible.builtin.apt`, `ansible.builtin.copy`, etc.) over `shell`/`command`
- Give all tasks meaningful names
- Use `ansible-lint` before committing playbooks
- Jinja2 templating (`{{ var }}`) for all variables

## Docker Compose Conventions
- All containers use Traefik labels for reverse proxy routing
- Docker network: `proxy`
- PUID/PGID: `1000`/`1000`, user: `ubuntu`
- Timezone: `America/Chicago`
- Containers should have healthchecks defined where possible

## Pre-commit Hooks
The repo uses [prek](https://prek.j178.dev) (config: `prek.toml`) with YAML linting, Ansible linting, and gitleaks (secret scanning).
Run `prek run --all-files` to check before committing. The `pytest` and
`validate-compose-templates` hooks shell out to `uv` (see **Python & Tests**), so uv must be
installed for a full `prek run`.

## Python & Tests
Dev/test tooling is managed by [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`).
The repo isn't a Python package — `[tool.uv] package = false` makes it a "virtual" project that
only pins the test deps (the `dev` dependency group) and the pytest config.

```bash
# One-time: install uv — https://docs.astral.sh/uv/getting-started/installation/
uv run pytest                 # all repo unit tests (auto-syncs the env from uv.lock first)
uv run pytest scripts         # just one suite
```

- **What runs is defined once** in `pyproject.toml` `[tool.pytest.ini_options]` `testpaths` —
  consumed by both `uv run pytest` and the prek `pytest` hook. It deliberately excludes the
  vendored `ansible/collections/**` third-party tests.
- **Deps live once** in the `dev` dependency group; the prek `pytest` and
  `validate-compose-templates` hooks call `uv run`, so there's no duplicated dependency list.
  **uv must be on `PATH` for `prek run`** (CI installs it via `astral-sh/setup-uv`).
- **Suites:** `ansible/tests/` (toposort deploy-ordering filters, the k8s auto-deploy guard,
  and the auto-deploy denylist derivation),
  `ansible/roles/k8s/monitor-bridge/files/` + `ansible/roles/k8s/autofix-bridge/files/`
  (B2/Prometheus/Loki check logic),
  `.claude/hooks/` (read-only Bash classifier), `scripts/` (image-diff parser).
- **Test-placement gotcha:** pytest tests must NOT live under `ansible/filter_plugins/` —
  Ansible's plugin loader imports every `.py` there at deploy time and would choke on the
  `pytest` import. `test_toposort.py` lives in `ansible/tests/` and imports its target via the
  `pythonpath` setting in `pyproject.toml`.

CI (`.github/workflows/ci.yml`) runs `prek run --all-files` on every PR and on push to master:
these tests plus lint, template validation, and secret scanning.

## Variables
Global vars in `ansible/inventory/group_vars/all.yml`. Per-host overrides in `ansible/inventory/host_vars/`.
