# Server Homelab — Claude Code Context

## Project Overview
Docker-based homelab managed with Ansible. ~42 containerized services deployed across two hosts via infrastructure-as-code.

**Hosts:**
- `daniel-server` — main server (Intel XE graphics, LVM storage)
- `daniel-pi` — Raspberry Pi

**Key technologies:** Docker Compose, Ansible, Traefik (reverse proxy), Cloudflare DNS, Authelia (SSO), SOPS/age (secret encryption)

## Directory Structure
```
ansible/          # Ansible playbooks, roles, inventory, templates  ← EDIT HERE
containers/       # Docker Compose definitions deployed by Ansible  ← DO NOT EDIT
scripts/          # Python helper scripts
files/            # Data migration utilities
```

> **`containers/` is read-only.** Files here are generated and deployed by Ansible from templates in `ansible/roles/containers/*/templates/`. Any direct edits will be overwritten on the next deploy. Always modify the corresponding Ansible role template instead.

## Adding a New Container Service
1. Create `ansible/roles/containers/<name>/tasks/main.yml`
2. Add a `docker-compose.yml.j2` template in `ansible/roles/containers/<name>/templates/`.
   Use the shared macros in `ansible/templates/` rather than hand-rolling boilerplate:
   `traefik.yml.j2` (`labels`), `autokuma.yml.j2` (`kuma`), `healthcheck.yml.j2`
   (`healthcheck`), `networks.yml.j2` (`service_networks()` / `external_networks()` —
   the per-service and top-level `networks:` blocks), and `resources.yml.j2`
   (`resources(cpu_limit, mem_limit, cpu_res, mem_res)` — the `deploy.resources` caps).
   The `/new-container` skill has the canonical skeleton.
3. Add the role to `ansible/deploy.yml` with a tag matching the service name
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
```bash
# Deploy a specific container
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>"

# Deploy all containers
uv run ansible-playbook ansible/deploy.yml

# Dry run
uv run ansible-playbook ansible/deploy.yml --tags "<service-name>" --check

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

## Secrets Management
- Secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS + age
- `ansible/.sops.yaml` (tracked — public keys only) lists the age recipients new/updated
  secrets are encrypted to, and auto-encrypts any `.yml`/`.yaml` in `vars/` or `secrets/`
  directories (SOPS searches upward from the file, so this lives at `ansible/`, not root)
- At runtime, `community.sops.sops_decrypt` lookup decrypts values
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
  `ansible/roles/containers/monitor-bridge/files/` (Kopia/Prometheus check logic),
  `.claude/hooks/` (read-only Bash classifier), `scripts/` (image-diff parser).
- **Test-placement gotcha:** pytest tests must NOT live under `ansible/filter_plugins/` —
  Ansible's plugin loader imports every `.py` there at deploy time and would choke on the
  `pytest` import. `test_toposort.py` lives in `ansible/tests/` and imports its target via the
  `pythonpath` setting in `pyproject.toml`.

CI (`.github/workflows/ci.yml`) runs `prek run --all-files` on every PR and on push to master:
these tests plus lint, template validation, and secret scanning.

## Variables
Global vars in `ansible/inventory/group_vars/all.yml`. Per-host overrides in `ansible/inventory/host_vars/`.
