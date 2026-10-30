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

7. **The way in on a host with no age key yet.** No playbook creates the operator's key. The
   operator runs `age-keygen` by hand, or places the recovery private key (item 1) at
   `~/.config/sops/age/keys.txt` instead. The host package supplies `sops` and `age`. After
   that, `initial_setup.yml` and `deploy.yml`'s secret-load pre-tasks work normally, and
   *The age keys* below says which hosts hold a key.
   Source: `ansible/README.md` (§5) and the `add-secret` skill's
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
   The bundle is also the render source for break-glass promotion (*Scenario: GitHub, GHCR or
   Sigstore is unreachable*), so a refresh covers that use too. Beside it the kit holds the
   **break-glass private key**, a password-protected cosign key whose public half the flux role
   commits (`flux_break_glass_public_key`). It is a second signing identity: it never enters
   the repo, CI or a host, and its only use is the scenario below. The kit also holds the three
   Flux controller images at `flux_version` (`ansible/roles/k8s/flux/defaults/main.yml`),
   saved as image archives and refreshed with the bundle, for a rebuilt cluster that cannot
   reach GHCR.
   Source: `docs/longhorn-disaster-recovery.md` ("The off-site recovery kit").
    - **The offline bootstrap files.** `sudo hosts/bootstrap-host.sh <host> --offline <kit-dir>`
      reads every fetch from one directory and checks each file against the pin the download
      path checks. A file that is missing or differs from its pin stops the script and names
      the file. The directory holds, for the node's architecture:
        - `install.sh` at `K3S_VERSION`, whose sha256 is `K3S_INSTALL_SHA256` in the script;
        - the `k3s` binary (`k3s-arm64` on arm64) and the release's `sha256sum-<arch>.txt`;
        - the `nfpm` and `uv` archives that the script's pins name:
          `nfpm_<version>_Linux_x86_64.tar.gz` and `uv-x86_64-unknown-linux-gnu.tar.gz` on
          amd64, or the arm64 files the same pins name;
        - `tools-cache/`, the files that `homelab-tools`' `tools.json` pins, each named by its
          sha256, which the build reads instead of downloading;
        - `uv-cache/` and `uv-python/`, uv's package cache and the interpreter at
          `host_python_version`, which `uv run --offline` reads.
      Refresh these files whenever the pins move. With `--local-build`, the script prints that
      the clone is unchecked, because it makes neither GitHub read.

9. **The host-package archive signing key.** The `homelab_builder` role generated it once on
   daniel-box (H3), and the `builder` component has run the builder since H7.41b. The key sits in
   `homelab-signer`'s GnuPG home `/var/lib/homelab-signer/gnupg/` (mode 0700). Every host's apt
   trusts its public half. Nothing removes that home. The password manager holds the copy,
   which the operator exports once:
   `sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --armor
   --export-secret-keys`. Then stop the agent that the export starts, with
   `sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent`.
   Left running, it is another uid's process in the operator's login slice. The worktree
   tools cannot read such a process's cwd, so they treat every worktree as in use.
    - **To restore it** onto a rebuilt daniel-box, install `homelab-daniel-box` first (the
      bootstrap does). The package declares the two users and creates the directories. As
      `homelab-signer`, import the copy with
      `gpg --homedir /var/lib/homelab-signer/gnupg --batch --import`. The builder's next run
      writes `/etc/homelab-builder/archive-key.fpr` from the imported key through its
      `ExecStartPre=+` script, with no Ansible apply. The package does not generate a key. A
      rebuild with no copy starts with a fresh key from
      `gpg --homedir /var/lib/homelab-signer/gnupg --batch --pinentry-mode loopback
      --passphrase "" --quick-generate-key "homelab archive signing key <homelab-archive@daniel-box>"
      rsa4096 sign never`, run as `homelab-signer`, which re-pins every host as a rotation does.
    - **To rotate it**, follow *Rotating the archive key, or recovering from a revoked one*
      in `docs/host-packages.md`. A rotation re-pins every host.
    - **When the builder's own package is broken.** The builder builds `homelab-daniel-box`,
      which carries the builder, so a broken version can stop the builds that would repair
      it. First try the repository's previous version: `sudo apt-get install
      homelab-daniel-box=<previous>`, where `apt-cache madison homelab-daniel-box` lists the
      versions the pool keeps. If that fails, build one package by hand. Set the hold with
      `sudo touch /etc/homelab-builder/hold`, check out the fix from master in a scratch
      directory, and run the builder's two steps as `homelab-builder`: render with
      `/usr/lib/homelab/bin/uv run --frozen --python <host_python_version> python
      scripts/validate/host_packages.py render --tree . --out <out>`, then build with
      `HOMELAB_VERSION=<version> /usr/lib/homelab/bin/nfpm package --config nfpm.yaml --packager
      deb --target <dir>/` inside `<out>/homelab-daniel-box`. Install the file with `sudo apt
      install ./<file>.deb`, then release the hold with `sudo rm /etc/homelab-builder/hold`.
   Source: `hosts/components/builder/CLAUDE.md` and `docs/host-packages.md`.

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

11. **daniel-server's host age key.** `/etc/homelab/age.key` (root, mode 0600) is the key
    `homelab-secrets.service` decrypts `ansible/vars/hosts/daniel-server.sops.yaml` with. It
    is the same keypair as daniel-server's key in `~ubuntu/.config/sops/age/keys.txt`, copied
    rather than generated (H4.2), so one key lives at two paths on one host. Its public half
    is the first recipient of the generic rule in `ansible/.sops.yaml`. The recovery key
    (item 1) is a recipient of every file this key decrypts, so it is the way back when the
    host loses the key.
    - **To restore it** onto a rebuilt daniel-server, place the recovery key (item 1) at
      `~ubuntu/.config/sops/age/keys.txt`, as item 7 describes, and copy that file to
      `/etc/homelab/age.key` (root, mode 0600). Or let `sudo hosts/bootstrap-host.sh
      daniel-server` create a fresh key at the second path (its step 3) and print the public
      key. A fresh key then needs the recipient steps in the header of `ansible/.sops.yaml`,
      applied to every file that rotation list names, before the host decrypts anything.
    - **A lost or exposed key means rotating every value the host held**, not only re-keying
      the host. A re-key protects future revisions. Git history and every published
      package keep ciphertext encrypted to the old key. The rotation list is derived, not
      remembered: every value in every SOPS file whose creation rule in `ansible/.sops.yaml`
      lists the host's public key. To print the rules, take the public key from
      `age-keygen -y` on the surviving copy of the key, or from the first recipient of the
      generic rule, and run, from the repo root:
      `uv run python -c 'import sys, yaml; [print(r["path_regex"]) for r in yaml.safe_load(open("ansible/.sops.yaml"))["creation_rules"] if sys.argv[1] in r["age"]]' <public key>`.
      For daniel-server the rules are the cluster file, the cluster manifests,
      `vars/hosts/daniel-server.sops.yaml` and the generic rule, so the list is every value in
      `secrets.yml`, in the cluster file and manifests, and in its host file. Rotate through
      `docs/secret-rotation.md`, then `host_secrets.py sync daniel-server` (**operator-only**)
      to refresh the host file's copies.
   Source: `docs/target-architecture/host/h4-secrets-unit.md` ("Which ciphertext each host
   carries") and `hosts/bootstrap-host.sh` (step 3).
12. **daniel-pi's host age key.** `/etc/homelab/age.key` (root, mode 0600) on the Pi is the
    key `homelab-secrets.service` decrypts `ansible/vars/hosts/daniel-pi.sops.yaml` with. The
    Pi generated it at `hosts/bootstrap-host.sh` step 3 on 2026-10-24, so it exists at one
    path on one machine and nowhere else. The Pi is a recipient of its own file only. Its
    public half appears in the `vars/hosts/daniel-pi\.sops\.yaml$` creation rule of
    `ansible/.sops.yaml`, beside daniel-box's key and the recovery key (item 1), and in no
    other rule. The file arrived with H4.5. Since then the key decrypts the two push tokens that
    the Pi's heartbeats load from `/run/credstore`, so a lost key costs those two values.
    - **To restore it** onto a rebuilt Pi, run `sudo hosts/bootstrap-host.sh daniel-pi`. Its
      step 3 creates a fresh key at the same path and prints the public key. No copy of the
      old key exists to restore. Replace the Pi's public key in the Pi's creation rule. Then,
      on daniel-box or any host holding the recovery key (item 1), run
      `sops updatekeys vars/hosts/daniel-pi.sops.yaml` from `ansible/` and commit the result.
      The recovery key and daniel-box's key are recipients of the file, so either one
      decrypts it while the Pi has no working key.
    - **A lost SD card is a lost host key**, and a lost or exposed key means rotating every
      value the Pi's key decrypts. A re-key protects future revisions. Git history and every
      published package keep ciphertext encrypted to the old key. The rotation list is the
      one item 11 derives: every value in every SOPS file whose creation rule lists the Pi's
      public key. Take the public key from the rule, or from `age-keygen -y` on a surviving
      copy, and run item 11's command. For the Pi the only rule is
      `vars/hosts/daniel-pi.sops.yaml`, so the list is every value in that file, which holds
      `pi_sd_health_push_token` and `pi_recovery_push_token`. Rotate each through
      `docs/secret-rotation.md`, then `host_secrets.py sync daniel-pi` (**operator-only**)
      to refresh the host file's copies.
   Source: `docs/target-architecture/host/h4-secrets-unit.md` (H4.4, H4.5) and
   `hosts/bootstrap-host.sh` (step 3).

### The age keys

Each host holds up to two age keys, and they have different readers.

- **The host key, `/etc/homelab/age.key`.** `hosts/bootstrap-host.sh` (step 3) creates it on
  every host, and `homelab-secrets.service` decrypts the host's own SOPS file with it.
- **The operator's user-home key, `~/.config/sops/age/keys.txt`.** Ansible's `community.sops`
  lookups, the operator's `sops` edits, the `add-secret` skill and the secret rotation timer
  read it. They decrypt `ansible/vars/secrets.yml` and the cluster files.

The decision recorded with H7.24b on 2026-10-30:

- **daniel-box keeps its user-home key.** The deployer, `deploy.sh` and the rotation timer run
  there as the operator, and every one of them reads it.
- **daniel-server's user-home key is removed only after the operator confirms that nothing on
  daniel-server reads it.** H4.2 left it there as a duplicate. Its public key stays a
  recipient in `ansible/.sops.yaml` until the removal, and the removal deletes it from all
  three rules (`ansible/roles/setup/sops_setup/CLAUDE.md`, *Notable*). Both steps are
  **operator-only**.
- **daniel-pi holds no user-home key**, since 2026-10-03.
- **No playbook creates a user-home key.** The retired `sops_setup` role did. A new host that
  runs Ansible gets one by hand, as `ansible/README.md` §5 says.

## What to do first, by scenario

### Scenario: daniel-box is dead, daniel-server survives

daniel-server keeps its own SOPS key, so decryption still works without the recovery key.

1. Follow `docs/longhorn-disaster-recovery.md` → *Procedure (fresh host, total loss)*, step 1:
   bring up a replacement daniel-box with `ansible/bootstrap.yml`, add its pubkey to
   `ansible/.sops.yaml`, `sops updatekeys` from daniel-server, commit, pull.
2. Decide whether cluster **objects** (Deployments, Secrets, PVC bindings) are also lost —
   if so, restore etcd first per `docs/k3s-etcd-restore.md` (needs item 2, the cluster
   token, if this is a genuinely new host rather than the same disk).
3. Bring up the cluster and Flux by [Bootstrapping Flux on a rebuilt cluster](flux-deploy-plane.md#bootstrapping-flux-on-a-rebuilt-cluster) in `docs/flux-deploy-plane.md`.
   That section owns the order: k3s bring-up, the Flux install, the plumbing tags, the
   Longhorn restores of `docs/longhorn-disaster-recovery.md` (*Wait for the backupstore
   sync*, then *Restore volumes BEFORE the root exists*), and the root Kustomization last. The
   root provisions empty PVCs under the backed-up names, so it comes after the restores. No
   Ansible path deploys a workload on the cluster.
4. Verify with `probe.py targets` / `probe.py health <svc>` per that doc's *Verify* step.

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

1. Annotate both BackupTargets, so `kustomize-controller` skips them:
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

`k3s_server_config` (`hosts/components/k3s-server/component.yml`) holds the server's flags,
`kube-apiserver-arg` among them. daniel-box's host package renders it into
`/etc/rancher/k3s/config.yaml.d/50-homelab.yaml`, and `homelab-k3s-restart` restarts k3s once
inside its window when it changes. An argument the API server does not recognise makes it
refuse to start, and the k3s unit is `Type=notify` with `TimeoutStartSec=0` — so the restart
**hangs indefinitely** instead of failing, which is the same symptom the `node-ip` note in the
component describes for a different cause. The restart unit cannot recover it, because its
gates read the API that daniel-box alone serves.

1. Read `tail -n 200 /var/log/k3s.log` on daniel-box. A rejected flag names itself. (Not
   `journalctl -u k3s`: k3s's own lines are priority info, which the journald cap drops;
   the package's unit drop-in sends them to that file instead.)
2. **Operator-only:** revert the PR on master. Once the builder publishes the reverted
   package, install it with `sudo apt-get install homelab-daniel-box=<previous version>`, then
   run `sudo systemctl daemon-reload` and `sudo systemctl restart k3s`. `dpkg` puts the previous
   `50-homelab.yaml` back, or removes it when the previous version shipped none.
3. Verify as `docs/k3s-upgrade.md`'s *Verify the change, not just the workload* says, then
   `sudo rm /var/lib/homelab-k3s-restart/failed.sha256`, which the failed restart left behind.

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

### Scenario: GitHub, GHCR or Sigstore is unreachable

**Operator-only to run.** The normal deploy path needs GitHub, GHCR and Sigstore at once:
`promote` runs on GitHub, the artifact lives on GHCR, and the OCIRepository verifies keyless
against the `promote` identity. While any of the three is down, no local path writes the
cluster, because `deploy.sh` skips every flux-owned entry. This runbook publishes one artifact
to the in-cluster registry, signs it with the break-glass key, and points the OCIRepository at
it. The kind drill `.github/workflows/flux-deletion-drill.yml` runs cases `break-glass` and
`break-glass-red` against the flux role's own break-glass templates. It does not run the
steps below on production.

1. Clone item 8's repo bundle and commit the change to apply on top of it.
1. Commit the break-glass source form: set `flux_break_glass: true` in
   `ansible/roles/k8s/flux/defaults/main.yml`. The OCIRepository `homelab` then names the
   in-cluster registry (`flux_break_glass_url`), reads it over plain HTTP, and verifies against
   the Secret `flux-break-glass-key` instead of the keyless identity.
1. Check that the commit to render equals or descends from the commit that the cluster's
   OCIRepository status names in `org.opencontainers.image.revision`. Item 8's bundle is
   refreshed only with the age-key backup, so it can predate `prod`. A render from an older
   tree prunes every tag added since and reverts every change since. If the bundle fails the
   check, render from a surviving host checkout that holds that commit.
1. Render with `homelab-render cluster` in placeholder mode, as the `render` job in
   `.github/workflows/promote.yml` does.
1. On daniel-box, push the rendered bundle to the registry's loopback port with the pinned
   `flux` CLI, then sign the digest it prints with the kit's private half. Sigstore is
   unreachable by assumption, so the signature skips the transparency log:

   ```bash
   digest="$(flux push artifact oci://localhost:5000/homelab-manifests:break-glass \
     --path=out --source=break-glass --revision="break-glass@sha1:$(git rev-parse HEAD)" \
     --insecure-registry --output=json | jq -r .digest)"
   cosign sign --key break-glass.key --tlog-upload=false --use-signing-config=false \
     --allow-http-registry --yes "localhost:5000/homelab-manifests@$digest"
   ```

1. Apply the rendered OCIRepository `homelab` and the Secret `flux-break-glass-key` by hand.
   The bundle it serves carries the same source, so whichever Kustomization applies the
   OCIRepository keeps that source rather than reverting it.
1. Confirm `kubectl -n flux-system get ocirepository homelab` reads Ready with
   `SourceVerified=True`, and that every Kustomization reaches the new revision.

**What fires meanwhile.** `ProdMovedOutsidePromote` fires, because `prod`'s live digest no
longer matches `PROD_DIGEST`. That is expected. The break-glass path neither reads nor clears
`DEPLOY_HOLD`, which needs GitHub.

**A fresh or rebuilt cluster needs two more steps first.** It holds neither the in-cluster
registry nor the Flux controllers. Apply the rendered `cluster/registry/` directory before the
artifact push. Install the Flux controllers from the image archives that item 8's kit
carries, not from GHCR. Neither step is drilled (Open question 17 of the D6 plan), so the
drill covers a running cluster only.

**The way back to GHCR.** Re-pointing at GHCR first applies `prod`, which predates the
emergency change, and silently reverts it. So order the steps:

1. Push the break-glass commits to master with `flux_break_glass` back at `false`.
1. Wait until `promote` verifies a commit that contains them and moves `prod` there.
1. Apply `prod`'s OCIRepository by hand, as the flux role renders it with the lever off.
1. Confirm every Kustomization is Ready at `prod`'s origin revision and that
   `ProdMovedOutsidePromote` has cleared.

The registry's NetworkPolicy admits source-controller on the push port at all times, so
neither direction applies or deletes a policy by hand.
`ansible/roles/k8s/registry/templates/networkpolicy.yaml.j2` has the trade-off, in its
`DECIDED:` marker for that rule.

## Documented vs drilled

Never read a runbook's existence as proof it has been exercised. This table states only
what each source records — not what "should" have happened.

| Runbook | Drilled? | Evidence |
|---|---|---|
| k3s etcd restore (full, onto a replacement host) | **Partly, and ongoing.** The `--list-only` leg runs weekly by cron and has been verified since 2026-08-22. The full restore first passed 2026-09-11 — `offbox-daniel-box-1789094702.zip` restored in a throwaway guest on daniel-server and served 8 namespaces, 72 Deployments, 45 PVCs, 48 CRDs and 51 Secrets — and runs monthly since. Still not drilled: the restore onto a REPLACEMENT HOST, the agent rejoin and the Longhorn reattach. | `docs/k3s-etcd-restore.md`, *The full drill runs monthly in a throwaway guest* |
| Longhorn volume restore | **Yes, and ongoing.** First attempt 2026-08-15 failed on a B2 cap (not the data); retry 2026-08-16 passed (`traefik-acme`, ~21s, verified real data). Scheduled nightly since 2026-08-19, rotating one volume per night over the full backup set since 2026-08-20. | `docs/longhorn-disaster-recovery.md`: "Assurance gap (known, narrowing)" |
| SOPS decrypt with the recovery age key alone (no host key) | **No record found.** No drill of this specific path is recorded anywhere in the repo. | absence of any citation — see *Annual drill* below |
| SOPS onboarding (`age-keygen` → `sops updatekeys` → onboard) | **Yes, routinely** — the standard way every host (daniel-pi, whose key was removed on 2026-10-03, and daniel-box) was onboarded. Never specifically exercised as a *total-loss* recovery (starting from the recovery key rather than a host's own fresh key). | `ansible/README.md` §5; the `sops_setup` role created the key until H7.24b |
| Break-glass promotion (*GitHub, GHCR or Sigstore is unreachable*) | **Partly.** `flux-deletion-drill` verifies a bundle signed with a throwaway key through the flux role's break-glass source form on a kind cluster, and refuses one signed with another key. Not drilled: the runbook on production, source-controller's fetch through the registry's NetworkPolicy (kind enforces none), and the fresh-cluster steps. | `.github/workflows/flux-deletion-drill.yml`, cases `break-glass` and `break-glass-red` |
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
