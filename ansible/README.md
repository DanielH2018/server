# Host Bring-Up Runbook

One-time, low-level steps to get a **new physical host** ready for this repo's Ansible.
Once the host is reachable over SSH with the repo cloned, day-to-day deploys, tooling (uv),
secrets (SOPS), and "adding a service" are documented in the repo-root
[`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md) — this file only covers the
OS/hardware bring-up that those don't.

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
3. Copy your public key to the host:
   `type C:\Users\<username>\.ssh\id_rsa.pub | ssh username@remote_host "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"`
4. SSH in with the key.

## 2. Clone the repo

```bash
git config --global user.name  "your_username"
git config --global user.email "your_email@example.com"
git config --global credential.helper store
git clone https://github.com/DanielH2018/server.git   # use a GitHub PAT as the password
```

Secrets are committed **encrypted** (SOPS/age), so the clone already contains
`ansible/vars/secrets.yml` — there is no separate secrets-copy step. To let this new host
*decrypt* them, follow **"Onboarding a host to SOPS"** in the repo-root
[`CLAUDE.md`](../CLAUDE.md): run `ansible/bootstrap.yml`, add the host's age public key to
`ansible/.sops.yaml`, then `sops updatekeys` on a host that can already decrypt.

## 3. Storage (server, as needed)

Extend the root LV to fill the disk (the partition name is likely different):

```bash
sudo lvm
lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
exit
sudo resize2fs /dev/ubuntu-vg/ubuntu-lv
```

## 4. Intel iGPU / QuickSync (Jellyfin / Tdarr transcode)

1. If `/dev/dri/` is missing: `sudo apt install linux-oem-22.04`, then reboot.
2. Enable GuC:

   ```bash
   sudo mkdir -p /etc/modprobe.d
   echo 'options i915 enable_guc=2' | sudo tee -a /etc/modprobe.d/i915.conf
   sudo update-initramfs -u && sudo update-grub && sudo reboot
   ```

## 5. Run the playbooks

Ansible runs through the repo's pinned uv env (see repo-root [`CLAUDE.md`](../CLAUDE.md) →
"Common Commands"). From the repo root:

```bash
uv run ansible-playbook ansible/initial_setup.yml   # OS hardening; installs uv/docker/tooling
uv run ansible-playbook ansible/deploy.yml          # deploy all containers (dependency-ordered)
```

After the first deploy, register the Traefik bouncer with CrowdSec and store the key in
`secrets.yml`: `docker exec crowdsec cscli bouncers add bouncer-traefik`.

> **Adding a new service**, **secrets**, and **deploy flow** are documented once in the
> repo-root [`CLAUDE.md`](../CLAUDE.md) and [`README.md`](../README.md) and the
> `new-container` skill — not duplicated here. **Backups** are handled by the Kopia role
> (snapshots the bind-mounted `containers/` data), not the legacy Duplicati setup.

Not covered here: home-router port forwarding and Cloudflare DNS setup.

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
Repo cloned at `~/server/Email-to-RSS`. Admin UI at <https://email-rss.daniel-hunter.com/admin>.

**Prerequisites:** Node.js 20+, Cloudflare account, ForwardEmail account, domain managed in Cloudflare DNS.

**Initial setup (already done — for reference):**

1. Clone repo: `git clone https://github.com/yl8976/Email-to-RSS.git`
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
