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

> **Two roles trees, deliberately.** `roles/k8s/<name>` is where a service now lives; `roles/containers/<name>` is Docker. Several `roles/containers/` roles survive with no `containers_list` entry because they are the git-owned *source* a k8s role reads at deploy time — **deleting one breaks that k8s role's render, not its own**. These are **config sources, not deployable roles**: their `tasks/`, `meta/`, and `docker-compose.yml.j2` were removed once Docker left both cluster nodes, so nothing in them can be deployed and only the templates/files below are read. The full set, with what reads it:
>
> | source role | read by |
> |---|---|
> | `grafana` | `k8s/claude-otel/tasks/dashboards.yml:80` (dashboard JSON) |
> | `home-assistant` | `k8s/home-assistant/templates/configmap.yaml.j2:19-52` (automations/scenes/scripts/templates) |
> | `homepage` | `k8s/homepage/templates/config-secret.yaml.j2:23-29`, `icons-configmap.yaml.j2:13` |
> | `configarr` | `k8s/configarr/templates/config-secret.yaml.j2:22` + pytest `testpaths` |
> | `janitorr` | `k8s/janitorr/templates/config-secret.yaml.j2:22` |
> | `freshrss` | `k8s/freshrss/templates/configmap.yaml.j2:14` |
> | `livesync` | `k8s/livesync/templates/configmap.yaml.j2:15` |
> | `zigbee2mqtt` | `k8s/zigbee2mqtt/templates/config-secret.yaml.j2:21` |
> | `n8n` | `k8s/n8n-images/tasks/main.yml:18,25,29` (two Dockerfiles + runners JSON) |
> | `ical-proxy` | `k8s/ical-proxy/tasks/main.yml:10,14` + pytest `testpaths` |
> | `code-server` | `k8s/code-server/tasks/main.yml:13,16` |
>
> Plus `common`, the shared Docker deploy path for the Pi's roles. Don't "clean those up"; edit them as before. The rest of `roles/containers/` is either live on `daniel-pi` (`autoheal`, `docker-proxy`, `dozzle`, `glances`, `wg-easy` — these keep their full `tasks/` + compose plumbing) or belongs in `archive/`. To revive one of the config sources as a Docker service, take its Compose plumbing from git history, not from the tree.

## Where to Look (task → start here)
Route to the source of truth by what you're doing, before reading linearly:

| If you're… | Start here |
|---|---|
| Adding / changing a service (k3s — the default) | `## Adding a New Service` below · a sibling role in `ansible/roles/k8s/` |
| Adding / changing a Docker service (the Pi only) | `## Adding a New Service` → *Adding a Docker service* · `/new-container` skill |
| Deploying or redeploying a service | `/deploy` skill · `## Common Commands` |
| Adding / rotating a secret | `/add-secret` skill · `docs/secret-rotation.md` · `## Secrets Management` |
| A Bash command keeps prompting for approval | `## Shell Commands — Shape Them to Auto-Approve` |
| Editing HA automations / lighting / fans | `ansible/roles/containers/home-assistant/CLAUDE.md` (still the config source; the workload runs from `roles/k8s/home-assistant`) · `/ha-edit-automation` |
| Reviewing the homelab for gaps | `/homelab-review` skill (per-domain reviewer agents) |
| Chasing a reliability / monitoring "gap" | The role's `CLAUDE.md` + monitor-bridge `check.py` **first** — mature setup, most are handled |
| A config edit won't restart the pod (k3s) | A ConfigMap/Secret change alone doesn't roll a Deployment — the role needs a `checksum/config` pod annotation. See `roles/k8s/monitor-bridge/templates/deployment.yaml.j2` for the pattern. |
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
`--check` runs unlocked. The bare `ansible-playbook` forms below still work and are what
the wrapper runs; use them only when you deliberately want no lock.

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

# Config-only: render dirs/templates/host config WITHOUT touching the container
# (every container-role task is block-tagged config/deploy/cron; tags union in
# Ansible, so scope with --skip-tags. --skip-tags config is NOT supported — the
# registered config-change facts feed docker_deploy's recreate decision.)
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --skip-tags deploy

# Edit encrypted secrets
sops ansible/vars/secrets.yml

# Initial server setup — first-host bring-up ORDER (uv → SOPS onboarding → this) is in ansible/README.md
uv run ansible-playbook ansible/initial_setup.yml
```

## Shell Commands — Shape Them to Auto-Approve
A PreToolUse hook (`.claude/hooks/auto-approve-readonly.py`) auto-approves Bash it can
**prove is read-only**, so those run without a permission prompt. Write exploratory/
read-only commands to fit it. Anything that writes or executes still prompts — that's intended.

**Auto-approves (no prompt):**
- Single read-only commands and pipelines: `grep … | sort | head`
- Read-only stages sequenced with `;`, `&&`, `||`, or newlines: `cd dir && grep … *.j2`
- Write-free redirects: `… 2>/dev/null`, `>/dev/null 2>&1`
- Read-only `git`/`docker`/`find` (no `-exec`/`-delete`) and read-only `awk`/`sed`
- Read-only host/package queries: `apt list`/`apt show`/`apt policy`, `apt-cache …`,
  `dpkg -l`/`-L`/`-s`/`-S`, `dpkg-query …`, `apt-mark showmanual`, `pipx list`,
  `lsb_release`, `sensors`, `mailq`, `crontab -l` (the write forms — `dpkg -i`,
  `apt install`, `crontab -r`, `sensors -s`, … — still prompt)
- Those same read-only commands run over `ssh daniel-server`/`ssh daniel-pi` — the remote
  command is classified exactly like a local one, so `ssh daniel-pi docker logs wg-easy
  --since 24h 2>&1 | tail -20` goes through. (Pick a host that still has Docker — neither
  cluster node does since 2026-08-14; for cluster logs use `kubectl logs` locally instead.) Connection flags (`-i`, `-p`, `-l`, `-q`, `-o`
  with a connection-only key) are fine; forwarding/proxying (`-L`/`-R`/`-D`/`-A`/`-F`,
  `-o ProxyCommand=…`), a second hop, any other host, and remote reads of secret paths or
  globs still prompt.

**Forces a prompt — restructure, or just accept the one-off prompt:**
- **Command substitution** `$(…)`, backticks, `${…}` — rejected outright. Replace
  `svc=$(echo "$d" | cut -d/ -f4)` with a substitution-free pipeline, or split the step out.
- **Shell control flow** — `for`/`while` loops, `if/then/else/fi`. Prefer one `grep`/`find`/`awk`
  over a loop: e.g. `grep -L "limits:" …/*.j2` (files missing a pattern) + `grep -l "limits:" …`
  (files with it) instead of looping `if grep -q …; then …; fi`.
- **Writes/exec** — `> file`, `tee`, `sed -i`, `sed s///e|w`, `awk 'system()'`/`print > "f"`,
  subshells `(…)`, backgrounding `&`. (Note: `awk` programs containing `>` — even as a
  numeric comparison — are conservatively rejected; use a different test or accept the prompt.)

Source of truth + tests: `.claude/hooks/auto-approve-readonly.py`, `.claude/hooks/test_auto_approve_readonly.py`.
The ssh case is wired separately, via `auto-approve-remote-ssh.sh` on **PermissionRequest**: Claude Code
evaluates `ask` rules whatever a PreToolUse hook returns, and `Bash(ssh:*)` is one, so the PreToolUse
decision alone would never reach it. Registered in the *user-level* settings (chezmoi
`settings.base.json`), not this repo's — a project's settings may only tighten what is auto-approved,
never widen it, so that a repo can't grant itself permissions merely by being opened.

### `kubectl` — which verbs auto-approve
`kubectl` is **not** handled by `auto-approve-readonly.py`; it is allow-listed by verb in this repo's
`.claude/settings.json`.

**The cluster credential is the real ceiling, not this allow-list.** Plain `kubectl` authenticates as
`system:serviceaccount:kube-system:homelab-readonly`, which holds `get list watch` and nothing else
(`k3s_readonly_sa_name` in `ansible/roles/setup/k3s/defaults/main.yml`). Every write verb below is
therefore refused by RBAC — `kubectl exec` returns *"cannot create resource pods/exec"*, not a
permission prompt. Allow-listing a write verb only decides whether Claude is prompted *before*
receiving that refusal. Real writes go through Ansible, or through `sudo k3s kubectl`, which uses the
root kubeconfig and still prompts because `sudo` is ask-listed.

**A `Bash()` rule matches on `kubectl <verb>` and nothing finer — flags and sub-subcommands in the
rule are decorative.** Measured 2026-08-08 against the OTEL `tool_decision` stream:

- `Bash(kubectl get *)` matches `kubectl -n homelab get pods` — flag *position* is normalised away,
  so a rule is needed per verb, not per flag order. This part is convenient.
- `Bash(kubectl create job *)` also permitted `kubectl create namespace`, and
  `Bash(kubectl config view *)` also permitted `kubectl config get-clusters`. The trailing
  sub-subcommand does not narrow anything.
- A flag-level guard **cannot be written at all**: `Bash(kubectl apply --prune*)` failed to fire as
  an `ask` rule *and* as a `deny` rule, while `kubectl apply --prune …` ran unprompted.

So: only allow-list a verb whose **entire** surface is acceptable. Never write a rule that looks like
it narrows a verb — it doesn't, and it reads as a guarantee that isn't there.

- **Auto-approved, read-only:** `get`, `logs`, `describe`, `top`, `explain`, `events`,
  `api-resources`, `api-versions`, `version`, `diff`, `wait`.
- **Auto-approved, reversible writes:** `apply`, `create`, `patch`, `set`, `scale`, `label`,
  `annotate`, `cordon`, `uncordon`, `auth`, and `rollout` (restart/undo/pause/resume, plus the
  read-only status/history that come with the verb). Each is undone by redeploying the role from
  the Ansible-rendered manifests. Three edges come with the verbs and cannot be carved out:
  `apply --prune` deletes resources absent from the manifest set, `create token` mints a
  ServiceAccount credential, and `auth reconcile` rewrites RBAC.
- **Auto-approved, container access:** `exec`, `cp`, `port-forward`. These are arbitrary code
  execution inside a container — allowed deliberately (decided 2026-08-08) because in practice they
  are used for reads, and no rule can distinguish `exec -- cat …` from `exec -- rm -rf …`. Several
  of these containers mount Longhorn PVCs.
- **Deliberately still prompts:** `delete`, `drain`, `taint` — they destroy state, and Longhorn PVCs
  and their B2 backups sit behind them. Note `delete pod` alone is routine and safe (the Deployment
  recreates it), but per-verb matching means allowing it would also allow `delete pvc|namespace`.
  Also `replace` (`--force` = delete + recreate), `proxy` (a local API gateway authenticating as
  you), `debug` (`debug node/…` mounts the host filesystem in a privileged pod), `attach`, `run`,
  and `edit` (interactive — it just hangs for an agent).
- `sudo k3s kubectl …` still prompts — `sudo` is ask-listed user-side and is unaffected here.

Hand-running an auto-approved *write* verb creates drift from the Ansible source of truth; prefer
`uv run ansible-playbook … --tags <svc>`. The write tier exists for iteration, not for deploys.

## Claude Tooling in This Repo (`.claude/`)
- **`scripts/probe.py`** — read-only homelab diagnostics, allow-listed (no prompt). Resolves the
  live container IP via `docker inspect`, so prefer it over curling bridge IPs (which change on
  recreate): `uv run python scripts/probe.py <targets | metric '<promql>' | loki-query '<logql>' |
  alerts | scrutiny | pi <path> | cert <host> | health <svc> | ha <state|automation|get> …>`.
  `alerts [--days N --check X]` reconstructs monitor-bridge's DOWN alert history from Loki (Kuma
  keeps only current state) — one row per firing episode; the same view is the "Alert History"
  Grafana board (Infrastructure folder). `health <svc>`
  exits 0 only when the container is running + healthy — usable as a post-deploy gate. `ha …`
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
- **`uv run python scripts/prune_worktrees.py`** reports which worktrees are finished with;
  `--prune` removes the merged, clean, unlocked ones. A lock held by a *running* session is
  never overridden; a lock whose process is gone is ignored, because Claude Code doesn't
  release the lock when a session ends.

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
- **Suites:** `ansible/tests/` (toposort deploy-ordering filters),
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
