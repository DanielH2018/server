# Host Bring-Up Runbook

One-time, low-level steps to get a **new physical host** ready for this repo.
`hosts/bootstrap-host.sh` brings the host up, and a host's setup plane ships in its host
package. No playbook in `ansible/` applies a setup role. Day-to-day deploys, tooling (uv),
secrets (SOPS), and "adding a service" are documented in the repo-root
[`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md). This file covers the
OS/hardware steps around the script that those don't.

## 1. Reach the host over SSH

1. Generate an SSH key (e.g. <https://phoenixnap.com/kb/generate-ssh-key-windows-10>) and set
   a password on the new machine.

2. **WiFi (if no ethernet):** edit the file under `/etc/netplan/` (`ls` to find it):

   ```yaml
   wifis:
       wlan0:
           dhcp4: true
           optional: true
           access-points:
               "Wifi SSID":
                   password: your-wifi-password
   ```

   then `sudo netplan apply`, and find the host's IP with `ip a`.
3. Copy your public key to the host: `ssh-copy-id <user>@<host>`. It ships with OpenSSH on
   Linux/macOS and in Git Bash / WSL on Windows, and gets the `~/.ssh` permissions right on
   its own. Only if you're stuck in PowerShell with no `ssh-copy-id`:

   ```powershell
   type C:\Users\<username>\.ssh\id_rsa.pub | ssh <user>@<host> "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```
4. SSH in with the key.

## 2. Clone the repo

```bash
git config --global user.name  "your_username"
git config --global user.email "your_email@example.com"
git config --global credential.helper store
git clone --recurse-submodules https://github.com/DanielH2018/server.git   # use a GitHub PAT as the password
```

Clone it to **`/home/<user>/server`, as the user named by `sys_user`** (`ubuntu`, in
`group_vars/all.yml`). Both the path and the username are baked into role templates and
systemd units, such as the secret-rotation crons and AIDE's local config. A repo cloned elsewhere, or owned by a
differently-named user, breaks those with no clear error.

`--recurse-submodules` picks up `Email-to-RSS` (a pinned submodule — see
[`docs/email-to-rss.md`](../docs/email-to-rss.md)); a plain clone leaves the directory empty.

Secrets are committed **encrypted** (SOPS/age), so the clone already contains
`ansible/vars/secrets.yml` — there is no separate secrets-copy step. Letting this host
*decrypt* them is **§5** below.

## 3. Install uv

`uv` runs the repo's Python tooling. The repo is a uv "virtual" project (`.python-version`
pins 3.14), so `uv run` self-provisions Python and the runtime from `uv.lock`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # (or re-login) to pick up ~/.local/bin on PATH
```

## 4. Add the host to inventory

`ansible/inventory/hosts.ini` and `ansible/inventory/host_vars/<host>.yml` describe each host to
the renderer and the host packages. A host that isn't listed there matches nothing. Before
touching SOPS:

1. Add the new host's bare hostname under `[homeservers]` in `ansible/inventory/hosts.ini`.

2. Create `ansible/inventory/host_vars/<host>.yml` — copy
   [`host_vars/_example.yml`](inventory/host_vars/_example.yml), which lists every variable a
   new host can set with its default and why it matters. At minimum:
   - `server_ip` — the host's LAN IP (no default exists; every role that publishes a
     LAN-bound port or firewalls by IP needs it).
   - `containers_list: []` on a Docker host only. A k3s node drops the key, because the k3s
     services live in `cluster/services.yml` (see the repo-root `CLAUDE.md` → "Adding a New
     Service").
   - Any hardware capability flags a role you plan to deploy expects — e.g. `has_igpu: true`
     only if the host has an Intel iGPU (gates `/dev/dri` passthrough for jellyfin/tdarr;
     defaults to `false` in `group_vars/all.yml`).

## 5. Onboard the host to SOPS

This step gates decryption: a host needs `sops` and an age key before it can read
`ansible/vars/secrets.yml`.

- `sops` comes from the host package. `hosts/bootstrap-host.sh` installs it, and the package's
  `sops` component puts `/usr/bin/sops` on `PATH` and depends on `age`.
- `hosts/bootstrap-host.sh` creates the host's age key at its step 3 and prints the public
  key. `docs/break-glass.md` (*The age keys*) records which hosts hold one.

Then:

1. Add the printed `age1…` public key to `ansible/.sops.yaml` (tracked) under `age:`.
2. On a host that can already decrypt (daniel-box): `sops updatekeys ansible/vars/secrets.yml`,
   then commit + push the re-encrypted `secrets.yml` + `.sops.yaml`.
3. Back on the new host: `git pull`.

Multi-recipient is OR — any listed key decrypts the whole file.

## 6. Storage (server, as needed)

Extend the root LV to fill the disk (the partition name is likely different):

```bash
sudo lvm
lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
exit
sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
```

## 7. Intel iGPU / QuickSync (Jellyfin / Tdarr transcode)

Only needed if this host will run jellyfin/tdarr with hardware transcode. Set
`has_igpu: true` in its `host_vars` (§4) — that flag passes `/dev/dri` through in the
jellyfin/tdarr compose templates, and on an **Intel** GPU the host package also writes
`/etc/modprobe.d/i915.conf` (`hosts/components/host-hardening/templates/i915.conf`).

The package deliberately does **not** reboot for you — this is the machine running the whole homelab.
GuC only takes effect after a reboot, so do that when convenient, then confirm with
`ls /dev/dri` and `dmesg | grep -i guc`.

> This used to be a hand-run `echo … | sudo tee -a`, which appends a **duplicate** option
> line every time it's run — daniel-server's `i915.conf` still carries the line twice from
> exactly that. The Ansible task is idempotent and converges it back to one.

`enable_guc` is an i915 option, so the task additionally checks that `/sys/module/i915` exists
and skips on a non-Intel GPU. `has_igpu` on its own would be the wrong gate — it means "pass
`/dev/dri` through", which AMD also uses for VAAPI.

If `/dev/dri/` is missing entirely the kernel has no i915 driver for this GPU, which needs
an OEM kernel matching **this host's Ubuntu release** plus a reboot. Don't copy
`linux-oem-22.04` from an older runbook onto a 24.04 host: there it's a transitional dummy
package (verified on daniel-server, where the generic 6.8 kernel already provides i915).

## 8. Install the host packages

`hosts/bootstrap-host.sh <hostname>` installs the host's packages. `docs/host-packages.md`
(*Rebuilding daniel-box*, *Rebuilding daniel-server*) orders the steps for a cluster node, and
the Pi runbook in `docs/break-glass.md` does the same for `daniel-pi`.

No playbook deploys a workload. Flux applies every k8s workload once it is bootstrapped; on a
rebuilt cluster, follow [Bootstrapping Flux on a rebuilt
cluster](../docs/flux-deploy-plane.md#bootstrapping-flux-on-a-rebuilt-cluster). The Pi's services
ship in its host package.

**CrowdSec's bouncers need no manual registration.** Since slice-6 B2 the single LAPI runs in
the k3s cluster (`roles/k8s/crowdsec`) and registers its one bouncer declaratively from the
`BOUNCER_KEY_*` env — `k8straefik`, the cluster edge's (the Docker edge and its
`dockertraefik` key retired at E7, 2026-08-13). Verify with
`kubectl -n homelab exec deploy/crowdsec -c crowdsec -- cscli bouncers list` on daniel-box (the
Docker container is an agent now and has no LAPI to ask). Rotation is `docs/secret-rotation.md`,
not a hand-run `cscli bouncers add`.

> **Adding a new service**, **secrets**, and **deploy flow** are documented once in the
> repo-root [`CLAUDE.md`](../CLAUDE.md) and [`README.md`](../README.md) and the
> `new-container` skill — not duplicated here. **Backups** are Longhorn's since the
> 2026-08-10 consolidation (nightly PVC backups to B2 —
> `docs/archive/k3s-migration/backup-consolidation-longhorn.md`); kopia is retired and Docker-tier
> bind mounts are no longer backed up (the remaining Docker services hold regenerable or
> migrating state only).

Router port-forwarding, Cloudflare DNS and the other off-box prerequisites are in §9.

## 9. Post-deploy setup that Ansible can't do

Flux applying the host's workloads green does **not** mean the host is done. The steps below live in each
app's own database, which Ansible never writes. Most of them **fail silently** — the container
stays healthy while the feature behind it does nothing (the exception is Authelia, whose role
asserts up front and fails the deploy). Work top-down.

```bash
uv run python scripts/diagnostics/postflight.py   # exercises every item below against the live host
```

It authenticates with each SOPS credential exactly as the real consumers do, prints one line
per item, and exits non-zero naming what's still pending. A service this host doesn't run
reports SKIP. Run it once Flux has applied the host's workloads, and again after working through the list.

1. **Seed the *arr API keys.** `sonarr_api_key`, `radarr_api_key`, `prowlarr_api_key` and
   `jellyfin_api_key` in SOPS are what configarr, janitorr, homepage, monitor-bridge and
   autofix-bridge authenticate with — but a fresh *arr generates its own random key on first
   start. Either paste the SOPS value into each app's Settings → General, or stop the
   container and write it into `config.xml`'s `<ApiKey>`. Skip this and every consumer 401s
   against a service that reports healthy.
2. **Home Assistant onboarding**, then mint four long-lived tokens (Profile → Security) for
   `monitor_bridge_ha_token`, `homepage_ha_token`, `prometheus_ha_token`, `claude_ha_token`.
   The rest of HA's one-time setup — HACS, Zigbee pairing, the `light.bedroom_lights` group,
   companion-app sensors — is in [`roles/k8s/home-assistant/SETUP.md`](roles/k8s/home-assistant/SETUP.md).
3. **Authelia**, on a genuinely fresh install: generate the OIDC HMAC secret, client password
   hash and RSA key per the setup instructions in [`roles/k8s/authelia/templates/config-secret.yaml.j2`](roles/k8s/authelia/templates/config-secret.yaml.j2)
   (header comment explains fresh-install secrets) — the role asserts they exist. Note that
   OIDC configuration is live as of E7 (2026-08-13).
**Why these can't move into Ansible** (checked 2026-07-30, so it doesn't get re-litigated):
Home Assistant's `/api/onboarding/users`
is drivable, but minting a long-lived token isn't, and seeding `.storage/` by hand corrupts
installs. The *arr keys are deliberately left manual: writing `config.xml` under a running
container is the kind of stateful surgery that silently breaks an app.
**Grafana is no longer on this list.** An initContainer on the grafana pod runs `grafana cli
admin reset-admin-password` from the SOPS password before every start, and a password change
restarts the pod, so a rotation reaches the live admin user on the next deploy with no UI step.

External prerequisites, none of them IaC-managed: the Cloudflare DNS records (including the
hand-created grey-cloud `*.local.<domain>` wildcard that all internal routing depends on — see
[`roles/k8s/cloudflare-ddns/CLAUDE.md`](roles/k8s/cloudflare-ddns/CLAUDE.md)),
router port-forwards for Traefik and WireGuard, a Backblaze B2 bucket for the Longhorn
backup plane (kopia retired 2026-08-13), and the off-box UptimeRobot dead-man's-switch.
Rebuilding rather than bringing up a new host? Follow
[`docs/longhorn-disaster-recovery.md`](../docs/longhorn-disaster-recovery.md), the current
runbook. `docs/archive/kopia-disaster-recovery.md` describes the RETIRED plane and survives only as
history — do not follow it.

## Not bring-up, moved out of this file

- **journald** log level and disk cap ship in the host package (the drop-in at
  `/etc/systemd/journald.conf.d/50-homelab.conf`) — nothing to do by hand.
- **LaTeX editing in code-server:**
  [`roles/k8s/code-server/CLAUDE.md`](roles/k8s/code-server/CLAUDE.md).
- **Email-to-RSS** (the Cloudflare Worker submodule):
  [`docs/email-to-rss.md`](../docs/email-to-rss.md).
