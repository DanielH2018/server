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
  2026-08-23 by piping both through the classifier's shim. Worth knowing because usage is
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
  over a loop: for example, `grep -L "limits:" …/*.j2` (files missing a pattern) + `grep -l "limits:" …`
  (files with it) instead of looping `if grep -q …; then …; fi`.
- **Writes/exec** — `> file`, `tee`, `sed -i`, `sed s///e|w`, `awk 'system()'`/`print > "f"`,
  subshells `(…)`, backgrounding `&`. (Note: `awk` programs containing `>` — even as a
  numeric comparison — are conservatively rejected; use a different test or accept the prompt.)

Source of truth: `.claude/hooks/auto-approve-readonly.py` holds the per-command guards and the
PreToolUse entry point. `.claude/hooks/_readonly_tables.py` holds the allow-list and the ssh gate, except
the trusted-host set and the secret-path pattern — those are `claude_guard.tables`'s
`TRUSTED_SSH_HOSTS` and `SECRET_PATH_RE`, defined once in the dotfiles `claude_guard` package
(deployed to `~/.local/share/claude-guard`) and imported through `.claude/hooks/_claude_guard.py`.
`.claude/hooks/_readonly_shell.py` holds the operator tokens and the redirect rules, `.claude/hooks/_readonly_sed.py` the `sed` guard; the command is cut into stages by the same `claude_guard.segment` the deny guards read, through `_hook_common.segments` (#2198), so a host without the package gets no auto-approve at all. Tests:
`.claude/hooks/tests/test_auto_approve_readonly.py`, `.claude/hooks/tests/test_claude_guard_import.py`.
The ssh case on **PermissionRequest** is the user-level `guard-permission-request.sh` (the dotfiles
`claude_guard` judge), and only that, since 2026-09-18 — the paragraph below has the history. Claude
Code evaluates `ask` rules whatever a PreToolUse hook returns, so a PreToolUse decision alone never
reaches an ask-listed command; this repo registers no PermissionRequest hook of its own.

**A machine without the dotfiles deploy gets no auto-approve on the remote-ssh path, rather than a
stale local copy.** The hook prints one `classifier did not run` line to stderr and exits 0 with no
stdout, and the prompt stands. The two PreToolUse shims take the other posture on a failed cd —
`bash-pretool.sh`, which carries the three Bash deny guards since #2394, and
`block-protected-edits.sh`: an **ask** naming the guards that did not run, because a bare exit 0
from a deny guard is an allow (#2171).
`.claude/hooks/tests/test_claude_guard_import.py` measures the allow side end to end, and diffs the
CI stand-in in `tests/conftest.py` against the deployed tables — a diff CI itself cannot run, since
CI has no dotfiles deploy; it goes red under `prek run` on a deployed host.

**As of 2026-08-16 those PermissionRequest hooks no longer fire in a normal session.** `Bash(ssh:*)`
and `Bash(curl:*)` were removed from the `ask` tier — they were the largest single source of prompts
and every one was approved — and it is the *ask rule* that routes a call through a PermissionRequest
hook. Without it, ssh and curl fall through to the auto-mode classifier, which reads the whole
command against the hosts and domains named in `autoMode.environment` and makes the narrowing a
prefix rule never could. The judge's `curl_safe` / `readonly_remote_safe` / `trusted_host_safe`
checks are retained because they still carry **Manual mode**, where no classifier runs.

**Two PermissionRequest hooks judged a prompted ssh command from the dotfiles `claude_guard`
cutover of 2026-09-17 until 2026-09-18** (issues #1864, #1898). The user-level
`guard-permission-request.sh` ran `claude_guard.judge()`, and this repo's
`auto-approve-remote-ssh.sh` ran `classify_remote`; both read `TRUSTED_SSH_HOSTS` and
`SECRET_PATH_RE` from the same package. Measured 2026-09-17 on daniel-server, 20 runs each,
payload `ssh daniel-server docker ps | head -3`:

| hook | package reachable | median |
|---|---|---|
| `auto-approve-remote-ssh.sh` (`uv run --no-sync python`) | yes | 37 ms |
| `auto-approve-remote-ssh.sh` | no — fails open, one stderr line | 35 ms |
| `guard-permission-request.sh` (`uv python find` + `python -S -P`) | yes | 65 ms |
| `guard-permission-request.sh` | no — exits at the `cli.py` existence check | 1 ms |

The cost was not the finding; the two hooks were not interchangeable. On the payload above the
judge emitted nothing and the repo shim allowed: `readonly_remote_safe` returned no opinion
unless the parse yielded exactly one segment, while `classify_remote` walked each local stage.
The shim also reached `git`, `sed`, `awk`, `find`, `sort`, `apt`, `dpkg`, `crontab` and `pipx`
over ssh through per-command guards the package did not carry. So the decision of 2026-09-18
was "both hooks stay," pending the port.

**Decided 2026-09-18, later the same day: the repo shim retired** (the `# DECIDED:` marker sits
on `main` in `auto-approve-readonly.py`, replacing the one on `classify_remote`). dotfiles PR
#521 moved both differences into the package: `judge_segment` tries `readonly_remote_safe` and
`trusted_host_safe` on an `ssh`/`hl` segment of its own, so a local pipeline around the stage
is judged stage by stage (`ssh daniel-server docker ps | head -3` → `remote-readonly-check`,
`allow-list`), with a `2>&1` / `2>/dev/null` word stripped first; and the thirteen guards are
`checks/remote_guards.py`. The port found the package's word splitting unsound for a program text —
`ssh host "sed '1 w /x' f"` reaches the far shell as a write and quote-stripping read it as the
script `1` — so `readonly_remote_safe` now splits words the way ssh and the remote shell do. The
`_ssh` handler and the guards stay in `auto-approve-readonly.py` for the PreToolUse path
(local commands, and ssh under Manual mode's PreToolUse allow); the package holds the copy
that judges a PermissionRequest.
`.claude/hooks/tests/test_claude_guard_import.py::test_every_guard_carried_on_both_sides_reaches_the_same_verdict`
replays this suite's local vectors through both copies, so they cannot drift apart unnoticed;
`test_no_verb_is_guarded_on_one_side_of_the_boundary_and_bare_on_the_other` reads the package's
`REMOTE_GUARDED_VERBS` for the placement half. The verb tables converged on every guard-free
name that morning — 29 `TIER1` readers into `REMOTE_READONLY_VERBS` (dotfiles PR #520) and
`ping`, `ping6`, `tracepath`, `traceroute`, `uptimed` into `TIER1` — and a guarded verb moves
only with its guard, because the replay corpus cannot see a remote fail-open (4 of 1058
prompted records touch ssh). Since #2052 that convergence is structural rather than
hand-synced, and since #2078 it runs through one shared table: the package exports
`READONLY_BASE`, the names read-only under any argument on BOTH sides of the ssh boundary,
and each side extends it with its own delta — `REMOTE_READONLY_VERBS` adds the one the server
admits nowhere (`htop`, with its reason beside it in `claude_guard/tables.py`), and `TIER1`
in `_readonly_tables.py` adds the three that are read-only only locally (`cd`, `false`,
`printenv`). #2052 derived `TIER1` from the REMOTE table instead, so a name the package
added for the far shell widened local auto-approve on the next `chezmoi apply` with no edit
on this side. The verbs that read under most arguments but not all (`journalctl`, `dmesg`,
`ss`, `rg`, `sensors`, and since dotfiles #559 `nvidia-smi`) sit in neither table: each
side reaches them through its own guard, and since #2078 the package's are
`remote_guards.GUARDS` entries, so the shared-verdict replay above covers the five this
side also guards — as regex arms of `readonly_remote_safe` they ran before the table lookup
and sat outside it. `nvidia-smi` has no guard on this side (no NVIDIA hardware in the
fleet), so the replay does not exercise it. The CI
stand-in in `tests/conftest.py` carries a copy of `READONLY_BASE` for the runners that have
no dotfiles deploy; `test_the_ci_stand_in_matches_the_deployed_tables` diffs it on every
deployed-host commit. Measured demand is low either way: 800 of the 1064 ssh-led Bash
decisions in the 28 days to 2026-09-18 were settled by a settings rule with no hook involved.

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

**Read that last bullet as being about the NATIVE matcher only.** On this machine a hook restores
flag-level matching, and reading the bullet as universal has already cost something: a 2026-08-29
review proposed deleting about two dozen `deny`/`ask` rules in the chezmoi `settings.permissions.json`
as decorative, citing this section. They are not decorative. `allow-compound-bash.sh` — a
`PermissionRequest` hook, so it runs before the native engine reaches a verdict — routes any pattern
carrying an **interior** `*` to a real glob match against the whole command text
(`~/.claude/hooks/allow-compound-bash.sh:78-81`, `:278`). That is what enforces
`Bash(git commit *--no-verify*)`, `Bash(* | sh*)`, `Bash(find *-exec*)` and the `gh api *-X POST*`
family. `tests/hooks/allow-compound-bash.test.js` in the dotfiles repo proves it both ways: one test
fires those rules on the flag form, another proves they leave the everyday form of each command
allowed. The rules in THIS repo's `.claude/settings.json` are plain verb prefixes and the native
limitation applies to them unchanged.

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

## `git diff` on a SOPS path — why a pipe is denied and a flag is not

`.gitattributes:1` sets `diff=sops` on `ansible/vars/secrets.yml`, so `git diff`, `git show`
and `git log -p` decrypt the file before printing it. The plaintext lands in the terminal, the
scrollback and any agent transcript that captured the command. The repo-root `CLAUDE.md`
carries the rule (`--stat` / `--name-only` to see THAT it changed, `sops` to see WHAT); this
is the record of how the rule got its shape.

- **2026-08-24** — a reviewer ran the bare `git diff` during the homelab review (finding L-5),
  and the value it printed had to be rotated.
- **Until 2026-08-27** the `CLAUDE.md` remedy piped the diff through `grep -E '^[-+][a-z_]+:'`,
  without `-o`. Without `-o` grep prints the whole matching line — key *and* plaintext value —
  and that line leaked a freshly minted push token into a session transcript before the token
  was rotated.
- **Until 2026-08-29** the remedy was the `-o` form of that pipe. The user-level deny hook
  matched the `git diff` half regardless of what followed the pipe, so for a while the remedy
  the file named was denied by the rule printing it.
- **Since 2026-09-17** that deny lives in `claude_guard.deny`, behind
  `~/.claude/hooks/guard-pre-tool-use.sh` (it was `block-dangerous-bash.sh` before that). It
  denies every `git diff`/`show`/`log -p` naming a SOPS path unless a content-free flag is
  present.

The generalisation: a filter that leaks everything when mistyped is the wrong shape for the
job, and a flag that emits no content cannot leak however it is typed.
