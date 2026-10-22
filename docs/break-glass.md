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

1. **The off-box recovery age key.** A password-manager-held age keypair, added as a SOPS
   recipient on 2026-06-11 so `ansible/vars/secrets.yml` survives losing every host's own key.
   The host recipients are `daniel-server` and `daniel-box`; `daniel-pi`'s key was removed on
   2026-10-03, because the Pi is driven over ssh and never decrypts. The recovery key decrypts
   the SOPS-encrypted secrets file; nothing else in this list works without it if every
   cluster host's own key is also gone.
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
   `longhorn-b2`, rendered from the `longhorn_b2_*` SOPS keys) holds everything not
   explicitly routed to R2; R2 (credential Secret
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
   itself does not strand everything else on this list. The bundle carries no issue history,
   so beside it keep the `claude` issue register exported by `findings.py export`: every
   refuted and accepted ruling lives only on GitHub, and without it the next review re-files
   them.
   Source: `docs/longhorn-disaster-recovery.md` ("The off-site recovery kit").

9. **The host-package archive signing key.** The `homelab_builder` role generates it once on
   daniel-box, in `homelab-signer`'s GnuPG home `/var/lib/homelab-signer/gnupg/` (mode
   0700). Every host's apt trusts its public half. The role's teardown never removes that
   home, so a disarm keeps the key. The password manager holds the copy, which the operator
   exports after the first apply that generates the key:
   `sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --armor
   --export-secret-keys`. Then stop the agent that the export starts, with
   `sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent`.
   Left running, it is another uid's process in the operator's login slice. The worktree
   tools cannot read such a process's cwd, so they treat every worktree as in use.
    - **To restore it** onto a rebuilt daniel-box, run `initial_setup.yml --tags
      homelab_builder` once with the flag armed. That apply creates the users and
      directories and generates a fresh key. As `homelab-signer`, import the copy with
      `gpg --homedir /var/lib/homelab-signer/gnupg --batch --import`, then delete the fresh
      key with `--batch --yes --delete-secret-and-public-key <its fingerprint>`. Leave
      `openpgp-revocs.d/` in place: it is the key generation's `creates:` guard, and without
      it the next apply generates another key. Then re-run the same apply. From the plan's PR H3.5a, that apply also runs the task that
      writes `/etc/homelab-builder/archive-key.fpr`, so the builder names the restored key.
    - **To rotate it**, follow *Rotating the archive key, or recovering from a revoked one*
      in `docs/host-packages.md`. A rotation re-pins every host, so it is never a step in a
      routine disarm or re-arm.
   Source: `ansible/roles/setup/homelab_builder/CLAUDE.md` and `docs/host-packages.md`.

10. **The cluster age key.** A dedicated age keypair that decrypts only the cluster file
    (`ansible/vars/cluster-vars.sops.env`) and the SOPS manifests under
    `ansible/vars/cluster-manifests/`. It never decrypts `ansible/vars/secrets.yml`. Its
    private half lives in the password manager, beside item 1, and in the Secret
    `flux-system/sops-age`. The cluster needs it only as that Secret: `kustomize-controller`
    reads `sops-age` to decrypt the cluster file into the Secret `homelab-cluster-vars`. A
    rebuilt cluster gets `sops-age` by hand from the password-manager copy
    (**operator-only**). No template renders it, and no runbook creates
    `homelab-cluster-vars`, which Flux regenerates on the first reconcile after `sops-age`
    exists.
    - **A lost key needs no recovery.** Replace it with *Rotating the cluster key* in
      `docs/secret-rotation.md`, from any host that decrypts `secrets.yml`. Every creation
      rule that covers a cluster file also lists the host recipients and item 1, so any of
      those keys regenerates the cluster files for the new recipient.
    - **A leaked key is not a lost key.** The rotation protects future revisions only,
      because git history and every published `sha-<sha>` artifact keep copies of the
      cluster file and of each cluster manifest, all encrypted to the old key. A suspected
      compromise also needs every key that `ansible/vars/cluster-vars.source` lists rotated
      (**operator-only**) and its consumers redeployed. That list includes the keys the
      cluster manifests hold. *Rotating the cluster key* in `docs/secret-rotation.md` has
      the steps.
   Source: `docs/target-architecture/deploy/d3-secrets.md` ("Where the values come from")
   and `docs/adr/0022-the-cluster-decrypts-its-own-file-with-its-own-key.md`.

## What to do first, by scenario

### Scenario: daniel-box is dead, daniel-server survives

daniel-server keeps its own SOPS key, so decryption still works without the recovery key.

1. Follow `docs/longhorn-disaster-recovery.md` → *Procedure (fresh host, total loss)*,
   through *Cluster bring-up*: bring up a replacement daniel-box with `ansible/bootstrap.yml`, add its
   pubkey to `ansible/.sops.yaml`, `sops updatekeys` from daniel-server, commit, pull. The
   bring-up has hand steps since H1.2: the first `k3s-bringup.yml` run skips the `kubeconfig`
   tag, the operator applies `cluster-base` by hand, and the root `homelab` Kustomization stays
   suspended until the volumes are restored (that document's *Order from H1.2*). Since H1.3 the
   bring-up installs no MetalLB: the `metallb` Kustomization does, once Flux applies the plumbing tags.
   Since H1.4 the `coredns` Kustomization owns the CoreDNS objects, and the bring-up keeps only
   the marker that releases k3s's bundled add-on.
   From H1.5a the `longhorn` Kustomization installs Longhorn and its Settings, and from H1.7b the
   `longhorn-backup` Kustomization creates the backup credentials, both backup targets and the
   RecurringJobs, once `longhorn` reads `Ready`. No `k3s-bringup.yml` run applies them: the first
   run selects no task that reads or writes a Longhorn object. The CSI sidecar requests are a
   Setting the `longhorn` Kustomization applies. The label reconcile is the
   `longhorn-reconcile` CronJob's, which the root applies once it resumes.
2. Decide whether cluster **objects** (Deployments, Secrets, PVC bindings) are also lost —
   if so, restore etcd first per `docs/k3s-etcd-restore.md` (needs item 2, the cluster
   token, if this is a genuinely new host rather than the same disk).
3. Restore Longhorn volumes per `docs/longhorn-disaster-recovery.md` (*Wait for the backupstore sync*, then
   *Restore volumes BEFORE any deploy*), **before** the deploy — deploying first provisions
   empty PVCs under the same names.
4. Deploy: `./scripts/deploy.sh`.
5. Verify with `probe.py targets` / `probe.py health <svc>` per that doc's *Verify* step.

### Scenario: daniel-server is dead, daniel-box survives

daniel-box holds the control plane, etcd and its own SOPS key, so the cluster stays up and
needs no restore. The home-critical chain is pinned to `k8s_primary_node` (daniel-box), so it
keeps running. What breaks is every workload that was on daniel-server.

Placement moves between deploys, so read it rather than trusting a list. From daniel-box:
`kubectl get pods -A -o wide --field-selector spec.nodeName=daniel-server`.

What happens without any action:

- **Stateless pods reschedule on their own.** The node controller marks daniel-server
  unreachable after about 50 s, and each pod's default `node.kubernetes.io/unreachable`
  toleration expires 300 s later. The pods then start on daniel-box.
- **CoreDNS is one of those, and it has one replica.** In-cluster name resolution fails for
  about six minutes, until the replacement pod is Ready. Pods with a cached answer or an IP
  keep working. Do not chase DNS errors in that window.
- **Pods with a Longhorn RWO volume stay Terminating.** Longhorn's
  `node-down-pod-deletion-policy` is `do-nothing`, and the `DECIDED:` comment on
  `k3s_longhorn_node_down_pod_deletion_policy` in `ansible/roles/k8s/longhorn/defaults/main.yml`
  says why. Their volumes stay attached to the dead node until step 2 below.
- **`nut` cannot move.** It is pinned to daniel-server because the UPS is USB-attached there.
  While daniel-server is down, daniel-box gets no power-loss shutdown signal and runs to battery
  end on a power cut.

Steps:

1. Confirm daniel-server is down rather than cut off. Check its console or power. If it is
   still running but unreachable from daniel-box, fix the network instead: step 2 would start a
   second copy of each stateful pod against the other replica.
2. Shed load first, per *one node is down and the survivor runs short of memory* below.
   daniel-box does not have the memory for daniel-server's whole load.
3. Force-delete each stuck pod that you still need, using the k3s admin kubeconfig, because plain
   `kubectl` is read-only: `sudo k3s kubectl -n <ns> delete pod <pod> --force --grace-period=0`.
   The replacement can then report a Multi-Attach error for up to about 6 minutes, until
   Kubernetes force-detaches the volume from the dead node. That wait is expected. Longhorn
   then attaches the volume on daniel-box from its local replica.
4. Verify with `probe.py targets` and `probe.py health <svc>` for each service you brought back.
5. Once daniel-server is back, Longhorn rebuilds its replicas from daniel-box. Bring back each
   shed service by the shed scenario's step 4, below: it reads `DEPLOY_HOLD` first, resumes
   each flux-owned service and redeploys each Ansible-owned one. The rescheduled pods stay on
   daniel-box until their next rollout.

### Scenario: one node is down and the survivor runs short of memory

k3s reschedules the lost node's pods onto the survivor, which may not hold every workload.
The home-critical chain is pinned to `k8s_primary_node`, so it stays Pending while
daniel-box is down, whatever this step frees.

Every step below is **operator-only**. Each needs the k3s admin kubeconfig or a write to the
`DEPLOY_HOLD` repository variable, and an agent session has neither. A flux-owned service
needs more than a scale: Flux's drift correction restores the rendered `replicas`, and
`./scripts/deploy.sh --tags <svc>` exits 5 for a flux-owned tag. So a flux-owned service is
suspended before it is scaled and resumed rather than redeployed.

1. Print the shed set: `uv run python scripts/diagnostics/probe.py shed-set`. It lists the
   Deployments and StatefulSets of every service with no `tier:` in `containers_list`, each
   with its owner. Every tiered service is left off it, and so is the cluster plumbing that
   `ansible/filter_plugins/service_tier.py:NEVER_SHED` names. For each flux-owned service it
   also prints the suspend and resume commands used below.
2. If the set holds a flux-owned service, set the promotion hold first:
   `gh variable set DEPLOY_HOLD --body <reason> -R DanielH2018/server`. Note the reason you
   set. A hand suspend is not in `digests.json`, so when a suspended tag is in a later
   revision's changed set, `promote`'s `verify` would wait on it until it timed out.
    - **When GitHub cannot be reached**, skip this step and record the reason and the time in
      a local file on daniel-box. Step 3 needs only the LAN, so it runs as written. Once the
      WAN returns, run this step's `gh variable set DEPLOY_HOLD` first, before any resume and
      before step 4, because a queued `promote` run can start as soon as GitHub answers.
3. For each flux-owned service in the set, suspend its tag, then scale it to zero. Use the
   field manager `homelab-operator`, so Flux leaves the field alone:

    ```bash
    sudo k3s kubectl -n flux-system patch kustomization <tag> --type merge \
      --field-manager=homelab-operator -p '{"spec":{"suspend":true}}'
    ```

   A suspend frees the reconcile worker that the tag holds only when that worker's current
   wait ends, which can take up to the tag's `timeout`. Scale each Ansible-owned service to
   zero as it stands. Start with the largest, and use the k3s admin kubeconfig, because plain
   `kubectl` on daniel-box is read-only.
4. Once the node is back, and before any resume, read the hold with
   `gh variable get DEPLOY_HOLD -R DanielH2018/server` and compare it with step 2's reason.
   Step 2 does not stop a `promote` run that was already past `decide`, because `decide`
   reads the hold only before it moves `prod`. If that run's `verify` failed, it wrote its
   revision's SHA over the reason, and it may have reset `prod` to `last-good`. A reset moves
   every tag that is not suspended to `last-good` at once, and each suspended tag applies `last-good`
   when it resumes.
    - **The hold names a SHA.** It records a failed revision, so do not clear it here. Work
      it by the revert runbook's *Choosing the branch* in `docs/volume-snapshot-drills.md`,
      and resume the shed tags after that branch's own resume step; the branch's last step
      clears the hold. A hold the revert runbook does not cover, one that names no
      `stateful=` roles, is worked by `docs/flux-deploy-plane.md`'s *Verification* instead,
      then resume as below.
    - **The hold still reads step 2's reason.** Resume each suspended tag with the same patch
      and `false`, rather than running `deploy.sh`:

        ```bash
        sudo k3s kubectl -n flux-system patch kustomization <tag> --type merge \
          --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'
        ```

      Flux's next reconcile restores the rendered replica count. Wait until each tag reads
      Ready, then clear the hold with
      `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.

   Redeploy each Ansible-owned service with `./scripts/deploy.sh --tags <svc>`, as before.
   The deploy applies the replica count its manifests declare, so it scales the service back
   up.

### Scenario: Longhorn backups must stop and the disarm PR cannot reach the cluster

The normal lever is a PR that sets `k3s_longhorn_backup_armed: false` (or
`k3s_longhorn_r2_armed: false`) in `ansible/roles/k8s/longhorn-backup/defaults/main.yml`. Promote
applies it like any revision, and a failed revision never resets across it, because
`longhorn-backup` is forward-only. Three cases defeat that path: `DEPLOY_HOLD` holds a value other
than `none`, a transaction-cap spiral outruns a merge, and `longhorn` is not Ready, so the
`longhorn-backup` Kustomization applies nothing. For those, every step below is
**operator-only**:

1. Annotate both BackupTargets, so kustomize-controller skips them:
   `sudo k3s kubectl -n longhorn-system annotate backuptargets.longhorn.io/default backuptargets.longhorn.io/r2 kustomize.toolkit.fluxcd.io/reconcile=disabled`.
2. Patch each `backupTargetURL` blank:
   `sudo k3s kubectl -n longhorn-system patch backuptargets.longhorn.io/default --type merge -p '{"spec":{"backupTargetURL":""}}'`,
   then the same for `backuptargets.longhorn.io/r2`.
3. Land the disarm PR, then clear the hold:
   `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.
4. Wait until `longhorn-backup`'s `status.lastAppliedOriginRevision` names the disarm commit or a
   descendant of it. Read the revision with
   `sudo k3s kubectl -n flux-system get kustomization longhorn-backup -o jsonpath='{.status.lastAppliedOriginRevision}'`,
   which prints `master@sha1:<sha>`, and check it with
   `git merge-base --is-ancestor <disarm-sha> <sha>`. Only then remove the annotation from
   each target:
   `sudo k3s kubectl -n longhorn-system annotate backuptargets.longhorn.io/default backuptargets.longhorn.io/r2 kustomize.toolkit.fluxcd.io/reconcile-`.
   `prod` carrying the disarm is not enough, because the tag applies nothing while `longhorn` is
   not Ready. Removing the annotation earlier lets Flux restore the armed URL from the
   repository.

Until step 4, the backup-health monitor reads each annotated target as a problem, and
`probe.py health longhorn-backup` fails and names it. A forgotten break-glass therefore cannot sit
behind a green monitor. `docs/adr/0007-backup-tiering-r2-daily-b2-weekly.md` records the choice.

### Scenario: the laptop/machine holding your working access is dead, the lab hosts are fine

Ansible runs from daniel-box in normal operation (repo-root CLAUDE.md, *Project Overview*).
If your own working machine is what's gone, the lab itself needs nothing done to it.

1. SSH into daniel-box directly using credentials from the password manager — it holds
   its own age key and its own checkout, so you can work from there without rebuilding
   anything.
2. If daniel-box itself is unreachable this way, clone the repo fresh (item 8, or straight
   from GitHub) onto any replacement machine, drop the recovery age key (item 1) at
   `~/.config/sops/age/keys.txt`, and `sops -d ansible/vars/secrets.yml` to confirm decrypt
   before doing anything else. **Operator-only:** this prints every secret, and the agent
   guard denies it (`docs/claude-shell-permissions.md`, *Inspecting a secret without
   decrypting it*).
3. From there, operate normally — `./scripts/deploy.sh`, `probe.py`, etc., per the
   repo-root CLAUDE.md command tables.

### Scenario: `systemctl restart k3s` never returns after an API-server flag change

`k3s_server_args` (`ansible/roles/setup/k3s/defaults/main.yml`) reconfigures the API server
through `--kube-apiserver-arg=...`, and the k3s install task re-runs the installer whenever
that string gains an argument. An argument the API server does not recognise makes it refuse
to start, and the k3s unit is `Type=notify` with `TimeoutStartSec=0` — so the restart **hangs
indefinitely** instead of failing, which is the same symptom the `--node-ip` note in that file
describes for a different cause.

1. Read `tail -n 200 /var/log/k3s.log` on daniel-box. A rejected flag names itself. (Not
   `journalctl -u k3s`: k3s's own lines are priority info, which the journald cap drops;
   the k3s role sends them to that file instead.)
2. Fix `ExecStart` in `/etc/systemd/system/k3s.service` directly, `systemctl daemon-reload`,
   and restart. The unit file is the live configuration; the repo is not.
3. Then fix the repo, because the install task detects an argument that was **added**, never
   one that was **removed**. Deleting a line from `k3s_server_args` does not un-configure an
   installed host — re-run `/usr/local/bin/k3s-install.sh` by hand with the corrected
   `INSTALL_K3S_EXEC`.

A **wrong-but-valid** value is a different and much smaller problem. The `oidc-*` flags in
particular initialise asynchronously, so a misspelled or unreachable issuer leaves the API
server up and serving: OIDC logins fail and nothing else does. The root kubeconfig
authenticates by client certificate, which no `oidc-*` flag touches.

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
| k3s etcd restore (full, onto a replacement host) | **Partly, and ongoing.** The `--list-only` leg runs weekly by cron and has been verified since 2026-08-22. The full restore first passed 2026-09-11 — `offbox-daniel-box-1789094702.zip` restored in a throwaway guest on daniel-server and served 8 namespaces, 72 Deployments, 45 PVCs, 48 CRDs and 51 Secrets — and runs monthly since. Still not drilled: the restore onto a REPLACEMENT HOST, the agent rejoin and the Longhorn reattach. | `docs/k3s-etcd-restore.md`, *The full drill runs monthly in a throwaway guest* |
| Longhorn volume restore | **Yes, and ongoing.** First attempt 2026-08-15 failed on a B2 cap (not the data); retry 2026-08-16 passed (`traefik-acme`, ~21s, verified real data). Scheduled nightly since 2026-08-19, rotating one volume per night over the full backup set since 2026-08-20. | `docs/longhorn-disaster-recovery.md`: "Assurance gap (known, narrowing)" |
| SOPS decrypt with the recovery age key alone (no host key) | **No record found.** No drill of this specific path is recorded anywhere in the repo. | absence of any citation — see *Annual drill* below |
| GitOps bootstrap (`bootstrap.yml` → `sops updatekeys` → onboard) | **Yes, routinely** — the standard way every host (daniel-pi, whose key was removed on 2026-10-03, and daniel-box) was onboarded. Never specifically exercised as a *total-loss* recovery (starting from the recovery key rather than a host's own fresh key). | `ansible/roles/setup/sops_setup/CLAUDE.md`, `ansible/bootstrap.yml` header |
| Full total-loss sequence end to end (etcd + Longhorn + redeploy, in order, on hardware with nothing pre-existing) | **No.** Each piece above has partial or full drill coverage on its own; the sequence has not been run together. | inferred from the above — no doc claims otherwise |

## Annual drill (recommended — do not run this now)

Once a year, on a scratch host with no other access to this repo's live infrastructure:

1. Place **only** the recovery age key (item 1) at `~/.config/sops/age/keys.txt`.
2. Clone the repo (or restore item 8's bundle).
3. Run `sops -d ansible/vars/secrets.yml` and confirm it decrypts cleanly, using the
   recovery recipient alone — no host key present. **Operator-only:** an agent cannot run
   this step, because the guard denies any `sops` decrypt.
4. Record the date and outcome in this doc's *Documented vs drilled* table, in the SOPS
   decrypt row, the same way `docs/longhorn-disaster-recovery.md`, *Assurance gap (known,
   narrowing)*, records its own drill date.
5. Destroy the scratch host's copy of both the key and the decrypted output afterward.
