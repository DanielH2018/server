# daniel-box handoff — 2026-08-01

Written to hand off to a Claude session running **on the server**. Everything below was
verified on the hosts unless explicitly marked otherwise.

Two independent workstreams ran today: the **k3s migration** (slice 0) and the **developer
environment** (chezmoi + Claude Code). They share a host but nothing else.

---

## 1. Where daniel-box actually is

**Hardware / role.** 10.0.0.215, Ubuntu 24.04, Ryzen 7 8845HS (8c/16t), 914 G root FS with
~864 G free. Now the k3s control plane.

**Verified working:**

| Thing | State |
|---|---|
| k3s | Single-node cluster, embedded etcd (`--cluster-init`), Traefik + servicelb disabled |
| MetalLB | Installed, pool `10.0.0.240-250` |
| Longhorn | Installed, `default-replica-count: 1`, backup target → B2 (`daniel-server-kopia`, prefix `longhorn/`) |
| Docker | **Not installed, deliberately** — k3s brings its own containerd |
| `gh` | Installed + authenticated as DanielH2018 |
| chezmoi | Installed at `~/.local/bin/chezmoi`, dotfiles cloned and applied |
| CLI tools | node, eza, fastfetch, curlie, sd, starship, fzf, zoxide, rg, gron, nvim, yazi |
| Claude Code | **Not yet installed** — the play never reached that role |

**`containers_list` is `[]`.** Nothing Docker-shaped runs here and nothing should; the
GitOps timer is a no-op while that list is empty. Anything landing here is `platform: k8s`.

---

## 2. Next steps — all need a TTY

Every remaining step needs a real terminal, because `sudo` must be able to prompt and
`claude login` is interactive. Run them **on daniel-box**:

```bash
ssh -t daniel-box
cd ~/server && git pull

# 1. Finish the dev environment. Should now clear chezmoi_setup (packages installed by
#    Ansible, gh_ready fixed) and reach claude_code.
~/.local/bin/uv run ansible-playbook ansible/initial_setup.yml --tags chezmoi,claude_code

# 2. Let install-cli-tools.sh add the WezTerm repo itself — this is the one step that
#    needs sudo to prompt. After it succeeds the script finally records success and stops
#    retrying on every apply.
chezmoi apply

# 3. Claude Code auth. Deliberately not automated: ~/.claude/.credentials.json is a
#    refreshing per-machine OAuth token that cannot be templated from SOPS.
claude login

# 4. The .chezmoiignore fix stops chezmoi MANAGING this file but does not delete the copy
#    already deployed. Remove it once.
rm ~/.ssh/config
```

### Then verify

```bash
claude --version                 # expect 2.1.x
chezmoi status                   # expect empty
gh auth status                   # expect logged in as DanielH2018
```

---

## 3. Outstanding work

### 3a. k3s slice 0 — three exit criteria unproven

The bring-up playbook asserts node readiness, the etcd datastore, and the absence of the
bundled Traefik/servicelb. It deliberately does **not** assert these three, and they are
the ones that matter:

1. **A LoadBalancer Service gets a pool IP and answers over L2 from another machine.**
   Allocation is not the hard part; ARP reaching the LAN is.
2. **A Longhorn PVC reaches `Bound`.**
3. **An object is genuinely written to B2.** `backuptarget.status.available: true` proves
   credentials and reachability, *not* that anything was stored.

Criterion 3 is the one not to skip — "a backup system that reports success while storing
nothing" is the exact failure slice 0 exists to prevent, and it is a hard precondition for
slice 1. See `docs/k3s-migration/slice-0-cluster-foundation.md` §Task 7.

```bash
sudo k3s kubectl create deploy smoke --image=traefik/whoami
sudo k3s kubectl expose deploy smoke --port=80 --type=LoadBalancer
sudo k3s kubectl get svc smoke                      # EXTERNAL-IP must not be <pending>
# then, FROM ANOTHER MACHINE:  curl -s --max-time 5 http://<EXTERNAL-IP>/
sudo k3s kubectl delete deploy/smoke svc/smoke
```

Record the outcomes under a "Slice 0 results" heading in the slice-0 plan — the next
slice's author needs to know what was observed, not what was intended.

### 3b. MCP wiring — agreed scope, deferred

Was in scope for the dev-environment work and deliberately left out rather than guessed.
MCP servers live in `~/.claude.json`, which is machine-local and **not** chezmoi-managed,
and daniel-server's working config could not be read (it holds tokens). `homelab_mcp_token`
is already in SOPS, so the credential half is ready — what is missing is the confirmed
endpoint and header format for `homelab-mcp`.

Easiest path now: on daniel-server, inspect how its `homelab` MCP server is registered,
then reproduce it on daniel-box with `claude mcp add`.

### 3c. Telemetry — out of scope by decision

daniel-server's `otel-collector` binds OTLP to its own loopback, and daniel-box has no
Docker to run one. Revisit when the k3s monitoring cluster lands (slice 3).

---

## 4. Traps found the hard way

Each of these cost a round-trip. They are recorded so they are not rediscovered.

**`ansible_env.HOME` is `/root`, even in a `become: false` task.** `ansible_env` reports the
environment of the user facts were *gathered* as, and `initial_setup.yml` gathers escalated
on purpose. Use `/home/{{ sys_user }}`. Guarded by
`ansible/tests/test_per_user_home_resolution.py`.

**`gpg --dearmor` via `command` inherits root's umask.** `initial_setup` sets `UMASK 027`
earlier in the same play, so the keyring landed 0640; apt fetches as the unprivileged `_apt`
user and reports the repo *unsigned*. This failed `initial_setup.yml` one task before Docker
would have installed, and was invisible on daniel-server because its keyring predates the
umask change. Fetch armored keys with `get_url` and an explicit `mode`. Guarded by
`ansible/tests/test_apt_keyring_permissions.py`.

**chezmoi's `--promptBool`/`--promptString` do not satisfy `promptBoolOnce`.** They were
passed and init prompted anyway (v2.71.1). `promptXxxOnce` only skips when the value is
already in the config *data*, so the config is seeded before init.

**`DanielH2018/dotfiles` is private; `DanielH2018/server` is public.** That asymmetry is why
`git pull` works on daniel-box while `chezmoi init` failed. Cloning needs
`credential.helper = !gh auth git-credential`.

**A failed `chezmoi init` leaves a complete clone but no rendered config.** Gating init on
"source directory absent" would then skip it forever and fall through to a bare `apply`
running without the `umask` pin and `[interpreters.sh]`. `init --apply` is therefore
unconditional.

**Privileged remote commands are blocked from agent sessions.** A guard refuses `sudo`
inside `ssh`. This is why every fix above was authored blind and validated one round-trip at
a time — a session running *on* the host does not have that constraint, which is the main
reason this handoff exists.

---

## 5. Security notes worth revisiting

**`gh auth login` stored a plaintext token.** No keyring on a headless host, so
`~/.config/gh/hosts.yml` holds a **full-scope GitHub user token in plaintext**. Anyone who
gets `ubuntu` on daniel-box gets the GitHub account. A read-only deploy key scoped to the
dotfiles repo would be tighter; it was rejected only to match daniel-server. Worth
revisiting if daniel-box's exposure changes.

**Resilience is asymmetric and currently absent.** Longhorn runs at 1 replica until
daniel-server joins the cluster in slice 7. Until then B2 is the only durability story, and
losing daniel-box loses the control plane outright.

---

## 6. Landed today

Server repo: **#46** (k3s slice 0 + `platform` key), **#47** (Docker keyring umask fix),
**#48** (chezmoi + claude_code roles), **#49** (per-user home resolution), **#50**
(`github_cli` role), **#52** (chezmoi config seeding), **#53** (deferred distro packages).
Dotfiles: **#157** (`.chezmoiignore` for daniel-box + `gh_ready` deb822 fix).

Design and plan documents live in `docs/k3s-migration/`.
