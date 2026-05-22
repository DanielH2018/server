# Server Homelab Setup

## Send Setup Folder to Server

1. Create SSH key
   1. Follow <https://phoenixnap.com/kb/generate-ssh-key-windows-10>
2. Login to the remote machine and setup password
3. Setup WiFi (if needed):
   1. Run `cd /etc/netplan`.
   2. Run `ls` and note the file present.
   3. Run `sudo nano <file>`.
   4. Copy the following:

      ```yaml
      wifis:
          wlan0:
              dhcp4: true
              optional: true
              access-points:
                  "Wifi SSID":
                      password: your-wifi-password
      ```

   5. Run `sudo netplan apply`
   6. Run `ip a` and locate the server's local ip address
4. Copy SSH key from local to remote:
   1. On local, run `type C:\Users\<username>\.ssh\id_rsa.pub | ssh username@remote_host "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"`.
5. SSH with key into server
6. Copy repository from git
   1. Run `git config --global user.name "your_username"`.
   2. Run `git config --global user.email "your_email_address@example.com"`.
   3. Run `git config --global credential.helper store`
   4. Run `git clone https://github.com/DanielH2018/server.git`.
      1. For password, provide a personal token generated from github.
7. Copy secrets.yml from local to remote:
   1. Run `scp -r <secrets file path> ubuntu@<server ip>:~/server/ansible/`
      1. When prompted, enter the password for the remote user
8. Fix lvm (as needed, partition name likely different):
   1. Run `sudo lvm`
   2. Run `lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv`
   3. Run `exit`
   4. Run `sudo resize2fs /dev/ubuntu-vg/ubuntu-lv`

For more instructions, look at the README in the ansible/ folder.

Not covered in these docs is port forwarding, and Cloudflare DNS setup.

## Setup Server Environment

1. If using Intel XE graphics, ensure `/dev/dri/` exists, otherwise run `sudo apt install linux-oem-22.04` and reboot.
2. Run `pip install ansible`
3. Run `ansible-playbook initial_setup.yml --ask-become-pass`.
4. Run `source ~/.bashrc`
5. Run `ansible-playbook deploy.yml --ask-become-pass`.
6. Run `docker exec crowdsec cscli bouncers add bouncer-traefik` and save api key to .env

## Add Container to Server Environment

1. Create role and tags.
2. Create folder in `roles/containers`, and create `tasks` and `templates` subdirectories.
3. Create `main.yml` in `tasks` and `docker-compose.yml.j2` in templates.
4. Add environment variables to .env and update the docker compose.
5. Add traefik labels and cloudflare CNAME as needed.
6. Add entry to `inventory/host_vars` for each server it should run on.
7. Run `ansible-playbook deploy.yml --tags "<svc-name>"`.

## Setup LaTeX Editor

1. Clone Resume repository in server
2. Copy .devcontainer from <https://github.com/James-Yu/LaTeX-Workshop/tree/master/samples/docker>
3. Install VS Code Remote - Containers, and SSH
4. Reopen the Resume directory with the container

## Setup Intel QSV

1. `sudo mkdir -p /etc/modprobe.d`
2. `sudo sh -c "echo 'options i915 enable_guc=2' >> /etc/modprobe.d/i915.conf"`
3. `sudo update-initramfs -u && sudo update-grub`
4. `sudo reboot`

## Duplicati

1. For backing up to Google Drive, to store not in the root directory, you need a full access token which can be attained here: <https://duplicati-oauth-handler.appspot.com/>

## journald Logs

1. `sudo nano /etc/systemd/journald.conf`
2. Find, uncomment and change the parameters: `MaxLevelStore=notice` `MaxLevelSyslog=notice`
3. `sudo systemctl restart systemd-journald`

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
