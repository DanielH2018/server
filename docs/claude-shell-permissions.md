# Claude Shell Permissions — What Actually Decides

Full reference for how this repo's Bash and `kubectl` commands get auto-approved, prompted, or
denied. The repo-root `CLAUDE.md` carries the short always-on summary; this file holds the detail
behind it — the tables, the measured dates, and the caveats.

## Shell Commands — Shape Them to Auto-Approve
The user-level PreToolUse hook (`guard-pre-tool-use.sh`, the dotfiles `claude_guard`
package's `readonly.py`) auto-approves Bash it can **prove is read-only**, so those run
without a permission prompt. It applies only when the session's cwd is `$HOME` itself or
inside this repo or the dotfiles checkout, because `git` runs a repo's own `core.fsmonitor`
and pager config; the same rule refuses `git -C`, `--git-dir` or a `cd` that points git
anywhere else. Write exploratory and read-only commands to fit it. Anything that writes or
executes still prompts, by design.

**Auto-approves (no prompt):**
- Single read-only commands and pipelines: `grep … | sort | head`
- `rg` on the same terms as `grep`; the classifier answers `allow · read-only: rg`. Neither
  tool is better for permissions, so pick on merit.
- Read-only stages sequenced with `;`, `&&`, `||`, or newlines: `cd dir && grep … *.j2`
- Write-free redirects: `… 2>/dev/null`, `>/dev/null 2>&1`
- Read-only `git`/`docker`/`find` (no `-exec`/`-delete`) and read-only `awk`/`sed`.
  `printenv`, `docker inspect` and `systemctl show`/`cat` prompt: each prints environment
  values (dotfiles #628).
- Read-only host/package queries: `apt list`/`apt show`/`apt policy`, `apt-cache …`,
  `dpkg -l`/`-L`/`-s`/`-S`, `dpkg-query …`, `apt-mark showmanual`, `pipx list`,
  `lsb_release`, `sensors`, `mailq`, `crontab -l` (the write forms — `dpkg -i`,
  `apt install`, `crontab -r`, `sensors -s`, … — still prompt)
- Those same read-only commands run over `ssh daniel-server`/`ssh daniel-pi` — the remote
  command is classified exactly like a local one, so `ssh daniel-pi docker logs wg-easy
  --since 24h 2>&1 | tail -20` goes through. (Pick a host that has Docker. Neither
  cluster node does, so use `kubectl logs` locally for cluster logs.) Connection flags (`-i`,
  `-p`, `-l`, `-q`, `-o` with a connection-only key) are fine; forwarding/proxying (`-L`/`-R`/`-D`/`-A`/`-F`,
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

Source of truth: the dotfiles `claude_guard` package, deployed to
`~/.local/share/claude-guard`. `claude_guard/readonly.py` holds the stage walk, the redirect
rules and the `ssh`, `docker`, `systemctl` and `ip` handlers. `checks/remote_guards.py` holds the
per-verb guards, and `tables.py` holds the verb table, the trusted hosts and the secret-path
pattern. The package's `tests/test_readonly.py` carries the approve and reject tables. This repo
carries no copy of the classifier.

The package shares one verb table across the ssh boundary. `READONLY_BASE` names the verbs that
are read-only under any argument on both sides, and each side adds its own delta. A verb that
reads under most arguments but not all (`journalctl`, `dmesg`, `ss`, `rg`, `sensors`,
`nvidia-smi`) sits in neither table and goes through a guard in `remote_guards.GUARDS`. A guarded
verb moves between tables only together with its guard.

The ssh case on **PermissionRequest** is the user-level `guard-permission-request.sh` (the
dotfiles `claude_guard` judge) and only that. Claude Code evaluates `ask` rules whatever a
PreToolUse hook returns, so a PreToolUse decision alone never reaches an ask-listed command. This
repo registers no PermissionRequest hook of its own.

**A machine without the dotfiles deploy gets no auto-approve at all, rather than a stale local
copy:** the classifier is part of the deployed hook. This repo's two PreToolUse guards take the
other posture on a failed `cd`, which `docs/claude-tooling.md` *Hooks* describes
(`run-hook.sh --ask-on-cd`).

**PermissionRequest hooks do not fire in a normal session.** `Bash(ssh:*)` and `Bash(curl:*)` are
not in the `ask` tier, and it is the *ask rule* that routes a call through a PermissionRequest
hook. Without it, ssh and curl fall through to the auto-mode classifier, which reads the whole
command against the hosts and domains named in the auto-mode environment setting. The judge's
`curl_safe`, `readonly_remote_safe` and `trusted_host_safe` checks remain because they carry
**Manual mode**, where no classifier runs.

### `kubectl` — what actually decides
**Read this before trusting the per-verb allow-list below: in a normal session that list decides
nothing.** Sessions default to auto mode (`defaultMode: auto`) with `autoMode.classifyAllShell: true`
in user settings, and that setting **suspends every `Bash()` allow rule while auto mode is active**.
So the classifier judges each `kubectl` command on its full text, and the verb tiers below only apply
in Manual mode or if `classifyAllShell` is turned off. Treat them as a fallback, not as the
mechanism.

**The cluster credential is the ceiling for `kubectl`, and it is lower than any of this.** Plain `kubectl`
authenticates as `system:serviceaccount:kube-system:homelab-readonly`, which holds `get list watch`
and nothing else (`k3s_readonly_sa_name` in `ansible/roles/setup/k3s/defaults/main.yml`). Every write
verb is refused by RBAC. `kubectl auth can-i` answers **no** for `delete pods`, `delete pvc` and
`pods/exec`, and `kubectl exec` returns *"cannot create resource pods/exec"* rather than a
permission prompt.

**`sudo k3s kubectl` is not an escape hatch — `sudo` is in `permissions.deny`**, so it is blocked
outright, not prompted. With the
read-only SA on one side and denied `sudo` on the other, **Ansible is the only write path to this
cluster.**

**That ceiling bounds `kubectl`, not the session.** A session holds everything its Unix user
holds. Remote Control sessions and new interactive sessions run as the `claude` user, which holds
neither the age key that decrypts the become password nor the repo owner's `gh` token
(`docs/claude-agent-user.md`, *What the `claude` user cannot do*). A session that still runs as
`sys_user` holds both, and the header of `ansible/roles/setup/common/tasks/agent_user.yml` lists
the rest of what that user can read. With the age key, such a session can run `deploy.sh` and
`initial_setup.yml` as root on every host. With the token, it can merge its own PR past the master
review gate through the REST bypass, which is how `land.sh` merges every landing. The GitOps
deployer then applies master with `become`. So for a `sys_user` session the boundary is the
permission layer this page describes, plus the trail every change leaves: the review gate refuses
a direct push to master from any token, so every change reaches master as a PR. The unattended
Renovate session runs as its own user, on a token that can neither push nor merge
(`ansible/roles/setup/renovate_agent/CLAUDE.md`).

**A `Bash()` rule matches on `kubectl <verb>` and nothing finer — flags and sub-subcommands in the
rule are decorative.** The OTEL `tool_decision` stream shows:

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
flag-level matching, so the `deny` and `ask` rules in the chezmoi `settings.permissions.json`
that look flag-level are not decorative. `allow-compound-bash.sh` is a `PermissionRequest` hook,
so it runs before the native engine reaches a verdict. It routes any pattern carrying an
**interior** `*` to a real glob match against the whole command text. That enforces
`Bash(git commit *--no-verify*)`, `Bash(* | sh*)`, `Bash(find *-exec*)` and the `gh api *-X POST*`
family. `tests/hooks/allow-compound-bash.test.js` in the dotfiles repo proves it both ways: one
test fires those rules on the flag form, another proves they leave the everyday form of each
command allowed. The rules in THIS repo's `.claude/settings.json` are plain verb prefixes, and
the native limitation applies to them unchanged.

**That limitation is exactly why `classifyAllShell` is on.** A `Bash()` rule cannot see a flag, so
`Bash(kubectl apply *)` also approves `apply --prune`; the classifier reads the whole line and can.
A 2026-08-08 decision blanket-allowed `exec`, `cp` and `port-forward` because no rule could
distinguish `exec -- cat …` from `exec -- rm -rf …`. That reasoning no longer binds. The classifier makes
that distinction, no allow rule names those verbs, and RBAC refuses `exec` regardless.

The tiers below describe the **Manual-mode fallback**, not what happens in a normal session:

- **Auto-approved, read-only:** `get`, `logs`, `describe`, `top`, `explain`, `events`,
  `api-resources`, `api-versions`, `version`, `diff`, `wait` and `auth` (this repo's
  `.claude/settings.json`), plus `config view` and `config current-context` (the chezmoi
  `settings.permissions.json`).
- **Prompted in Manual mode, classifier-judged in auto mode:** every mutating verb — `apply`,
  `create`, `patch`, `set`, `scale`, `label`, `annotate`, `cordon`, `uncordon`, `rollout`,
  `exec`, `cp`, `port-forward`. No allow list names them. The chezmoi
  `settings.permissions.json` records them as absent by intent rather than by omission, and RBAC refuses
  each write regardless, because plain `kubectl` authenticates as the read-only ServiceAccount.
- **`delete` is denied outright**, not prompted — `Bash(kubectl delete:*)` is in `permissions.deny`
  (user settings), which is evaluated before both the ask tier and the classifier and cannot be
  cleared by stating intent. RBAC already refuses it; the deny exists so a future credential
  widening cannot silently hand back `delete` against Longhorn PVCs and their B2 backup chain. Per-verb matching means the routine, safe `delete pod` (the Deployment recreates it)
  is denied too — accepted, because RBAC refuses that as well.
- **`drain` and `taint` are classifier-judged, not prompted** — they sit in `autoMode.soft_deny`, so
  the classifier blocks them unless you name the node and the operation. A soft block *clears on
  explicit user intent*, which is precisely why `delete` was moved up to a hard deny instead.
- **Never allow-listed, so the classifier judges them:** `replace` (`--force` = delete + recreate),
  `proxy` (a local API gateway authenticating as you), `debug` (`debug node/…` mounts the host
  filesystem in a privileged pod), `attach`, `run`, and `edit` (interactive — it just hangs for an
  agent).

Ansible is the only write path to the cluster. Deploy through `./scripts/deploy.sh --tags <svc>`
rather than reaching for a write verb.

## `git diff` on a SOPS path — why a pipe is denied and a flag is not

The `.gitattributes` entry for `ansible/vars/secrets.yml` sets `diff=sops`, so `git diff`,
`git show` and `git log -p` decrypt the file before printing it. The plaintext lands in the
terminal, the scrollback and any agent transcript that captured the command. The repo-root
`CLAUDE.md` carries the rule: `--stat` or `--name-only` shows THAT the file changed, and `sops`
shows WHAT changed.

The user-level deny, `claude_guard.deny` behind `~/.claude/hooks/guard-pre-tool-use.sh`, denies
every `git diff`, `git show` and `git log -p` naming a SOPS path unless a content-free flag is
present. The rule is a flag rather than a filter because a filter that leaks everything when
mistyped is the wrong shape for the job. A `grep -E '^[-+][a-z_]+:'` pipe without `-o` prints the
whole matching line, key and plaintext value together. A flag that emits no content cannot leak
however it is typed.

## Inspecting a secret without decrypting it

`claude_guard.deny.sops_decrypt`, behind the same `~/.claude/hooks/guard-pre-tool-use.sh`,
denies every `sops` command that decrypts. It matches `-d` alone or inside a combined short
flag, `--decrypt`, and the `decrypt`, `exec-env` and `exec-file` subcommands. `sops -d
--extract '["one_key"]'` is denied too, because one value in the transcript is still a leak.
The guard matches the command text, so a here-document or a `python3 -c` whose body
contains such a command is denied as well. The guard is user-level (dotfiles), not in this
repo's `.claude/hooks/`.

What an agent can check without plaintext:

- **Whether a key exists.** SOPS encrypts values and leaves top-level key names in
  plaintext, which `scripts/secrets_mgmt/rotation_tools.py:sops_names` also relies on.
  `grep -c '^<name>:' ansible/vars/secrets.yml` prints `0` or `1`.
- **Whether its value is ciphertext.** `grep -c '^<name>: ENC\[' ansible/vars/secrets.yml`
  prints `1` for a key SOPS encrypted. `ansible/.sops.yaml` sets no `encrypted_regex` or
  `unencrypted_suffix`, so every value carries the `ENC[` prefix.

Both commands print a count and auto-approve. Whether a value *decrypts* is proven by the
deploy that reads it. A runbook step that decrypts on purpose, such as the break-glass
decrypt check, is marked operator-only where it appears.
