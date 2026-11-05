# Server Homelab — Claude Code Context

## Project Overview
Docker + k3s homelab managed with Ansible and deployed across three hosts via infrastructure-as-code. Docker survives only on `daniel-pi`. `docs/reference/services.md` holds the service count and the per-service host/route/auth/backup/auto-deploy facts; the `docs-refresh` cron regenerates it, and `docs.local.<domain>` serves it.

**Hosts:**
- `daniel-box` — k3s server / control-plane node (Traefik edge, Authelia+OIDC, Pi-hole DNS, Longhorn storage, most workloads since 2026-08 migration). Ansible runs on this host.
- `daniel-server` — k3s agent node (Intel XE graphics, LVM storage, UPS hardware + the
  `nut_host` shutdown chain; Docker uninstalled 2026-08-14 — the migration's end state)
- `daniel-pi` — Raspberry Pi, the **only** remaining Docker host (LAN-only: wg-easy, alloy,
  autoheal, docker-proxy). Its containers ship in its `homelab-daniel-pi` package; Ansible's
  setup plays still reach it over SSH with `-e target=daniel-pi`.

**Key technologies:** k3s (Kubernetes), Ansible, Docker Compose (Pi only), Traefik (reverse proxy), Cloudflare DNS, Authelia (SSO), SOPS/age (secret encryption), Longhorn (storage), CrowdSec (WAF)

## Directory Structure
```
ansible/          # Ansible playbooks, roles, inventory, templates  ← EDIT HERE
  roles/k8s/        # One role per k3s workload (rendered manifests) — where most services live
scripts/          # Helper scripts, grouped by what they act on
  lib/              # The helpers everything else imports: repo_paths, render_guard, docs_provenance
  backup/ deploy_tools/ diagnostics/ docs/ grafana/
  home_assistant/ infra_map/ secrets_mgmt/ validate/ dev/
docs/             # Runbooks, design specs, security notes
  archive/          # Superseded planning docs, incl. the completed Docker → k3s migration
```

> **The Pi's Docker services are not Ansible roles.** `ansible/inventory/host_vars/daniel-pi.yml` lists them, and `.claude/rules/docker.md` has how they render. A service's config lives in the tree that deploys it: **if a k3s workload reads it, it is under `roles/k8s/<name>/`**.

## Where to Look (task → start here)
Route to the source of truth by what you're doing, before reading linearly. A task a skill
covers — deploying, landing, secrets, Renovate PRs, a GitOps tick, worktree cleanup, a homelab
review — routes through the skill listing rather than this table.

| If you're… | Start here |
|---|---|
| Adding / changing a service (k3s — the default) | `/new-k8s-service` skill (it scaffolds with `scripts/dev/new_k8s_service.py`; no sibling copying) · `ansible/roles/k8s/manifests/CLAUDE.md` for the shared render → apply → queue contract every k8s role includes (`grep -rl k8s/manifests ansible/roles/k8s/*/tasks/` lists them) |
| A PR just merged — what now | `## After a PR Merges — Pull, Deploy, Verify` below (the directive and *When to wait*) · `/land-after-merge` skill (the commands) |
| Picking up an open issue, or working through the backlog | `findings.py next` names what is free to take · `/issue-fanout` skill for several at once. Claim before you start: `findings.py claim <n> --worktree <branch>`; `findings.py claim --help` says what a refusal means and which subcommands read and release claims. |
| A Bash or `kubectl` command keeps prompting, or you need the full permission tables | `## Shell Commands — Shape Them to Auto-Approve` below (summary) · `docs/claude-shell-permissions.md` (full detail) |
| Editing HA automations / lighting / fans | `ansible/roles/k8s/home-assistant/CLAUDE.md` (config and workload both live there; it routes to `docs/` for per-topic behaviour) · `/ha-edit-automation` |
| Answering "what runs here / where / behind what" | `docs/reference/` — generated from the tree by the `docs-refresh` cron, browsable at `docs.local.<domain>`. Services, hosts, secret rotation, scheduled jobs, networking. **Never hand-edit a generated page** (a file carrying a `generated_from:` banner); a hook denies the write. Change the generator instead (`scripts/docs/build_docs.py` lists them). |
| Changing code that a `CLAUDE.md` section cites | Run `uv run python scripts/dev/fact_status.py notice --since origin/master`, and re-read each listed section whose prose the PR left unchanged. `.claude/rules/facts.md` has the citation grammar. |
| Writing a Python module or a test, and deciding where it goes | `.claude/rules/python-layout.md` — the `sys.path` bootstrap a cross-directory import needs, the `tests/` sibling rule and the `ansible/tests/` taxonomy. It loads on its own when you touch a path it governs; read it first when you are choosing the path. |
| Chasing a reliability / monitoring "gap" | The role's `CLAUDE.md` + monitor-bridge `ansible/roles/k8s/monitor-bridge/files/registry.py` (the check registry) **first** — mature setup, most are handled |
| Checking that a service's UI actually renders, not just that its pod is Ready | The `homelab-ui` MCP server (`docs/claude-tooling.md`). `probe.py health` cannot see a broken UI behind a healthy pod. **Grafana is the exception** — it still serves a login page behind Authelia, so drive it with `uv run pytest -m ui -k grafana` rather than by hand. |
| A config edit won't restart the pod (k3s) | A ConfigMap/Secret change alone doesn't roll a Deployment. The general mechanism is the central rollout-restart in `ansible/roles/k8s/manifests/CLAUDE.md`, which fires when a role's rendered manifests change. A file embedded in a rendered ConfigMap needs nothing more; `ansible/roles/k8s/monitor-bridge/templates/script-configmap.yaml.j2` is the worked example. Content that reaches the cluster another way needs its own `checksum/<thing>` pod annotation from `ansible/templates/checksum-annotation.yml.j2`. |
| A config edit won't recreate the container (Docker, the Pi) | `hosts/components/pi-compose/services/alloy/compose.yaml.j2` — its `homelab.config-sha256` label carries the bind-mounted config's hash, so a config edit changes the service and `up -d` recreates it |
| Starting Claude Code sessions from a phone | `hosts/components/claude-code/CLAUDE.md` — `claude-rc.service` hosts them. `/remote-control` inside a session and `claude rc` from a shell are different features; only the second creates sessions on demand. |
| Adding / changing a cron that changes state | that role's `CLAUDE.md` *Autonomous-role contract*. Every setup role whose cron or timer changes state carries one, and every setup role has a `CLAUDE.md` (`ansible/tests/repo/test_role_claude_md.py`) — `ansible/tests/setup/test_setup_cron_roles_have_a_contract.py` derives the cron-installing set from `tasks/` and names the heartbeat-only exemptions. A host component's timer owes one too: the `component-timers-carry-a-contract` census row in `ansible/tests/repo/_component_contract_row.py` holds it |
| Recording a finding, fix or improvement you will not do this session | `findings.py open` (flags: `findings.py open --help`) — files a GitHub Issue labelled `claude`, deduped by title/file. `--repo OWNER/NAME` files into another repo's register, such as the dotfiles repo. `findings.py list` is the open register; `docs/reference/backlog.md` renders it. Never `gh issue create` by hand — `.claude/hooks/block-footguns.py:issue_create_by_hand_problem` denies it and names the wrapper. |

## Adding a New Service

**Default to k3s.** A new service belongs in `ansible/roles/k8s/<name>/` unless it must run
on `daniel-pi` (LAN-only utilities, WireGuard) — `daniel-server` and `daniel-box` have no
Docker at all, so a Compose service there deploys nothing.

- **k3s** → the `new-k8s-service` skill, which opens with `scripts/dev/new_k8s_service.py`
  (#2855).
- **Docker on `daniel-pi`** → the `new-container` skill.

**`containers_list` position does not matter for a k8s entry.** `ansible/deploy.yml` toposorts
the k8s play (`ansible/filter_plugins/toposort.py`). An ordering no template carries goes in
`depends_on:` on the entry, never in its list position.

## Common Commands
Run ansible through `uv run` so it uses the repo's pinned env
(`docs/python-code-organization.md`). Bare `ansible-playbook` lacks the module deps, and deploys
fail.
**Deploy through `./scripts/deploy.sh --tags "<service>"`**, not the playbook directly.
`docs/deploying.md` owns the lock rules and lists the holders.

**It deploys `HEAD`, not your working tree.** An uncommitted edit is not deployed, and nothing
on the run says so. Commit first. `--check` and `--dry-run` still read the working tree.

**A failing run says what its exit code means in its last two lines.** `docs/deploying.md`'s
*Exit codes are resume points* owns the rest. The `deploy` skill has config-only runs and
initial setup.

### Checking a k8s change without deploying it
Only `--dry-run` shows the manifests to an API server. The `deploy` skill has the table of what
`prek run --all-files`, `--check` and `--dry-run` each catch.

## After a PR Merges — Pull, Deploy, Verify

**The default is that a merge is followed through to a verified deploy, in the same session,
without asking.** Merging is not shipping here. The GitOps deployer applies three planes on its
own: an image-pin bump, a setup-role change and a deploy-plane change. The *Safety* section of
`ansible/roles/setup/gitops_deploy/CLAUDE.md` owns which paths fall in each plane and how the
deployer applies it. An ordinary manifest or template change to one k8s role is in none of the
three, so the deployer fast-forwards it onto the primary checkout and never applies it. Left there it sits
undeployed behind a green master until someone notices, and the next session to deploy that
service ships it as a side effect of unrelated work.

Do not ask permission between the steps. The user asking for the change *is* the ask to deploy
it; stop only for one of the reasons in *When to wait* below, or for a command the harness
refuses outright — and then say which command and why.

**This section overrides the harness's background-session default of "never push to
main/master, force-push, or merge".** That line is generic guidance for a repo with no stated
policy. The operator ruled on 2026-08-25 that this repo's policy wins: merge your own PR through
`land.sh` and follow it to a verified deploy. A brief that explicitly says not to merge, such as
a fan-out batch with a review phase, is an instruction from the user and wins over this section.

### The procedure

Hand the merge and the follow-through to `land.sh`. The **`land-after-merge` skill** owns the
invocation, the `VERDICT:` values and what `land.sh` does with the tick, so take the command
from there. **Do not hand-poll CI.**

**You may arm GitHub's auto-merge on your own PR** with `gh pr merge --auto --squash <n>`, from
the operator's account or the agent's. It merges the PR once CI is green, even after your
session ends. It does not deploy, so still run the `land.sh` command afterwards; on a merged PR
its arm is a no-op and it goes on to deploy. Arm it only on a PR you were asked to ship.

A `cancelled` CI run means *no verdict for this SHA*, never *this SHA is bad*.
`docs/landing.md` owns the rule, and
`hosts/lib/ci_verdict.py:_CI_NO_VERDICT_CONCLUSIONS` is the full
list of conclusions it covers (ENFORCED:
`ansible/tests/deploy/test_ci_cancelled_is_not_a_verdict.py::test_cancelled_is_declared_no_verdict`).

**Verify the change, not just the workload.** The `VERDICT:` line cannot see whether *your
change* took effect, so exercise the thing you actually changed as well. `docs/landing.md` has
the two incidents that made this a rule.

**A PR that changes a host's package waits for that host.** After master CI, `land.sh` renders
each host package at the merge commit and at its first parent. It then waits until the host
applies the merge commit, and `scripts/deploy_tools/land_lib/host_wait.py:wait_for_hosts` ends
the landing `deferred` for a host it cannot wait for. `docs/landing.md` says when.

### Working alongside other sessions

**The tick pulls all of master, not just your commit.** Another session's merged work
fast-forwards with yours. `land.sh` already scopes to your PR's own files; if you override with
`--tags`, keep it to your own services. `land.sh --help` lists the exit codes, including the
ones that mean another session got there first.

### When to wait

Say which of these applies, then stop:

- Master CI is red. Pending is not a reason to stop, because `land.sh` waits on it.
- The host holds a non-empty `hold_sha` — a previous SHA already failed its health gate, or a
  broad apply failed. `hold_plane` names each failed playbook when it was the latter.
- A change in one of the bring-up playbooks — `bootstrap.yml`, `k3s-bringup.yml`,
  `initial_setup.yml`, the list
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_MANUAL_PREFIXES` holds —
  sits in the `local..origin` range, and it is another session's. The deployer applies every
  other broad change itself, but not the bring-up playbooks: those run by hand by
  construction. Clearing it means applying their change; name it and stop.
- A `manual_plane` line in the host's `owed.jsonl` ledger names a setup role the deployer
  cannot apply, and another session's PR created it. That line is theirs to clear. When your
  own merged PR created the line, do not stop: run `uv run python
  scripts/deploy_tools/owed_run.py <role>` from any checkout. It runs exactly the commands
  the line owes in the primary checkout, which must be at `origin/master`, on the hosts the
  line names, then clears it, and it refuses a line that needs you, such as a maximal apply. The operator
  granted that standing clearance on 2026-10-24.
- `deploy.sh` exits 3: the change is broad (shared templates, inventory, the setup plane) and
  maps to no single service.
- The change is docs- or `tasks/`-only — the deployer skips those deliberately, and so do you.
- The change is in the policies-first ordering plane, where a partial run leaves the exact state
  the ordering exists to prevent.
- A classifier denial. Report the denied command; do not reach for a bypass flag.

## Shell Commands — Shape Them to Auto-Approve
Write exploratory commands so they auto-approve; expect a prompt for the rest.

- **Auto-approves:** read-only single commands and pipelines (`grep … | sort | head`), read-only
  stages sequenced with `;`, `&&`, `||` or newlines, write-free redirects, and those same
  read-only commands run over `ssh daniel-server` / `ssh daniel-pi`.
- **Forces a prompt:** command substitution — `$(…)`, backticks, `${…}` — rejected outright;
  shell control flow (`for`/`while` loops, `if/then/else/fi`); anything that writes or execs
  (`> file`, `tee`, `sed -i`, subshells `(…)`, backgrounding `&`).
- Restructure rather than loop: one `grep`/`find`/`awk` usually replaces the control flow.
- An isolated session meets a stricter rule: the binary refuses a variable or a here-document
  there too. *Parallel Claude Sessions* below names every construct it refuses.

### `kubectl` — what actually decides
Plain `kubectl` authenticates as a **read-only ServiceAccount**, so RBAC refuses every write
verb, and `sudo` and `kubectl delete` are denied outright. **Ansible is the only write path to
this cluster**: deploy through `./scripts/deploy.sh --tags <svc>`.

Full tables, per-verb tiers, hook wiring and the RBAC evidence: `docs/claude-shell-permissions.md`.

## Claude Tooling in This Repo (`.claude/`)
One directive per tool. `docs/claude-tooling.md` is the full reference, and a hook that
denies prints its own reason.

- **`scripts/diagnostics/probe.py`** — read-only homelab diagnostics, allow-listed (no prompt);
  `--list` prints every subcommand. **`health <svc>` is the k8s post-deploy gate**, and its
  argument is a deploy TAG.
- **`homelab-ui` MCP server** — a headless Chromium that can *see* a service's UI, which
  `probe.py health` cannot.
- **`rootq`** — reads root-only host state on daniel-box and daniel-server through one read-only
  sudoers grant to the operator's account. A session gets no other sudo, and the agent user gets
  none at all, so never probe with `sudo -n true`.

## Review & Memory Hygiene (making judgment cumulative)
The rules for promoting a review learning into memory live in the `memory-consolidation` and
`homelab-review` skills, which run at the moment they apply.
- **Mark a settled trade-off `# DECIDED:` at the line it governs.** A decision recorded only in a
  memory file or a commit message is a decision every future reviewer re-derives. Reviewer briefs
  grep for the marker (`.claude/skills/homelab-review/SKILL.md`, step 3). `docs/adr/index.md` has
  how to write, cite and contradict one.
- **A decision recorded in a Claude artifact moves into `docs/` in the PR that acts on it.**
  `prune-artifacts.sh` deletes an artifact 30 days after its last update, and artifacts are in
  neither git nor the break-glass bundle. Write the substance into `docs/` or `docs/adr/`.
  `docs/claude-agent-user.md` is the worked example (#3657).
- **A finding you will not fix this session is filed, not remembered.** Run `findings.py open`
  before the session ends, and list the issue numbers in the end-of-job report. Open items are
  `findings.py list`'s register, not a memory file's. `findings.py <cmd> --help` has the flags,
  and `findings.py close --help` has what each way of closing means.

## Parallel Claude Sessions
Several sessions work this repo at once, each in its own `.claude/worktrees/<name>` checkout.

- **One worktree per session, one PR per session.** Need a second piece of work? Open a
  second worktree rather than stacking it on the first.
- **Name the worktree the branch slug you want.** `EnterWorktree` derives the branch from the
  worktree name, prefixing `worktree-`.
- **In an isolated session or Agent subagent, write each Bash command so the Claude Code
  binary can verify it stays inside the worktree.** The binary refuses a command it cannot
  verify ("too complex to verify"), and no hook can lift the refusal. Open a compound command
  with `cd <your worktree> &&`. `docs/claude-tooling.md`, *Worktree sessions*, has the rest.
  Three kinds of construct draw the refusal, with or without the prefix:
  - **A shell expansion:** `$VAR`, `$TMPDIR`, `$?`, `$(…)`, backticks or brace expansion
    `{a,b}`. Write the literal path.
  - **Control flow:** a `for` or `while` loop, a shell function, a `( … )` subshell, a
    trailing `&` or a `<(…)`. Make one call per item, or run a script file that holds the loop.
  - **A here-document:** `cat > f <<'EOF'` or `python3 - <<'EOF'`. Write file content with
    the Write tool.
- **The SessionStart banner lists the other live sessions** and the paths each has changed
  vs `origin/master`. Check it before editing a file several sessions are near (`CLAUDE.md`,
  `group_vars/`, a shared role).
- **Never pass `discard_changes` to `ExitWorktree`** when it refuses a merged worktree. That is
  how unlanded work is lost. Retiring a worktree is the `worktree-cleanup` skill.

## Secrets Management
- Secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS + age. Edit them with `sops`
  or the `/add-secret` skill.
- `ansible/.sops.yaml` lists the age recipients and sets which paths auto-encrypt.
  `.claude/rules/secrets.md` holds its scope and the `grep -c` check that a key exists.
- **After adding a secret, run `uv run python scripts/secrets_mgmt/secret_rotation.py sync`
  and commit.** `ansible/secret_rotation.yml` is the rotation registry, and
  `docs/secret-rotation.md` owns the tiers and the DANGER `pinned` procedure.
- **`git diff ansible/vars/secrets.yml` prints plaintext credentials.** `.gitattributes:1` sets
  `diff=sops`, so git decrypts the file before diffing it, and a value exposed that way needs
  rotating. To see THAT it changed without seeing anything it holds, use `git diff --stat
  ansible/vars/secrets.yml` or `--name-only`. To see what actually changed, open it with `sops
  ansible/vars/secrets.yml`. Do not filter the plaintext through a pipe; the deny hook refuses
  it, and `docs/claude-shell-permissions.md` has why.
- **An agent never decrypts `ansible/vars/secrets.yml`.** The user-level guard denies every
  `sops` decrypt form, `--extract` included, because the plaintext lands in the transcript.
  A runbook step that decrypts is marked operator-only.
  `docs/claude-shell-permissions.md` has the guard's location.
- **Never commit plaintext secrets** (private age keys never leave `~/.config/sops/age/keys.txt`;
  `.gitignore` blocks `keys.txt`/`*.agekey`/`*.key` and gitleaks scans every commit)
- **The cluster file is generated, never hand-edited.** `cluster-vars.sops.env` sits in
  `ansible/vars/`, and only `cluster_vars.py write` writes it. That command decrypts, so only
  the operator or the rotation cron runs it, never an agent. `docs/secret-rotation.md` owns the
  mechanics.
- **A host that can't decrypt yet** fails `initial_setup.yml`/`deploy.yml` at their
  secret-load `pre_task`, which reads as a playbook bug rather than a missing key. The
  **`add-secret` skill** has the onboarding through `ansible/bootstrap.yml`.

## Pre-commit Hooks
The repo uses [prek](https://prek.j178.dev). `prek.toml` holds the config and each hook's history.
Run `prek run --all-files` to check before committing. **That does not run the suite.** No
hook runs it, neither at commit nor at push. The one exception is `static-ratchet-tests` and
its `static-ratchet-tests-*` siblings: at commit time it runs twenty-nine static ratchet, lint
and census test files. CI's `pytest` job runs the suite whole on every PR, and `land.sh` waits
on that job before it merges. To see a red test before CI does, run `uv run pytest` yourself.
A fan-out batch names test paths instead, and `.claude/hooks/block-batch-full-suite.py` refuses
its whole-suite run.
`.gitignore`'s root deny is load-bearing — one untracked file parks the deployer.

## Python & Tests
`uv run pytest` runs every suite `testpaths` in `pyproject.toml` names, and `uv run pytest
scripts` runs one. `docs/python-code-organization.md` has the uv setup.

- **A new check ships with a proof it can go RED**, a named member it must find if it finds its
  subject by pattern, and a measured transport if it reaches over a network.
  `.claude/rules/python-layout.md` has the three rules and the incidents behind them, and says
  where a new test or module goes.

CI (`.github/workflows/ci.yml`) runs `prek run --all-files` on every PR and on push to master:
these tests plus lint, template validation, and secret scanning.
