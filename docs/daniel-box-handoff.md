# daniel-box handoff — 2026-08-01

Written for a Claude session running **on the server**. Everything below was verified on the
host unless explicitly marked otherwise.

Host: **daniel-box**, 10.0.0.215, Ubuntu 24.04, Ryzen 7 8845HS / Radeon 780M, 914 G root FS.

---

## 1. One thing needs a decision before more work

### 1a. Docker was installed on the k3s node — RESOLVED, removed 2026-08-01

A full `initial_setup.yml` run (no `--tags`) at 18:56 installed Docker and created 12 bridge
networks on the k3s control-plane node. That was explicitly what the migration's slice ordering
was designed to prevent: k3s ships its own containerd plus flannel/kube-proxy iptables rules,
and daniel-box was chosen to go first *because it had no container runtime*. See
`docs/k3s-migration/slice-0-cluster-foundation.md`.

**Resolved by removing Docker**, restoring a clean k3s node. Nothing was lost — Docker had zero
containers, zero images and zero volumes; only the 12 empty bridge networks.

Confirmed gone by direct inspection: the six `docker-ce`/`containerd.io` packages,
`/usr/bin/docker`, `/var/lib/docker`, `/var/lib/containerd`, `/etc/docker`, the APT repo +
keyring, the whole `10.200.0.0/16` address pool, and every `docker0`/`br-*` bridge — `cni0` is
the only bridge left on the host.

**Not verified:** the removal script also deletes the `DOCKER*` iptables/ip6tables chains and
resets the `FORWARD` policy to ACCEPT, but it runs under `set -uo pipefail` *without* `-e`, and
its log is root-owned — the session that ran it could not read the log or query iptables. Treat
the chain cleanup as unconfirmed until someone runs:

```bash
sudo sh -c 'iptables -S | grep -ci docker; iptables -t nat -S | grep -ci docker; iptables -S FORWARD | head -3'
```

Both counts should be 0. If they are not, the residue is inert rather than dangerous — orphan
chains referencing deleted `br-*` interfaces never match, and k3s ran fine under Docker's
`FORWARD DROP` for hours — so this is cleanliness, not risk.

k3s was untouched throughout — it runs its **own** containerd out of
`/var/lib/rancher/k3s/data/…/bin/containerd`, entirely separate from the `/usr/bin/containerd`
that served `dockerd`. `net.ipv4.ip_forward=1` survives the removal: it is persisted at
`/etc/sysctl.conf:65` by the `initial_setup` role, not by Docker.

With `/usr/bin/docker` gone, the `roles/setup/k3s` fail-closed guard (`Refuse to run on a host
that already runs Docker containers`) passes again and `ansible/k3s-bringup.yml` is unblocked.

**This is now enforced, not remembered.** `docker_install` carries `when: has_docker`;
`has_docker` defaults true fleet-wide in `group_vars/all.yml` and is set **false** in
`host_vars/daniel-box.yml`. A bare `initial_setup.yml` on this host no longer reinstalls Docker,
so runs here no longer need `--tags` to stay safe. Guarded by
`ansible/tests/test_k3s_host_has_no_docker.py`.

### 1b. Claude Code is installed — RESOLVED 2026-08-01

At the time of writing, `/home/ubuntu/.local/bin/claude` did not exist: the `claude_code` role
had never executed, because `chezmoi_setup` failed first (§2) and aborted the play.

Installed since, at 19:20 — `/home/ubuntu/.local/bin/claude` is a symlink to
`~/.local/share/claude/versions/2.1.220`, and a session runs on this host. `claude login` (old
§4 step 4) is therefore also done.

It was installed **by hand, not by the role**: `~/.local/share/installers/` still contains only
`chezmoi-install.sh`, and the role fetches `claude-install.sh` there before running it. So this
does *not* evidence that `chezmoi_setup` now gets past §2 — that verification is still open. The
role stays idempotent regardless: its install task is `creates: ~/.local/bin/claude`, which is
now satisfied, so it will skip rather than reinstall.

---

## 2. The immediate blocker — fixed, needs verifying on the host

`chezmoi init --apply` was failing:

```
stdout: ".bashrc has changed since chezmoi last wrote it?"
stderr: chezmoi: .bashrc: could not open a new TTY: open /dev/tty: no such device or address
```

**Cause:** uv's installer appends `export PATH="$PATH:$HOME/.local/bin"` to `~/.bashrc` during
bring-up, before chezmoi ever runs on this host. chezmoi then sees a modified target and wants
to confirm before overwriting; Ansible has no TTY. On-disk `.bashrc` was 127 lines against the
managed 97.

**Fix applied:** `--force` on the init in `roles/setup/chezmoi_setup/tasks/main.yml`.

Safe here because the managed `dot_bashrc` delegates PATH to the shared shell config, and
daniel-server runs the same dotfiles with `~/.local/bin` on PATH — the uv line is redundant
drift. But note the standing consequence: **every apply now silently discards hand-edits to
managed files.** Anything host-specific belongs in the dotfiles, or in one of the `*.local.*`
files `.chezmoiignore` already excludes — not edited in place.

Not yet run on the host. Verify the next play gets past this task.

---

## 3. What is already working

| Component | State |
|---|---|
| k3s | Single-node cluster, control-plane, embedded etcd (`--cluster-init`) |
| MetalLB | Installed, pool `10.0.0.240-250` |
| Longhorn | Installed, **1 replica** (2 nodes not yet joined) |
| Longhorn backup | Target set to B2, `backuptarget default` reported `available: true` |
| `gh` | Installed, authenticated as DanielH2018, `credential.helper = !gh auth git-credential` |
| chezmoi | Binary installed; dotfiles cloned and applied |
| CLI tools | node, eza, fastfetch, curlie, sd, starship, fzf, zoxide, rg, gron, nvim, yazi |
| Distro packages | bat, fd-find, gron, shellcheck, lua5.4, btop, chafa, zsh + plugins |
| Docker | **Removed** 2026-08-01 — gated off by `has_docker: false` (see §1a) |
| Claude Code | Installed (2.1.220), logged in, running on this host (see §1b) |

---

## 4. Next steps, in order

§1a (Docker), §1b (Claude Code) and old step 4 (`claude login`) are done. What is left needs a
TTY, which is why it was not done from an agent session.

1. **`chezmoi apply`** in an interactive shell, so `install-cli-tools.sh` can sudo and add the
   WezTerm repo. Until then it exits 1 and, being `run_once_after`, retries every apply.
2. **`rm ~/.ssh/config`** — the `.chezmoiignore` fix (dotfiles PR #157, merged) stops chezmoi
   *managing* that file here, but does not delete the copy already deployed.

---

## 5. Slice 0 exit criteria still unproven

From `docs/k3s-migration/slice-0-cluster-foundation.md`. The bring-up playbook asserts node
readiness, the etcd datastore, and the absence of bundled Traefik/servicelb — all passed. These
three were deliberately left for a human:

1. A LoadBalancer Service gets a pool IP **and answers from another machine on the LAN**. The
   ARP/L2 half is what silently fails.
2. A Longhorn PVC reaches `Bound`.
3. **An object is actually written to B2.** `available: true` proves credentials and
   reachability, *not* that anything was stored. Only listing the bucket settles it — and
   "backup reports success while storing nothing" is the specific failure slice 0 exists to
   prevent.

One command covers 1 and 2:

```bash
sudo k3s kubectl create deploy smoke --image=traefik/whoami
sudo k3s kubectl expose deploy smoke --port=80 --type=LoadBalancer
printf 'apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: smoke-pvc\nspec:\n  accessModes: [ReadWriteOnce]\n  storageClassName: longhorn\n  resources:\n    requests:\n      storage: 1Gi\n' | sudo k3s kubectl apply -f -
sleep 25
sudo k3s kubectl get svc smoke pvc/smoke-pvc
```

Then `curl http://<EXTERNAL-IP>/` **from a different machine**, and clean up with
`sudo k3s kubectl delete deploy/smoke svc/smoke pvc/smoke-pvc`.

---

## 6. Traps already paid for — do not rediscover these

- **`ansible_env.HOME` is `/root`.** `initial_setup.yml` gathers facts escalated on purpose, so
  `ansible_env` reflects root even inside a `become: false` task. Use `/home/{{ sys_user }}`.
  Guarded by `ansible/tests/test_per_user_home_resolution.py`.
- **APT keyrings must set an explicit mode.** `initial_setup` sets `UMASK 027`; a keyring created
  by a `command` (e.g. `gpg --dearmor`) lands 0640, and apt's unprivileged `_apt` user then
  reports the repo as *unsigned*. This silently broke Docker's install for weeks. Guarded by
  `ansible/tests/test_apt_keyring_permissions.py`.
- **`DanielH2018/dotfiles` is private**; `DanielH2018/server` is public. That asymmetry hid the
  missing credential until chezmoi tried to clone.
- **chezmoi's `--promptBool`/`--promptString` do not satisfy `promptBoolOnce`/`promptStringOnce`.**
  Seed the answers into `~/.config/chezmoi/chezmoi.toml` before init instead — that is what the
  role's `chezmoi-seed.toml.j2` does.
- **Kopia backs up host bind paths to B2.** When service config moves onto Longhorn PVs, that
  path still exists but is empty of live data, so Kopia keeps *reporting success while backing
  up nothing*. Every migration slice must confirm data in its new backup path before the Docker
  copy is retired.
- **`peanut` publishes upsd on `127.0.0.1:3493`** — loopback only. Home Assistant cannot reach
  it from another host until that publish is widened, which is a security-relevant change to a
  shutdown-critical service.
- **`except OSError, yaml.YAMLError:` in `filter_plugins/toposort.py` is valid**, not a bug —
  Python 3.14 (PEP 758) allows unparenthesized exception groups. It only looks broken under an
  older interpreter.

---

## 7. Deferred work

- **MCP wiring for Claude Code on daniel-box.** Agreed in scope, never implemented. MCP servers
  live in `~/.claude.json`, which is machine-local and not chezmoi-managed. `homelab_mcp_token`
  is already in SOPS. Confirm the real endpoint against a working config rather than guessing.
- **Telemetry** — deliberately skipped. daniel-server's otel-collector binds OTLP to its own
  loopback for Claude Code on *that* host.
- **`gh` token is stored in plaintext** at `~/.config/gh/hosts.yml` (no keyring on a headless
  box) and carries full account scope. A read-only deploy key for the dotfiles repo would be
  tighter; it diverges from daniel-server, which is why it was not chosen.
- **Longhorn stays at 1 replica** until daniel-server joins at slice 7. Failover does not exist
  before then — resilience arrives last in this ordering, by design.

---

## 8. Landed this session

Server repo: #46 (k3s slice 0 + `platform` key), #47 (Docker keyring umask), #48 (chezmoi +
claude_code roles), #49 (per-user home resolution), #50 (github_cli role), #52 (chezmoi config
seeding), #53 (deferred distro packages). #51 was a duplicate, closed.

Dotfiles repo: #157 (`.chezmoiignore` for daniel-box + deb822 `gh_ready`), merged and applied on
the workstation. **Not yet applied on daniel-box** — step 3 above covers it.
