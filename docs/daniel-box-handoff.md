# daniel-box handoff — 2026-08-01

> **HISTORICAL — superseded 2026-08-14.** This was written *before* the k3s migration, when
> `daniel-server` was still the Docker host and `daniel-box` was the new machine being handed
> over. Statements below about what runs where (notably "daniel-server is the Docker host",
> the Kopia-backs-up-bind-paths section, and "Kopia and Longhorn share one B2 bucket") no
> longer hold: Docker is gone from both cluster nodes and Kopia is retired. Kept for the
> hardware/OS/network facts and the reasoning captured at handoff time. Current state:
> repo-root [`README.md`](../README.md).

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
| Longhorn | Installed, **1 replica** — proven on a bound PVC after the fix in §5 |
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

## 5. Slice 0 exit criteria — all met

From `docs/k3s-migration/slice-0-cluster-foundation.md`. The bring-up playbook asserts node
readiness, the etcd datastore, and the absence of bundled Traefik/servicelb — all passed. Three
were left for a human. Proving them found two real defects (§5's *replica failure* and §5a),
both since fixed; the cluster was rebuilt on 2026-08-01 and every criterion passes on that
rebuild:

| Criterion | State |
|---|---|
| One `Ready` control-plane node backed by etcd | pass — `control-plane,etcd`, `v1.36.2+k3s1` |
| No Traefik, no `svclb-*` pods | pass |
| Node registered on its canonical address | pass — `10.0.0.215` (was 10.0.0.153, see §5a) |
| LoadBalancer gets a pool IP | pass — `10.0.0.240`, in-pool |
| …**and answers from another LAN machine** | pass — curled from daniel-server |
| Longhorn PVC reaches `Bound` | pass |
| …**at 1 replica** | pass — `numberOfReplicas: 1` on the bound volume |
| Backup target reachable | pass — `available: true` |
| Backup **object** listed in B2 | pass — 10 `.blk` data blocks, listed from daniel-server |

The rebuild run was `ok=36 changed=12 failed=0 skipped=2`, and both skips are the intended
ones: the k3s install task found its arguments already in the systemd unit, and the
StorageClass had no replica count to strip.

### The replica failure

`smoke-pvc` bound at **3** replicas against a criterion of 1. The role's
`default-replica-count` patch was not at fault — it applied, and the setting read back `1`. The
`longhorn` StorageClass carried its own `numberOfReplicas: "3"`, hardcoded in upstream's
`deploy/longhorn.yaml`, and a StorageClass parameter overrides the global setting.

Fixed in `roles/setup/k3s`: it now applies `files/longhorn-storageclass.yaml` (upstream's class
with `numberOfReplicas` omitted, so the setting governs) and deletes/recreates the class when it
finds one still pinning a count — parameters are immutable. Guarded by
`ansible/tests/test_longhorn_storageclass.py`.

**Applied and proven** on the rebuilt cluster: a fresh PVC binds at `numberOfReplicas: 1`.

Do not `--check` this playbook if you re-run it: nearly every task is
`ansible.builtin.command`, which check mode skips rather than simulates, so a dry run reports
green having proved nothing.

### 5a. k3s bound its node IP to a NIC that no longer exists

Found while re-proving the replica fix, and it had already broken the cluster. The re-created
PVC never left `Pending`, because Longhorn could not talk to the API:

```
dial tcp 10.43.0.1:443: connect: no route to host
failed to validate nodeIP: node IP: "10.0.0.153" not found in the host's network interfaces
```

daniel-box was multi-homed when k3s was installed — a USB ethernet adapter on 10.0.0.153
alongside `eno1` on 10.0.0.215, both with equal-metric default routes — and k3s autodetected
the USB one, registering the node's InternalIP as **10.0.0.153**. That adapter has since
disappeared: `ip -br addr` now shows only `eno1` up. kube-proxy's DNAT is intact (101 nat
`KUBE-*` rules, the `10.43.0.1:443` rule present), so the ClusterIP still resolves — to an
apiserver endpoint on an address no interface owns. Every pod lost the API.

It stayed invisible for hours because `k3s kubectl` from the host talks to `127.0.0.1:6443`
and kept working throughout. The Docker purge is **not** implicated: the three surviving
`DOCKER*` entries are empty chain declarations (`-N DOCKER`, `-N DOCKER-BRIDGE`, `-N
DOCKER-CT`) with no rules, and `FORWARD` is `ACCEPT`.

Fixed in the role and applied: `k3s_server_args` pins `--node-ip` and
`--advertise-address` to `server_ip`. Two things had to change for that to be deliverable at
all — the install task's `creates: /usr/local/bin/k3s` guard meant any `k3s_server_args` edit
was silently skipped on an installed host, so it now compares the desired arguments against
the systemd unit; and `k3s_version` is pinned, because a re-runnable installer would otherwise
upgrade the control plane as a side effect of a flag change. The role also asserts the
registered InternalIP equals `server_ip`. Guarded by
`ansible/tests/test_k3s_node_ip_pinned.py`.

**The cluster had to be rebuilt to take it, and that is the trap worth remembering.** Moving
the address is not a restart — etcd stores the member's peer URL and k3s validates its own
membership against it:

```
this server is not a member of the etcd cluster.
Found [daniel-box-2eb310c7=https://10.0.0.153:2380], expect: ...=https://10.0.0.215:2380
```

k3s then retries forever without signalling ready, and the unit is `Type=notify` with
`TimeoutStartSec=0`, so `systemctl restart k3s` never returns and the play hangs with no
error. Remedies are `k3s server --cluster-reset`, which rewrites the member list from the
existing data, or a wipe. A wipe was taken here because the cluster held nothing: uninstall,
`rm -rf /var/lib/longhorn` (which `k3s-uninstall.sh` leaves behind), re-run `k3s-bringup.yml`.
The trap is recorded next to the flag in `roles/setup/k3s/defaults/main.yml`.

**Open question for whoever picks this up:** whether the USB adapter is meant to be plugged in
at all. It does not change the fix — a control-plane node must not bind its identity to a
removable NIC either way — but something unplugged it around 21:21 on 2026-08-01 and that is
worth understanding separately.

### How the last two were proven

**LoadBalancer across the LAN.** `curl -sS --max-time 10 http://10.0.0.240/` run from
**daniel-server** returned the whoami pod's response, so `10.0.0.240` was resolved by ARP and
the packet crossed the wire to daniel-box's `eno1`. See §6 for why the `RemoteAddr` in that
response is *not* the thing that proves it.

**An object actually written to B2.** `available: true` proves credentials and reachability,
not that anything was stored, and "backup reports success while storing nothing" is the
specific failure slice 0 exists to prevent. It takes two steps, because the role only
configures the backup *target*:

1. On daniel-box: mount `smoke-pvc` in a pod so the volume attaches (a snapshot cannot be
   taken while detached), write a marker, create a `Snapshot` CR, then a `Backup` CR. The
   `Backup` **must** carry the label `backup-volume: <volume>` — longhorn-manager's backup
   controller resolves the volume through it and fails with `cannot find the backup volume
   label` otherwise.
2. From daniel-server, list the bucket with an S3 client. `b2`, `aws`, `rclone`, `restic` and
   `kopia` are all absent from daniel-box; daniel-server is the Docker host, so
   `docker run --rm amazon/aws-cli s3 ls … --endpoint-url https://s3.<region>.backblazeb2.com`
   needs nothing installed. B2 speaks the S3 API — the endpoint URL is the only thing that
   makes it Backblaze rather than AWS.

Result on 2026-08-01: 12 objects under `longhorn/`, of which **10 were `.blk` data blocks**
alongside one `backup_*.cfg` and one `volume.cfg`. The blocks are the point — a `.cfg` on its
own is metadata describing a backup that stored nothing.

Clean up the smoke resources when done:
`sudo k3s kubectl delete deploy/smoke svc/smoke pvc/smoke-pvc`. Deleting the PVC does **not**
remove the backup from B2; drop the `Backup` CR too if you do not want it lingering.

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
- **`RemoteAddr` from a whoami pod cannot tell you where a request came from.** With a Service
  on the default `externalTrafficPolicy: Cluster`, kube-proxy masquerades external traffic
  before it reaches the pod, so the source reads as cni0's `10.42.0.1` whether the client was
  on the LAN or on the node itself. To test MetalLB's ARP, run the curl from a machine you know
  is not the node; to see the real client IP, patch the Service to
  `externalTrafficPolicy: Local` first.
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
- **Longhorn and Kopia share one B2 bucket**, isolated only by Longhorn's `longhorn/` prefix
  against Kopia's snapshots at the root. That is the role's intent, and it keeps a lifecycle
  rule written for one from expiring the other's data *only for as long as every such rule is
  prefix-scoped*. Nothing enforces that today. Worth settling before slice 1 puts a real
  service behind it.
- **Longhorn stays at 1 replica** until daniel-server joins at slice 7. Failover does not exist
  before then — resilience arrives last in this ordering, by design.

---

## 8. Landed this session

Server repo: #46 (k3s slice 0 + `platform` key), #47 (Docker keyring umask), #48 (chezmoi +
claude_code roles), #49 (per-user home resolution), #50 (github_cli role), #52 (chezmoi config
seeding), #53 (deferred distro packages). #51 was a duplicate, closed.

Dotfiles repo: #157 (`.chezmoiignore` for daniel-box + deb822 `gh_ready`), merged and applied on
the workstation. **Not yet applied on daniel-box** — step 3 above covers it.
