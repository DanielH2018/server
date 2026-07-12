# Kopia Disaster Recovery — bare-metal restore from B2

Recover the homelab's container state when **the server is gone** (dead disk, lost host,
total-loss event). The Kopia repository lives off-site in Backblaze B2, and every
credential needed to reach it is in SOPS — which is itself DR-closed (the age host keys
are backed up out-of-band + there's an off-box recovery recipient). So the capability
survives a total loss; this runbook is the procedure.

> For the *routine* "restore one service dir" case, the monthly drill
> (`/usr/local/bin/kopia-restore-drill.sh`) and the role
> [`CLAUDE.md`](../ansible/roles/containers/kopia/CLAUDE.md) already cover it. This doc is
> the full-rebuild case.

## What you need
1. **The SOPS age key** for at least one recipient in `ansible/.sops.yaml` (from your
   out-of-band backup, restored to `~/.config/sops/age/keys.txt` on the new host — see
   [secret-rotation.md](secret-rotation.md) and the bootstrap flow in
   `ansible/bootstrap.yml`).
2. A checkout of this repo (`git clone`), so you can `sops -d ansible/vars/secrets.yml`.
   **GitHub is the third independent leg of recovery** (alongside B2 for the data and the
   out-of-band age key to decrypt): it holds the *only* off-site copy of the encrypted
   `secrets.yml` + all the Ansible. The age key alone can't reconstruct `secrets.yml`, so a
   *simultaneous* loss of both the hosts and the GitHub repo would strand the B2 credentials.
   Cheap insurance: keep an occasional `git bundle create homelab.bundle --all` stored
   alongside the out-of-band age-key backup, or push to a second git remote.
3. Docker (or a local `kopia` binary).

## The five repository credentials (all in `ansible/vars/secrets.yml`)
| Secret | Purpose |
|--------|---------|
| `kopia_password` | repository **encryption** password (`KOPIA_PASSWORD`) — without it the data is unrecoverable |
| `kopia_b2_key_id` | B2 application key id (`KOPIA_B2_KEY_ID`) |
| `kopia_b2_application_key` | B2 application key (`KOPIA_B2_APPLICATION_KEY`) |
| `kopia_b2_bucket` | bucket name (`KOPIA_B2_BUCKET`) |
| `kopia_b2_endpoint` | S3-compatible endpoint (`KOPIA_B2_ENDPOINT`) |

```bash
sops -d ansible/vars/secrets.yml | grep -E 'kopia_(password|b2_)'
```

## Step 1 — connect to the repository from a fresh host
Mirrors `ansible/roles/containers/kopia/templates/entrypoint.sh.j2`. Export the five values
first, then (the repo speaks B2's **S3** endpoint):

```bash
export KOPIA_PASSWORD=...           KOPIA_B2_KEY_ID=...
export KOPIA_B2_APPLICATION_KEY=... KOPIA_B2_BUCKET=...   KOPIA_B2_ENDPOINT=...

docker run --rm -it \
  -e KOPIA_PASSWORD \
  -v "$HOME/.kopia-dr:/app/config" \
  kopia/kopia:latest repository connect s3 \
    --bucket="$KOPIA_B2_BUCKET" \
    --endpoint="$KOPIA_B2_ENDPOINT" \
    --access-key="$KOPIA_B2_KEY_ID" \
    --secret-access-key="$KOPIA_B2_APPLICATION_KEY"
```

Sanity-check: `... kopia repository status` and `... kopia snapshot list`.

## Step 2 — restore the container tree
The single snapshot source is `/data/home/ubuntu/server/containers`. Restore the whole
tree (or one `rootEntry.obj/<service>` subdir) to where the rebuilt host expects it:

```bash
ROOT=$(docker run ... kopia snapshot list --json | jq -r '.[-1].rootEntry.obj')
docker run ... kopia restore "$ROOT" /home/ubuntu/server/containers
```

Restoring an **older** state? `kopia snapshot list` shows all retained snapshots
(7 daily + 4 weekly + 3 monthly — see the entrypoint retention policy); pass that
snapshot's `rootEntry.obj`.

## Step 3 — fix ownership + bring services back
```bash
sudo chown -R 1000:1000 /home/ubuntu/server/containers   # PUID/PGID=1000
```
Then redeploy via Ansible (it re-renders compose, configs, and host units):
```bash
uv run ansible-playbook ansible/initial_setup.yml      # first-host bring-up (see ansible/README.md)
uv run ansible-playbook ansible/deploy.yml             # all containers
```
Bring up infra first if doing it piecemeal: **traefik → authelia → pihole/unbound →
the rest**. Verify DNS (`pihole`), SSO (`authelia`), then app data.

**Re-create the Uptime-Kuma admin user.** `uptime-kuma`'s data dir is deliberately *excluded*
from Kopia (`kopiaignore.j2`), so a bare-metal restore brings up a **fresh** Kuma with an empty
DB. AutoKuma authenticates with `uptime_kuma_username`/`uptime_kuma_password` from `secrets.yml`
but **cannot create the initial admin** — so until you set up that admin through Kuma's first-run
UI (`kuma.<domain>`, using exactly those secret values), AutoKuma provisions **zero** monitors and
no Discord notification, leaving the whole fleet unmonitored during the recovery window. Do this
right after the deploy; AutoKuma then backfills every monitor + the notification automatically.

**Regenerate the Kuma API key for the Prometheus scrape.** Prometheus' `uptime-kuma` scrape job
authenticates with `prometheus_kuma_api_key` (an HTTP-basic password) that Kuma **issues and stores
only in its own SQLite DB** — which is excluded from Kopia, so a fresh Kuma invalidates the old key.
The SOPS value is now stale, so the `uptime-kuma` scrape target comes up **DOWN (401)** — during the
recovery, exactly when monitoring matters. After recreating the admin user, mint a new key in
Kuma (**Settings → API Keys**), `sops ansible/vars/secrets.yml` to set `prometheus_kuma_api_key` to it,
then redeploy prometheus (`uv run ansible-playbook ansible/deploy.yml --tags prometheus`). The
Scrape-Targets monitor flags this if you miss it.

## Notes / gotchas
- **`kopia repository connect` ≠ `create`.** Never run `create` against the existing bucket
  in a recovery — `connect` attaches to the existing repo; `create` would try to initialize
  a new one. The role entrypoint only `create`s when `connect` fails (genuinely empty bucket).
- **Maintenance ownership** stays with the original identity (`root@kopia`) recorded in the
  repo; after a fresh-host reconnect the entrypoint re-asserts it (`kopia maintenance set
  --owner me ...`) so blob GC resumes.
- **The Pi is (almost) intentionally not in Kopia scope** — its services are stateless /
  Ansible-reconstructible. **The one exception (2026-07-04): the Pi's wg-easy peer configs**
  (`wg0.conf`/`wg0.json` — WireGuard private keys a redeploy can NOT rebuild). A daily
  daniel-server cron (`wg-easy-pull-pi-peers.sh`, wg-easy role) pulls them into
  `containers/wg-easy/pi-peers/` — inside the snapshot source — so an SD-card death doesn't force
  re-enrolling every VPN client. On recovery, restore that dir back to the Pi. Everything else on
  the Pi still re-templates on a redeploy.
- Test the read path any time without a real disaster: `kopia snapshot list` + a scratch
  `kopia restore <obj> /tmp/dr-test` from a host with the SOPS key.
- **The off-box dead-man's switch — an external UptimeRobot monitor — is the ONE backstop for a
  total daniel-server / Uptime-Kuma death.** The whole alert brain (monitor-bridge → Uptime-Kuma →
  Discord) lives on daniel-server, so it cannot page about its own host going down. For that backstop
  to also cover a *uptime-kuma-container* death — not just a host/network outage — it MUST probe a
  **Uptime-Kuma-served endpoint** (a public Kuma status page, e.g. `https://<kuma-host>/status/<slug>`,
  or the Kuma login page), **NOT** a generic Traefik-served URL: if the host + Traefik stay up while
  only the Kuma container dies, a generic URL still returns 200 and the backstop never fires — exactly
  when Kuma can no longer evaluate its own monitors. The monitor is deliberately out-of-repo (an
  external SaaS can't be IaC-managed), but its target is otherwise unrecorded, making this SPOF's only
  backstop un-auditable. **Record the configured UptimeRobot target here so it can be verified:**
  - **UptimeRobot monitor (recorded 2026-07-12):** dashboard
    `https://dashboard.uptimerobot.com/monitors/803270234`, probing `https://homepage.daniel-hunter.com`.
  - **KNOWN RESIDUAL — operator-accepted 2026-07-12:** that target is a generic, **Authelia-gated**
    route, NOT a Kuma-served endpoint. `homepage` is `use_authelia: true`, so an external probe only
    reaches Authelia's 302 → login portal (UptimeRobot counts the 302 as "up"). This DOES back-stop a
    total host / Cloudflare / Traefik / **Authelia** outage — but it does NOT catch a *uptime-kuma-
    container* death: with the host + Traefik + Authelia up, homepage still 302s "up" while Kuma can no
    longer evaluate its own monitors. The SPOF is real but narrow (a Kuma-only crash while everything
    else stays healthy) and consciously accepted; it is NOT a fresh review finding — don't re-flag.
  - **To close it later:** `uptime-kuma.daniel-hunter.com` is already publicly routed (Cloudflare), so
    add an Authelia `bypass` rule for `^/status/.*$` on `uptime-kuma.{{ domain }}` (configuration.yml.j2),
    create a public Kuma status page (`/status/<slug>` — served by the Kuma container, so a Kuma death →
    Traefik 502 → the probe fires), and repoint the UptimeRobot monitor at
    `https://uptime-kuma.daniel-hunter.com/status/<slug>`. Trade-off: that status page becomes publicly
    viewable (read-only; scope its contents).
