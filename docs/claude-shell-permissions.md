# Claude Shell Permissions — What Actually Decides

Full reference for how this repo's Bash and `kubectl` commands get auto-approved, prompted, or
denied. The repo-root `CLAUDE.md` carries the short always-on summary; this file holds the detail
behind it — the tables, the measured dates, and the caveats.

## Shell Commands — Shape Them to Auto-Approve
A PreToolUse hook (`.claude/hooks/auto-approve-readonly.py`) auto-approves Bash it can
**prove is read-only**, so those run without a permission prompt. Write exploratory/
read-only commands to fit it. Anything that writes or executes still prompts — that's intended.

**Auto-approves (no prompt):**
- Single read-only commands and pipelines: `grep … | sort | head`
- `rg` on the same terms as `grep` — the classifier answers `allow · read-only: rg`, verified
  2026-08-23 by piping both through `auto-approve-readonly.sh`. Worth knowing because usage is
  lopsided: 9 `rg` calls against 10,092 `grep` over the 7 days to 2026-08-23. Neither is better
  for permissions; pick on merit, not on fear of a prompt.
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
The ssh case is wired separately, via `auto-approve-remote-ssh.sh` on **PermissionRequest**, because
Claude Code evaluates `ask` rules whatever a PreToolUse hook returns, so a PreToolUse decision alone
would never reach an ask-listed command. Registered in the *user-level* settings (chezmoi
`settings.base.json`), not this repo's — a project's settings may only tighten what is auto-approved,
never widen it, so that a repo can't grant itself permissions merely by being opened.

**As of 2026-08-16 those PermissionRequest hooks no longer fire in a normal session.** `Bash(ssh:*)`
and `Bash(curl:*)` were removed from the `ask` tier — they were the largest single source of prompts
and every one was approved — and it is the *ask rule* that routes a call through a PermissionRequest
hook. Without it, ssh and curl fall through to the auto-mode classifier, which reads the whole
command against the hosts and domains named in `autoMode.environment` and makes the narrowing a
prefix rule never could. `allow-safe-curl.sh` / `allow-readonly-remote.sh` / `auto-approve-remote-ssh.sh`
are retained because they still carry **Manual mode**, where no classifier runs.

### `kubectl` — what actually decides
**Read this before trusting the per-verb allow-list below: in a normal session that list decides
nothing.** Sessions default to auto mode (`defaultMode: auto`) with `autoMode.classifyAllShell: true`
in user settings, and that setting **suspends every `Bash()` allow rule while auto mode is active**.
So the classifier judges each `kubectl` command on its full text, and the verb tiers below only apply
in Manual mode or if `classifyAllShell` is turned off. Treat them as a fallback, not as the
mechanism.

**The cluster credential is the real ceiling, and it is lower than any of this.** Plain `kubectl`
authenticates as `system:serviceaccount:kube-system:homelab-readonly`, which holds `get list watch`
and nothing else (`k3s_readonly_sa_name` in `ansible/roles/setup/k3s/defaults/main.yml`). Every write
verb is refused by RBAC — verified 2026-08-16, `kubectl auth can-i` answers **no** for `delete pods`,
`delete pvc` and `pods/exec`, and `kubectl exec` returns *"cannot create resource pods/exec"* rather
than a permission prompt.

**`sudo k3s kubectl` is not an escape hatch — `sudo` is in `permissions.deny`**, so it is blocked
outright, not prompted. (This paragraph used to say `sudo` was ask-listed; that was wrong.) With the
read-only SA on one side and denied `sudo` on the other, **Ansible is the only write path to this
cluster.**

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

**That limitation is exactly why `classifyAllShell` is on.** A `Bash()` rule cannot see a flag, so
`Bash(kubectl apply *)` also approves `apply --prune`; the classifier reads the whole line and can.
The 2026-08-08 note below — that `exec` had to be blanket-allowed because "no rule can distinguish
`exec -- cat …` from `exec -- rm -rf …`" — was true of the rule syntax and is no longer the binding
constraint: the classifier makes that distinction, and RBAC refuses `exec` regardless.

The tiers below describe the **Manual-mode fallback**, not what happens in a normal session:

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
- **`delete` is denied outright**, not prompted — `Bash(kubectl delete:*)` is in `permissions.deny`
  (user settings), which is evaluated before both the ask tier and the classifier and cannot be
  cleared by stating intent. It costs nothing today, since RBAC already refuses it; it exists so a
  future credential widening can't silently hand back `delete` against Longhorn PVCs and their B2
  backup chain. Per-verb matching means the routine, safe `delete pod` (the Deployment recreates it)
  is denied too — accepted, because RBAC refuses that as well.
- **`drain` and `taint` are classifier-judged, not prompted** — they sit in `autoMode.soft_deny`, so
  the classifier blocks them unless you name the node and the operation. A soft block *clears on
  explicit user intent*, which is precisely why `delete` was moved up to a hard deny instead.
- **Never allow-listed, so the classifier judges them:** `replace` (`--force` = delete + recreate),
  `proxy` (a local API gateway authenticating as you), `debug` (`debug node/…` mounts the host
  filesystem in a privileged pod), `attach`, `run`, and `edit` (interactive — it just hangs for an
  agent).

Hand-running an auto-approved *write* verb creates drift from the Ansible source of truth; prefer
`uv run ansible-playbook … --tags <svc>`. The write tier exists for iteration, not for deploys.
