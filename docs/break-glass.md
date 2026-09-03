# Break glass — rebuilding or recovering the lab without the author

This page is the single entry point for two situations: the author is unavailable and
something is red, or daniel-box (or the author's own machine) is gone entirely. It assumes
the reader is either a second person holding a phone and the password manager, or the
author working from a replacement machine after total loss. It links to the runbook that
covers each step in depth rather than repeating their procedures — follow the link, not a
summary of it.

**Read the *documented vs drilled* table below before trusting any step.** Several
procedures here have never been executed end to end. A runbook that has not been drilled
can still be the right thing to run — it is usually still better than improvising — but
know which one you are relying on before you start.

## The recovery kit — everything needed from outside the cluster

Use these in this order. Each lives in the password manager unless the citation says
otherwise; none of the values below are secrets themselves, only names and locations.

1. **The off-box recovery age key.** A password-manager-held age keypair, added as a fourth
   SOPS recipient on 2026-06-11 specifically so `ansible/vars/secrets.yml` survives losing
   both `daniel-server` and `daniel-pi`'s own host keys. It decrypts the SOPS-encrypted
   secrets file; nothing else in this list works without it if every cluster host's own key
   is also gone.
   Source: `ansible/.sops.yaml` (recipient comments above the `age:` line) and
   `ansible/roles/setup/sops_setup/CLAUDE.md` ("Notable" section).

2. **The k3s cluster token.** A copy taken out-of-band on 2026-08-23, matching
   `/var/lib/rancher/k3s/server/token` on daniel-box at the time of copy. Since
   2026-08-20 (`--secrets-encryption` armed), the etcd snapshot cannot decrypt Secrets, CA
   certs, or the service key without this token — restoring onto a *replacement* host is a
   no-op without it. Restoring onto daniel-box's own intact disk does not need it.
   Source: `docs/k3s-etcd-restore.md` ("Since 2026-08-20…" section).

3. **The etcd snapshot.** Off-box copies land daily at 02:45 in
   `s3://<r2_bucket>/etcd-snapshots/`, 14 retained, named
   `offbox-<node>-<unix-timestamp>.zip`. Local copies (daniel-box's own disk, not
   survivable in a total-loss event) sit at
   `/var/lib/rancher/k3s/server/db/snapshots/`, taken at 00:00 and 12:00, 5 retained.
   Credentials to reach the R2 copy are in
   `/etc/rancher/k3s/etcd-s3.env` on daniel-box — rendered from SOPS, so item 1 or an
   intact host key is the way back to them if that file itself is gone.
   Source: `docs/k3s-etcd-restore.md` ("Where the snapshots are").

4. **Longhorn's B2 and R2 backup targets.** Every PVC's data, split across two targets by
   volume: B2 (`s3://daniel-server-kopia@us-east-005/longhorn`, credential Secret
   `longhorn-b2`, rendered from the `kopia_b2_*` SOPS keys — Longhorn's credentials
   despite the name) holds everything not explicitly routed to R2; R2 (credential Secret
   `longhorn-r2`, rendered from the `r2_*` SOPS keys) holds
   `traefik-acme`, `authelia-config`, `home-assistant-config`, and `zigbee2mqtt-data` — the
   TLS material, the SSO store, and the two slow-to-rebuild home-automation stores. Both
   credential sets decrypt from SOPS, so item 1 gates this too.
   Source: `docs/longhorn-disaster-recovery.md` ("Two targets") and
   `docs/adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md`.

5. **Console logins for Healthchecks.io, Cloudflare, Backblaze B2, and GitHub.** These are
   third-party account credentials, not SOPS secrets — they live in the password manager
   only. `docs/reference/secrets.md`'s `external` tier tracks *rotation dates* for the
   tokens SOPS holds (the Cloudflare DNS token, webhook URLs, and similar), not the console
   login itself; the console login is the thing that lets a locked-out operator regenerate
   one of those tokens by hand. The Healthchecks.io check period/grace settings live only
   in that console — nothing in the repo can set them.
   Source: `docs/reference/secrets.md` (`external` section) and
   `docs/healthchecks-io-deadman.md` ("Period and grace").

6. **The router's WireGuard port-forward.** UDP `51820` forwards to daniel-box, where
   wg-easy runs as a k8s workload (`roles/k8s/wg-easy`). If daniel-box is rebuilt on new
   hardware, the forward's destination IP needs updating to match; the client subnet
   (`10.8.0.0/24`) and admin UI (`wg-easy.daniel-hunter.com`, behind Authelia) do not
   change. `daniel-pi` runs a second, LAN-only wg-easy on `51822/udp` — not this path, and
   not forwarded.
   Source: `docs/wireguard-private-homelab-access.md` ("Server-side networking").

7. **The GitOps bootstrap path.** `ansible/bootstrap.yml` has no secret dependency — it is
   the way in on a host with no age key yet. It installs the `age`/`sops` binaries and
   either generates a fresh keypair or, if the recovery private key (item 1) is placed at
   `~/.config/sops/age/keys.txt` first, skips generation and uses that key directly. After
   it runs, `initial_setup.yml` and `deploy.yml`'s secret-load pre-tasks work normally.
   Source: `ansible/bootstrap.yml` (header) and the `add-secret` skill's
   *Onboarding a host that cannot decrypt yet* section
   (`.claude/skills/add-secret/SKILL.md`).

8. **A GitHub-hosted repo bundle, refreshed alongside the age-key backup.** GitHub holds
   the only off-site copy of the encrypted `secrets.yml` and all the Ansible; the age key
   alone cannot reconstruct it. Keep a `git bundle create … --all` snapshot in the same
   off-site place as the recovery age key so a simultaneous loss of both hosts and GitHub
   itself does not strand everything else on this list.
   Source: `docs/longhorn-disaster-recovery.md` ("The off-site recovery kit").

## What to do first, by scenario

### Scenario: daniel-box is dead, daniel-server survives

daniel-server keeps its own SOPS key, so decryption still works without the recovery key.

1. Follow `docs/longhorn-disaster-recovery.md` → *Procedure (fresh host, total loss)*,
   steps 1–2: bring up a replacement daniel-box with `ansible/bootstrap.yml`, add its
   pubkey to `ansible/.sops.yaml`, `sops updatekeys` from daniel-server, commit, pull.
2. Decide whether cluster **objects** (Deployments, Secrets, PVC bindings) are also lost —
   if so, restore etcd first per `docs/k3s-etcd-restore.md` (needs item 2, the cluster
   token, if this is a genuinely new host rather than the same disk).
3. Restore Longhorn volumes per `docs/longhorn-disaster-recovery.md` steps 3–4, **before**
   `deploy.yml` — deploying first provisions empty PVCs under the same names.
4. Deploy: `uv run ansible-playbook ansible/deploy.yml`.
5. Verify with `probe.py targets` / `probe.py health <svc>` per that doc's step 6.

### Scenario: the laptop/machine holding your working access is dead, the lab hosts are fine

Ansible runs from daniel-box in normal operation (repo-root CLAUDE.md, *Project Overview*).
If your own working machine is what's gone, the lab itself needs nothing done to it.

1. SSH into daniel-box directly using credentials from the password manager — it holds
   its own age key and its own checkout, so you can work from there without rebuilding
   anything.
2. If daniel-box itself is unreachable this way, clone the repo fresh (item 8, or straight
   from GitHub) onto any replacement machine, drop the recovery age key (item 1) at
   `~/.config/sops/age/keys.txt`, and `sops -d ansible/vars/secrets.yml` to confirm decrypt
   before doing anything else.
3. From there, operate normally — `./scripts/deploy.sh`, `probe.py`, etc., per the
   repo-root CLAUDE.md command tables.

### Scenario: the author is unavailable and something is red

For a second person without deep familiarity with the repo:

1. Check the Uptime Kuma board first — most red tiles link to a specific service or
   subsystem. `docs/reference/services.md` names what each service is and who/what depends
   on it.
2. If Kuma itself is unreachable (its own outage, or a cluster/WAN outage), check
   Healthchecks.io instead — it is off-site by design for exactly this case
   (`docs/healthchecks-io-deadman.md`, *What is wired*). A silent check there means the
   underlying cron or the cluster itself stopped, not just the alert path.
3. Do **not** attempt an etcd restore, a Longhorn restore, or a SOPS key change without
   the author present, unless the situation is total loss of daniel-box and the runbooks
   above are the only path forward. Each of those procedures is destructive if run against
   the wrong target (`docs/k3s-etcd-restore.md`: "rolls the whole cluster back to the
   snapshot's moment").
4. If the fix is genuinely urgent and nobody with repo access is reachable, the recovery
   kit above (items 1, 2, 3, 7, 8) is what lets a competent operator rebuild from scratch —
   hand it to them rather than improvising a partial fix.

## Documented vs drilled

Never read a runbook's existence as proof it has been exercised. This table states only
what each source records — not what "should" have happened.

| Runbook | Drilled? | Evidence |
|---|---|---|
| k3s etcd restore (full, onto a replacement host) | **No.** The `--list-only` leg runs weekly by cron and has been verified since 2026-08-22 (credentials work, a snapshot downloads and decompresses); the full restore — reading the object graph back out — has never been performed. | `docs/k3s-etcd-restore.md`: "the restore is still NOT drilled" |
| Longhorn volume restore | **Yes, and ongoing.** First attempt 2026-08-15 failed on a B2 cap (not the data); retry 2026-08-16 passed (`traefik-acme`, ~21s, verified real data). Scheduled nightly since 2026-08-19, rotating one volume per night over the full backup set since 2026-08-20. | `docs/longhorn-disaster-recovery.md`: "Assurance gap (known, narrowing)" |
| Kopia disaster recovery | **N/A — tool retired 2026-08-14.** Doc kept as history only; do not follow it for a live recovery. | `docs/adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md` |
| SOPS decrypt with the recovery age key alone (no host key) | **No record found.** No drill of this specific path is recorded anywhere in the repo. | absence of any citation — see *Annual drill* below |
| GitOps bootstrap (`bootstrap.yml` → `sops updatekeys` → onboard) | **Yes, routinely** — the standard way every host (daniel-pi, daniel-box) was onboarded. Never specifically exercised as a *total-loss* recovery (starting from the recovery key rather than a host's own fresh key). | `ansible/roles/setup/sops_setup/CLAUDE.md`, `ansible/bootstrap.yml` header |
| Full total-loss sequence end to end (etcd + Longhorn + redeploy, in order, on hardware with nothing pre-existing) | **No.** Each piece above has partial or full drill coverage on its own; the sequence has not been run together. | inferred from the above — no doc claims otherwise |

## Annual drill (recommended — do not run this now)

Once a year, on a scratch host with no other access to this repo's live infrastructure:

1. Place **only** the recovery age key (item 1) at `~/.config/sops/age/keys.txt`.
2. Clone the repo (or restore item 8's bundle).
3. Run `sops -d ansible/vars/secrets.yml` and confirm it decrypts cleanly, using the
   recovery recipient alone — no host key present.
4. Record the date and outcome in this doc's *Documented vs drilled* table, in the SOPS
   decrypt row, the same way `docs/longhorn-disaster-recovery.md:172` records its own
   drill date.
5. Destroy the scratch host's copy of both the key and the decrypted output afterward.

This drill is out of scope for this change — it is a recommendation for a future session to
execute, not something run here.
