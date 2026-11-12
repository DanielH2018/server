# Claude Code root helpers — the takeover check and rootq

Two narrow grants let a Claude session read or dry-run as root without general sudo. The
`hosts/components/claude-code/` host component ships both, and its `CLAUDE.md` points here.
This page holds what each helper runs, what it prints, how to widen it and how to take it
back.

## The takeover check

`/usr/local/sbin/homelab-takeover-check` runs a D5 flip PR's pre-merge dry runs as root, behind
one `NOPASSWD` line in `/etc/sudoers.d/homelab-takeover-check`: `flux diff kustomization` or
the server-side `kubectl diff`, then the RBAC takeover census (issue #5494). The operator
granted it on 2026-10-17 ("Let's add those 3 to your allowed commands"). It exists on daniel-box
only, where `claude_code_takeover_check_enabled` is true.

- **What root runs:** `/usr/local/bin/k3s kubectl` and `/usr/lib/homelab/bin/flux`, the
  root-owned flux CLI that `homelab-tools` pins at the cluster's `flux_version`
  (`hosts/packages/homelab-tools/package.yml`). Every flag is written in the script. The only caller input that reaches either binary
  is a validated tag, and a path to a root-owned copy of the user's render.
- **What root never runs:** anything the invoking user can write, and Kustomize over the user's
  files. `kubectl kustomize` builds each tag as `SUDO_USER`, and root diffs only the built file.
  The census, which is the user's Python, runs as `SUDO_USER` through `runuser`.
- **Which diff a tag gets:** `flux diff` when the tag's own Kustomization carries `postBuild`,
  so Flux's substitution is exercised; `kubectl diff` otherwise. The wrapper finds that
  Kustomization by `metadata.name` in `flux/<tag>.yaml`. A `-snapshot`, `-verify` or `-deferred`
  directory has no flux file of its own, so its search runs over the base tag's file (#5575).
- **What it prints:** object names, field paths and exit codes. Full outputs stay in
  `/var/lib/homelab-takeover-check/<label>/`, mode 0700.
- **Who installs what:** daniel-box's host package ships the wrapper and the grant in one
  version, so the grant never names a missing command. The package depends on `homelab-tools`,
  whose builder refuses a flux tarball off its pin, and the updater installs both in one run.
  The wrapper creates the state directory.
- **To take it back:** set `claude_code_takeover_check_enabled: false`. The next package
  version drops the grant and the wrapper. The state directory stays, because its per-label
  contents are not a list reconcile can name; remove it by hand. flux stays, because
  `homelab-tools` ships it.
- **To widen who holds it,** add to `claude_code_takeover_check_users`; one line per user.

## The root inspector

`rootq` reads root-only host state for a session that has no general sudo. It runs
`/usr/local/sbin/homelab-rootq` (`hosts/components/claude-code/files/homelab_rootq.py`) as root, behind one `NOPASSWD`
line in `/etc/sudoers.d/homelab-rootq` (issue #6323), which the host package ships. The operator approved the shape on
2026-10-25 and reviews the grant before it lands. It exists where `claude_code_rootq_enabled`
is true: daniel-box and daniel-server. `claude_code_rootq_users` grants the operator's account
only, where fan-out batches and the operator's sessions run. The agent user stays without
sudo: the operator replaced its one sudo grant with a socket on 2026-10-10 (#4297), and
`NoNewPrivileges=yes` in `claude-rc.service` stops sudo there anyway. The agent reaches the
same script through a socket instead (*The agent's socket* below). The four files that decide
what the grant reaches, and the four that decide what the socket reaches, are on the
approval list the lander checks.

Everything rootq accepts is an explicit allowlist, and anything not listed is refused. The
list is deliberately minimal: the files and sources #6323's sessions needed. It grows only when
a need appears.

- **Tier 1 prints no file content.** `stat PATH` prints mode, type, owner, group, size and
  mtime. `sha256 PATH` prints the hash. `matches PATH REPO_FILE` exits 0 when the live file is
  byte-identical to REPO_FILE in the caller's checkout, and 1 when it differs. The shim
  (`hosts/components/claude-code/files/rootq.sh`) hashes REPO_FILE as the caller and passes only the hash. Root therefore
  receives `matches PATH HEX` and never opens a caller-named file. The shim refuses a REPO_FILE
  that is absolute, holds `..`, or resolves outside the checkout. It also takes the 64-hex
  SHA-256 in place of REPO_FILE and passes it on as it is, which gains the caller nothing a
  file in a checkout could not.
- **Tier 2 prints, from fixed sources only:** `nft list ruleset`, `ufw status verbose`,
  `sshd -T`, `crontab -l -u root`, and `cat` of `/etc/netplan/*.yaml`. Every argv is written
  in `hosts/components/claude-code/files/homelab_rootq.py:COMMANDS`, and no caller flag
  reaches any of them. There is no general `cat`.
- **Which paths:** `hosts/components/claude-code/files/homelab_rootq.py:PATHS` lists the
  files tier 1 takes, as a directory plus a glob over one file name, and `CAT` lists the ones
  tier 2 prints. A path must resolve to itself, so a symlink, `..` or `//` is refused. Every
  directory above it must be root-owned and writable by root alone. It must also miss every
  pattern in `DENY`, an independent second layer. `/var/lib/gitops-deploy` is not listed,
  because `sys_user` owns it and the caller already reads it.
- **A DENY path answers `stat` alone, and only when `PATHS` names it.** The operator ruled
  this on 2026-10-26 (#6369). `stat` of such a path prints its mode, type, owner and group,
  plus `deny=yes`. It never prints size or mtime, because a size gives away a secret's
  length. A missing one is refused with `does not exist`. `cat`, `sha256` and `matches` still
  refuse every DENY path. DENY never widens what rootq reaches. rootq tests the allowlist
  before it resolves a DENY path, so a DENY path off the list gets the same refusal whether
  it exists or not. The DENY entries in `PATHS` are exact names:
  `/etc/claude-rc/alert-webhook.env`, and the four paths under
  `/etc/rancher/k3s/manifests/registry-jobs/` and its directory (#6988). That tree stays denied as a whole, because it
  holds every staged role's `secret.yaml`.
- **Two DENY paths are exempt by exact name.** The operator ruled this on 2026-11-02 (#7007).
  `hosts/components/claude-code/files/homelab_rootq.py:DENY_EXEMPT` names the two registry
  mirror GC Jobs, `registry-jobs/ghcr-mirror-gc-job.yaml` and
  `registry-jobs/dockerhub-mirror-gc-job.yaml`, which hold no secret. `denied()` returns
  false for them, so every tier-1 verb answers them. Tier 1's credential check still applies to what they hold. The exemption takes
  exact names only, never a glob. rootq cannot stat a file in the agent user's home,
  which root does not own. The role therefore makes `~/.config/gh` and `~/.config/homelab-ui`
  0710 under `claude_code_agent_operator_read`, so the operator account stats the 0600 token
  and login files as the operator, without listing either directory or reading either file.
- **What it prints:** output that matches `REFUSE`, such as a private key, a bearer token or a
  webhook URL, is withheld whole. The rest is redacted by default. A value prints only when it
  matches one of the token classes the script defines in one block, such as a number of up to
  seven digits, a duration, an address, a port, an interface name, a path under a standard
  root, or a keyword of the printing program's grammar. Under a key whose name could name a
  credential, such as `pass`, `pin`, `auth`, `key` or `token`, the value is always replaced,
  digits included. A crontab command keeps its binary, flag names and paths, and every other
  argument becomes `<arg>`. A line with no key, list or cron structure becomes `<line>`.
  Output stops at 256 KiB.
- **To expand it:** a new tier-1 path needs a `PATHS` entry, a must-pass case and a
  must-refuse neighbour case in `hosts/components/claude-code/tests/test_rootq.py`: a sibling the new entry still does not
  admit. A new tier-2 source also needs a structural argument, written at `CAT` or `COMMANDS`,
  for why the filter redacts its grammar cleanly, with must-pass and must-redact cases in
  `hosts/components/claude-code/tests/test_rootq_redaction.py`.
- **To take it back:** set `claude_code_rootq_enabled: false` in the host's host_vars. The next
  package version drops the socket, the sudoers file, the shim and the script.

### The agent's socket

The operator approved this on 2026-10-29 (#6350). `homelab-rootq.socket`
(`hosts/components/claude-code/templates/homelab-rootq.socket.j2`) listens on `/run/homelab-rootq.sock`, mode 0660, group
`homelab-rootq`, with `Accept=yes`. For each connection systemd runs the root helper
`hosts/components/claude-code/files/homelab_rootq_socket.py`, installed as
`/usr/local/libexec/homelab-rootq-socket`, through `homelab-rootq@.service`. The agent runs
`rootq-socket` (`hosts/components/claude-code/files/rootq_socket.py`, the same verbs as `rootq`). `agent_access.yml` puts
each present agent in the group. The group is the whole grant, because the helper checks no
uid.

- **The helper adds no allowlist of its own.** It reads one line, `VERB [PATH [HEX]]`, of at
  most 4200 bytes within 5 seconds, and refuses anything else before rootq runs: a verb outside
  `hosts/components/claude-code/files/homelab_rootq_socket.py:ARITY`, a wrong argument
  count, a non-ASCII byte, a PATH holding an empty, `.` or `..` component, and a `matches` hash
  that is not 64 lowercase hex. It then runs `/usr/local/sbin/homelab-rootq` by absolute path
  with an argument list, a fixed environment and stdin closed, never a shell. Whether a PATH is
  on the allowlist is rootq's decision, so the caller gets rootq's own refusal text. The
  helper's own refusals use the same `refused: ...` text and exit 2.
- **The answer is one JSON line** with `exit`, `stdout` and `stderr`, because a socket carries
  no exit status. `rootq-socket` fails with 70 on an answer that is missing, truncated or not
  that object, and with 69 when it cannot connect.
- **`matches` sends a hash, never a file name.** The client hashes REPO_FILE as the caller, with
  the shim's checks, and sends `matches PATH HEX`.
- **The service keeps the host's network namespace.** `nft list ruleset` and `ufw status
  verbose` read it, and a `PrivateNetwork=` would print an empty ruleset instead of refusing.
- **A session sees the group after `claude-rc.service` restarts.** Group membership reaches a
  process at its start. The weekly `claude-rc-restart.timer` restarts the service, or the
  operator restarts it, and an older session gets exit 69 from `rootq-socket` until then.
- **The approval list covers the socket's four files.** #6350 expected the sudo grant's
  entries to cover it, and they cover only the script. The helper, the client and the two unit
  templates are listed in `hosts/components/claude-code/templates/claude-land-approval-paths.j2` and in `FLOOR` in
  `ansible/tests/setup/test_claude_lander.py`.
- **To take it back:** set `claude_code_rootq_socket_enabled: false`, or
  `claude_code_rootq_enabled: false`, in the host's host_vars. The next package version drops
  the socket, its units, the helper and the client, and `claude-agent-setup` drops the agent
  from the group. The operator's sudo path is untouched. The setting is
  `claude_code_rootq_socket_enabled` in the host's `host_vars`.
- **How to apply it:** a new version of the host's package, which the host's updater installs.
