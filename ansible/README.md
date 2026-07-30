# Host Bring-Up Runbook

One-time, low-level steps to get a **new physical host** ready for this repo's Ansible.
Once the host is reachable over SSH with the repo cloned, day-to-day deploys, tooling (uv),
secrets (SOPS), and "adding a service" are documented in the repo-root
[`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md) — this file only covers the
OS/hardware bring-up that those don't.

## 1. Reach the host over SSH

1. Generate an SSH key (e.g. <https://phoenixnap.com/kb/generate-ssh-key-windows-10>) and set
   a password on the new machine.

   > **That password must match the SOPS `become_password`** — or the deploy user must have
   > NOPASSWD sudo (how `daniel-pi` does it, see `inventory/hosts.ini`). `pre_tasks/load_secrets.yml`
   > sets `ansible_become_password` from that one fleet-wide secret before any role runs, and
   > there is no per-host override, so a host whose password differs fails every escalated
   > task in §8.
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
3. Copy your public key to the host:
   `type C:\Users\<username>\.ssh\id_rsa.pub | ssh username@remote_host "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"`
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
systemd units — gitops-deploy's `REPO_DIR`, the secret-rotation crons, AIDE's local config,
and every container role's `containers/` bind mount. A repo cloned elsewhere, or owned by a
differently-named user, breaks those with no clear error.

`--recurse-submodules` picks up `Email-to-RSS` (a pinned submodule — see the bottom of this
file); a plain clone leaves the directory empty.

Secrets are committed **encrypted** (SOPS/age), so the clone already contains
`ansible/vars/secrets.yml` — there is no separate secrets-copy step. Letting this host
*decrypt* them is **§5** below (and must happen before §8's `initial_setup.yml`).

## 3. Install uv

> **Shortcut:** [`bring-up.sh`](bring-up.sh) wraps §3+§5 — `./ansible/bring-up.sh` installs
> uv, runs `bootstrap.yml`, and prints the §5 manual steps. It assumes §4 (inventory) is
> already done. The walkthrough below is what it does (and the path to take if you'd rather
> run each step by hand).

`uv` is the **only manual prerequisite** — everything else flows from it. The repo is a uv
"virtual" project (`pyproject.toml` pins `ansible-core` in the `dev` group; `.python-version`
pins 3.14), so `uv run ansible-playbook …` self-provisions Python + ansible-core + the runtime
from `uv.lock`. That includes `bootstrap.yml` in §5, so no system-wide Ansible install is
needed.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc   # (or re-login) to pick up ~/.local/bin on PATH
```

## 4. Add the host to inventory

Every playbook (`bootstrap.yml`, `initial_setup.yml`, `deploy.yml`) resolves its target through
`ansible/inventory/hosts.ini` — a host that isn't listed there matches nothing, and the run
just silently no-ops. Before touching SOPS:

1. Add the new host under `[homeservers]` in `ansible/inventory/hosts.ini`. **The connection
   fields are not optional** — a bare hostname makes Ansible open an SSH connection to the
   host even when it *is* the host. Use whichever line applies:

   ```ini
   # Playbooks are run ON this host (the usual case for a new server):
   <host>   ansible_connection=local
   # Driven remotely from another host acting as controller (how daniel-pi is set up):
   <host>   ansible_host=<lan-ip> ansible_user=ubuntu ansible_connection=ssh
   ```

2. Create `ansible/inventory/host_vars/<host>.yml` — copy
   [`host_vars/_example.yml`](inventory/host_vars/_example.yml), which lists every variable a
   new host can set with its default and why it matters. At minimum:
   - `server_ip` — the host's LAN IP (no default exists; every role that publishes a
     LAN-bound port or firewalls by IP needs it).
   - `ssh_config_path` — where sshd's config actually lives on this OS image. Check with
     `sudo sshd -T | grep -i '^include'` or `ls /etc/ssh/sshd_config.d/` — cloud-init images
     (like the Pi's) often drop a config under `sshd_config.d/`; a stock Ubuntu install uses
     the bare `/etc/ssh/sshd_config`. Getting this wrong fails the SSH-hardening tasks in §8
     outright (the `lineinfile`/`blockinfile` tasks have no `create:` fallback).
   - `containers_list: []` to start — populate it once you decide what this host runs (see
     the repo-root `CLAUDE.md` → "Adding a New Container Service").
   - Any hardware capability flags a role you plan to deploy expects — e.g. `has_igpu: true`
     only if the host has an Intel iGPU (gates `/dev/dri` passthrough for jellyfin/tdarr;
     defaults to `false` in `group_vars/all.yml`), or `scrutiny_nvme_device` if you deploy
     scrutiny and its NVMe enumerates as something other than `/dev/nvme0`.
   - `has_gitops: false` **until the host's first successful manual deploy**. It defaults to
     `true`, and the `gitops_deploy` role's "Run gitops-deploy once" handler shells a full
     `uv run ansible-playbook deploy.yml` at the end of §8's `initial_setup.yml` — failing the
     play if that deploy fails. On a brand-new host that fires before you have ever deployed
     by hand, so a single bad service takes down the whole OS-hardening run. Flip it to `true`
     and re-run `--tags gitops_deploy` once §8's `deploy.yml` succeeds.

## 5. Onboard the host to SOPS

This **gates §8**: `initial_setup.yml` (and `deploy.yml`) decrypt `ansible/vars/secrets.yml` in
a `pre_tasks` block that runs *before any role*, so on a fresh host they fail before
`sops_setup` could install SOPS. `bootstrap.yml` breaks that chicken-and-egg — it runs
`sops_setup` on its own (no secret dependency): installs the `age`/`sops` binaries and the
pinned collections (incl. `community.sops`), generates this host's age key, and prints its
public key.

```bash
uv run ansible-playbook ansible/bootstrap.yml --limit <host>   # prints "Your Public Key is: age1…"
```

> Bare `ansible-playbook` also works here (bootstrap uses only builtin modules; any ansible-core
> will do) — but `uv run` keeps a single path now that §3 installed uv.

> **Run this ON the new host**, not remotely from another one. `bootstrap.yml`'s `hosts:`
> pattern falls back to `lookup('pipe', 'hostname')` — the *controller's* hostname — when
> `-e target=<host>` isn't passed. Running it from daniel-server against a fresh host with
> only `--limit <host>` (no `-e target=`) resolves the play to daniel-server itself; `--limit`
> then intersects to nothing and it silently does nothing. SSH into the new host first (§1–3
> already have you there), then run the command locally.

Then:

1. Add the printed `age1…` public key to `ansible/.sops.yaml` (tracked) under `age:`.
2. On a host that can already decrypt (daniel-server): `sops updatekeys ansible/vars/secrets.yml`,
   then commit + push the re-encrypted `secrets.yml` + `.sops.yaml`.
3. Back on the new host: `git pull`.

**First host ever** (no other host can decrypt yet): `sops_setup` seeds `ansible/.sops.yaml`
from this host's own key, so steps 2–3 don't apply. Multi-recipient is OR — any listed key
decrypts the whole file. See the `ansible/bootstrap.yml` header for the full flow.

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
`has_igpu: true` in its `host_vars` (§4) — that one flag now drives both halves: the
jellyfin/tdarr compose templates pass through `/dev/dri`, and `initial_setup.yml` writes
`/etc/modprobe.d/i915.conf` and rebuilds the initramfs.

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags igpu
```

It deliberately does **not** reboot for you — this is the machine running the whole homelab.
GuC only takes effect after a reboot, so do that when convenient, then confirm with
`ls /dev/dri` and `dmesg | grep -i guc`.

> This used to be a hand-run `echo … | sudo tee -a`, which appends a **duplicate** option
> line every time it's run — daniel-server's `i915.conf` still carries the line twice from
> exactly that. The Ansible task is idempotent and converges it back to one.

If `/dev/dri/` is missing entirely the kernel has no i915 driver for this GPU, which needs
an OEM kernel matching **this host's Ubuntu release** plus a reboot. Don't copy
`linux-oem-22.04` from an older runbook onto a 24.04 host: there it's a transitional dummy
package (verified on daniel-server, where the generic 6.8 kernel already provides i915).

## 8. Run the playbooks

Ansible runs through the repo's pinned uv env (see repo-root [`CLAUDE.md`](../CLAUDE.md) →
"Common Commands"). From the repo root:

```bash
uv run ansible-playbook ansible/preflight.yml       # read-only; asserts §1-§5 actually landed
uv run ansible-playbook ansible/initial_setup.yml   # OS hardening; base pkgs, Docker, uv-tool CLIs, gitops deployer — needs §5 SOPS
uv run ansible-playbook ansible/deploy.yml          # deploy all containers (dependency-ordered)
```

`preflight.yml` changes nothing — it just fails fast, with an error that names the cause, on
the mistakes that otherwise surface deep inside `initial_setup.yml`: a missing `host_vars`
entry, an `ssh_config_path` that doesn't exist on this OS image, a sudo password that doesn't
match the SOPS `become_password` (§1), or a repo cloned somewhere other than
`/home/<user>/server` (§2). Reaching its asserts at all proves §5's SOPS onboarding worked.

**CrowdSec's Traefik bouncer needs no manual registration.** The traefik role registers it
from the existing SOPS `crowdsec_bouncer_api_key` on every deploy, and its probe/delete/re-add
sequence is rotation-safe (`roles/containers/traefik/tasks/main.yml`). Verify with
`docker exec crowdsec cscli bouncers list` — the name is `traefik-bouncer`. Rotation is
`docs/secret-rotation.md`, not a hand-run `cscli bouncers add`.

> **Adding a new service**, **secrets**, and **deploy flow** are documented once in the
> repo-root [`CLAUDE.md`](../CLAUDE.md) and [`README.md`](../README.md) and the
> `new-container` skill — not duplicated here. **Backups** are handled by the Kopia role
> (snapshots the bind-mounted `containers/` data), not the legacy Duplicati setup.

Router port-forwarding, Cloudflare DNS and the other off-box prerequisites are in §9.

## 9. Post-deploy setup that Ansible can't do

`deploy.yml` finishing green does **not** mean the host is done. The steps below live in each
app's own database, which Ansible never writes. Most of them **fail silently** — the container
stays healthy while the feature behind it does nothing (the exception is Authelia, whose role
asserts up front and fails the deploy). Work top-down; the first two gate the whole monitoring
fleet.

1. **Create the Uptime-Kuma admin** at `https://uptime-kuma.<domain>` (first-run wizard). AutoKuma
   **cannot** create it, and until it exists AutoKuma provisions **zero** monitors — so nothing
   in the fleet is watched and no alert can fire. Kuma's own DB is deliberately excluded from
   Kopia backups, so a rebuilt host always needs this again.
2. **Re-mint `prometheus_kuma_api_key`** in Kuma (Settings → API Keys) and `sops set` it. Kuma
   issues keys into that same unbacked-up DB, so the value in `secrets.yml` is stale on any
   fresh Kuma and the `uptime-kuma` scrape target sits at 401/DOWN.
3. **Seed the *arr API keys.** `sonarr_api_key`, `radarr_api_key`, `prowlarr_api_key` and
   `jellyfin_api_key` in SOPS are what configarr, janitorr, homepage, monitor-bridge and
   autofix-bridge authenticate with — but a fresh *arr generates its own random key on first
   start. Either paste the SOPS value into each app's Settings → General, or stop the
   container and write it into `config.xml`'s `<ApiKey>`. Skip this and every consumer 401s
   against a service that reports healthy.
4. **Home Assistant onboarding**, then mint four long-lived tokens (Profile → Security) for
   `monitor_bridge_ha_token`, `homepage_ha_token`, `prometheus_ha_token`, `claude_ha_token`.
   The rest of HA's one-time setup — HACS, Zigbee pairing, the `light.bedroom_lights` group,
   companion-app sensors — is in [`roles/containers/home-assistant/SETUP.md`](roles/containers/home-assistant/SETUP.md).
5. **Authelia**, on a genuinely fresh install: generate the OIDC HMAC secret, client password
   hash and RSA key per [`roles/containers/authelia/CLAUDE.md`](roles/containers/authelia/CLAUDE.md)
   ("Fresh install") — the role asserts they exist. Note `users_database.yml` is written
   **first-run-only**, so `authelia_user`/`authelia_password` must be right before the first
   deploy; later changes never reach the file.
6. **Register a second host in Portainer** (Environments → Add), per
   [`roles/containers/portainer-agent/CLAUDE.md`](roles/containers/portainer-agent/CLAUDE.md) —
   Portainer keeps environments in its own BoltDB.

**Why these can't move into Ansible** (checked 2026-07-30, so it doesn't get re-litigated):
Uptime-Kuma 2.x exposes setup and API-key minting over **Socket.IO only** — there is no REST
route to drive, so items 1-2 are structurally manual. Home Assistant's `/api/onboarding/users`
is drivable, but minting a long-lived token isn't, and seeding `.storage/` by hand corrupts
installs. The *arr keys are deliberately left manual: writing `config.xml` under a running
container is the kind of stateful surgery that silently breaks an app.
**Grafana is no longer on this list** — the role now runs `grafana cli admin
reset-admin-password` whenever the SOPS password file changes, so a rotation reaches the live
admin user on the next deploy with no UI step.

External prerequisites, none of them IaC-managed: the Cloudflare DNS records (including the
hand-created grey-cloud `*.local.<domain>` wildcard that all internal routing depends on — see
[`roles/containers/cloudflare-ddns/CLAUDE.md`](roles/containers/cloudflare-ddns/CLAUDE.md)),
router port-forwards for Traefik and WireGuard, a Backblaze B2 bucket for Kopia, and the
off-box UptimeRobot dead-man's-switch. Rebuilding rather than bringing up a new host? Follow
[`docs/kopia-disaster-recovery.md`](../docs/kopia-disaster-recovery.md) instead — it covers
restore ordering this section doesn't.

## Misc host notes

### Trim journald log level

```bash
sudo nano /etc/systemd/journald.conf   # uncomment + set MaxLevelStore=notice, MaxLevelSyslog=notice
sudo systemctl restart systemd-journald
```

### LaTeX editor (code-server devcontainer)

1. Clone the Resume repository on the server.
2. Copy `.devcontainer` from <https://github.com/James-Yu/LaTeX-Workshop/tree/master/samples/docker>.
3. Install the VS Code Remote - Containers + SSH extensions, then reopen the directory in the container.

## Email-to-RSS (Cloudflare Worker)

Converts email newsletters to RSS feeds. Runs as a Cloudflare Worker (not a Docker container).
Tracked as a **git submodule** at `~/server/Email-to-RSS` (upstream
<https://github.com/yl8976/Email-to-RSS>, pinned to a known-good commit in `.gitmodules`).
Admin UI at <https://email-rss.daniel-hunter.com/admin>.

`wrangler.toml` (KV namespace ids, routes) is ignored by the submodule's own `.gitignore`
and stays local-only — recreate it from `wrangler-example.toml` + step 5 below on a fresh
machine. To pull upstream changes: `cd Email-to-RSS && git pull`, redeploy, then commit the
new submodule pointer here. If local patches are ever needed, fork upstream and re-point
the submodule URL at the fork.

**Prerequisites:** Node.js 20+, Cloudflare account, ForwardEmail account, domain managed in Cloudflare DNS.

**Initial setup (already done — for reference):**

1. Fetch the code: `git submodule update --init Email-to-RSS` (originally a plain clone of the repo above)
2. Run `npm install` in the repo directory.
3. Authenticate with Cloudflare: `npx wrangler login`
4. Create KV namespaces manually (setup.sh has a bug with namespace title matching):
   `npx wrangler kv namespace create EMAIL_STORAGE`
   `npx wrangler kv namespace create EMAIL_STORAGE --preview`
5. Copy wrangler-example.toml to wrangler.toml and fill in:
   - compatibility_date: today's date (YYYY-MM-DD)
   - KV namespace IDs from step 4
   - DOMAIN: daniel-hunter.com
   - routes: email-rss.daniel-hunter.com (subdomain required — root domain has existing A records)
6. Set admin password: `npx wrangler secret put ADMIN_PASSWORD --env production` (confirm worker creation when prompted)
7. Deploy: `npm run deploy`

**DNS records required in Cloudflare (daniel-hunter.com):**

- MX  @  mx1.forwardemail.net  (priority 10)  — email reception via ForwardEmail
- MX  @  mx2.forwardemail.net  (priority 10)
- TXT @  v=spf1 include:spf.forwardemail.net -all
- TXT @  `forward-email=https://email-rss.daniel-hunter.com/api/inbound`  — webhook to Worker

**Known limitation:** The DOMAIN variable controls both email addresses and RSS feed URLs. Since the
Worker is deployed on a subdomain (email-rss.daniel-hunter.com) but email must be received at the root
domain (daniel-hunter.com), these can't be the same value. DOMAIN is set to daniel-hunter.com so email
addresses are correct. When copying RSS feed URLs from the admin UI, manually replace daniel-hunter.com
with email-rss.daniel-hunter.com (e.g. `https://email-rss.daniel-hunter.com/rss/{feedId}`).

**Redeploying after changes:**

1. `cd ~/server/Email-to-RSS`
2. `npm run deploy`
