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

## Notes / gotchas
- **`kopia repository connect` ≠ `create`.** Never run `create` against the existing bucket
  in a recovery — `connect` attaches to the existing repo; `create` would try to initialize
  a new one. The role entrypoint only `create`s when `connect` fails (genuinely empty bucket).
- **Maintenance ownership** stays with the original identity (`root@kopia`) recorded in the
  repo; after a fresh-host reconnect the entrypoint re-asserts it (`kopia maintenance set
  --owner me ...`) so blob GC resumes.
- **The Pi is intentionally not in Kopia scope** — its services are stateless / Ansible-
  reconstructible (wg-easy keys re-template, clients re-enroll).
- Test the read path any time without a real disaster: `kopia snapshot list` + a scratch
  `kopia restore <obj> /tmp/dr-test` from a host with the SOPS key.
