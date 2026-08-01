# daniel-box handoff — 2026-08-01

Written for a Claude session running **on the server**. Everything below was verified on the
host unless explicitly marked otherwise.

Host: **daniel-box**, 10.0.0.215, Ubuntu 24.04, Ryzen 7 8845HS / Radeon 780M, 914 G root FS.

---

## 1. Two things need a decision before more work

### 1a. Docker is now installed on the k3s control-plane node

A full `initial_setup.yml` run (no `--tags`) at 18:56 installed Docker and created 12 bridge
networks. Both `docker` and `k3s` report `active`.

This was explicitly what the migration's slice ordering was designed to prevent: k3s ships its
own containerd plus flannel/kube-proxy iptables rules, and daniel-box was chosen to go first
*because it had no container runtime*. See `docs/k3s-migration/slice-0-cluster-foundation.md`.

Consequences:

- Docker's `DOCKER`/`DOCKER-USER` chains and FORWARD-policy handling now sit alongside k3s
  networking on the same host. Coexistence often works, but it is the exact interaction the
  plan deferred to slice 7 so it could be dealt with deliberately.
- `roles/setup/k3s` has a fail-closed guard (`Refuse to run on a host that already runs Docker
  containers`) that now **fails on this host**. Re-running `ansible/k3s-bringup.yml` will stop.

**Decide:** remove Docker from daniel-box to restore a clean k3s node, or accept coexistence
and drop that guard. If removing, `has_chezmoi`/`has_claude_code` runs must be scoped with
`--tags` so `docker_install` does not simply reinstall it — that is how it arrived.

Nothing here is on fire; k3s is still running. But do not treat it as the intended state.

### 1b. Claude Code is not installed

`/home/ubuntu/.local/bin/claude` does not exist, and `~/.local/share/installers/` contains only
`chezmoi-install.sh`. The `claude_code` role has never executed — `chezmoi_setup` fails first
and aborts the play. Not a PATH problem.

---

## 2. The immediate blocker

`chezmoi init --apply` fails:

```
stdout: ".bashrc has changed since chezmoi last wrote it?"
stderr: chezmoi: .bashrc: could not open a new TTY: open /dev/tty: no such device or address
```

The uv installer appends to `~/.bashrc` (it says so: "The installer edits ~/.bashrc for FUTURE
shells"), so chezmoi sees a modified target and wants to prompt before overwriting. Ansible has
no TTY.

**Likely fix**, untested: add `--force` to the init in `roles/setup/chezmoi_setup/tasks/main.yml`.

```yaml
cmd: >-
  /home/{{ sys_user }}/.local/bin/chezmoi init {{ chezmoi_setup_repo }} --apply --force
```

Consider this carefully rather than pasting it: `--force` overwrites *every* locally-modified
target without asking, on every run. That is usually right for a machine whose home directory is
declared by the dotfiles repo, and wrong if anything on the host legitimately hand-edits a
managed file. A narrower alternative is to resolve the `.bashrc` drift once by hand and leave
the role prompt-free.

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
| Docker | Installed (see §1a) |

---

## 4. Next steps, in order

All of these need a TTY, which is why they were not done from an agent session.

1. **Resolve §1a** — decide Docker's fate on this host.
2. **Fix the chezmoi TTY blocker** (§2), then:
   ```bash
   cd ~/server && git pull
   uv run ansible-playbook ansible/initial_setup.yml --tags chezmoi,claude_code
   ```
   Use `--tags`. A bare run reinstalls Docker.
3. **`chezmoi apply`** in an interactive shell, so `install-cli-tools.sh` can sudo and add the
   WezTerm repo. Until then it exits 1 and, being `run_once_after`, retries every apply.
4. **`claude login`** once `claude_code` has installed the binary.
5. **`rm ~/.ssh/config`** — the `.chezmoiignore` fix (dotfiles PR #157, merged) stops chezmoi
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
