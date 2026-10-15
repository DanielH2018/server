# Host packages — the operator runbook

The host-package pipeline builds `homelab-*` Debian packages from master on daniel-box, signs
them, and serves them as an apt repository at `http://<daniel-box server_ip>:8791`. The
Ansible role `homelab_builder` installs the builder half on daniel-box; its rules are in
`ansible/roles/setup/homelab_builder/CLAUDE.md`, and its design is in
[the H3 plan](target-architecture/host/h3-pi-pilot.md).

This page holds the procedures an operator runs when the pipeline stops or must be stopped:

- clearing a deployer hold that a failed apply of the role wrote;
- the two host-plane pages, and clearing a host's hold with either lever;
- the three stop levers: a host's timer, the builder hold, and the disarm;
- rolling a host back;
- rotating the archive key, or recovering from a revoked one;
- clearing a deployer park that the role's `initial_setup.yml` entry caused;
- the signer's pages, and the five builder pages.

## How a package reaches a host

1. `homelab-builder.timer` runs `homelab-builder.service` every 2 minutes. The builder fetches
   master, picks the newest green first-parent commit since the last one it built, renders and
   builds every package whose render changed, and writes them to
   `/var/lib/homelab-builder/staged/` with `staged/chosen`, which names that commit.
2. Every builder run that exits 0 starts `homelab-repo-sign.service` through `OnSuccess=`. The
   signer checks every staged entry, copies the packages into
   `/var/lib/homelab-signer/repo/pool/main/`, and regenerates and signs the indexes. The
   signed `Release` carries `Valid-Until` seven days after its `Date`, and the signer re-signs
   the unchanged tree once its `Date` is 24 hours old.
3. `homelab-apt-repo.service` serves `/var/lib/homelab-signer/repo/` on port 8791. Each host's
   `homelab-updater.timer` installs the newest versions and runs reconcile.

The builder's state is `/var/lib/homelab-builder/state.json`. Its `last_built` key names the
commit it last built. `/var/lib/homelab-builder/published/` holds the render of each package as
it was last staged. The signer's state is `/var/lib/homelab-signer/state.json`, and a refused
set lands in `/var/lib/homelab-signer/refused/<UTC time>/`. Both services log to the journal:
`journalctl -u homelab-builder -u homelab-repo-sign`.

## Clearing a deployer hold that a failed `homelab_builder` or `gitops_deploy` apply wrote

The GitOps tick applies `homelab_builder` on daniel-box through the setup plane. From PR H3.5a
the same tick applies `gitops_deploy` as well. Every broad arm of the deployer is
forward-only, so a failed apply leaves the tree merged and writes two records:

- `hold_sha` in `/var/lib/gitops-deploy/`, which names the SHA that failed;
- one `hold_plane` line in `/var/lib/gitops-deploy/owed.jsonl` for each playbook and tag set
  that failed.

While the hold stands, `land.sh` reports `deploy-failed` for every session's landing, and
**GitOps Deploy — Status** pages. Each apply that can fail this way merges in an attended
window for that reason.

To clear the hold:

1. **Merge a fix, or the disarm, as its own PR.** The disarm is
   `homelab_builder_enabled: false` in `ansible/roles/setup/homelab_builder/defaults/main.yml`.
   The tick applies the role. An apply that covers every held plane clears `hold_sha` with no
   further step.
2. **If that apply fails too, list what the hold names.** On daniel-box, read the held SHA
   and every `hold_plane` line:

    ```bash
    cat /var/lib/gitops-deploy/hold_sha
    grep '"hold_plane"' /var/lib/gitops-deploy/owed.jsonl
    ```

    The list can name `homelab_builder`, `gitops_deploy` or both.
3. **Operator-only: apply each named plane by hand.** The play's `load_secrets` `pre_task`
   decrypts, so an agent session cannot run this step. From the primary checkout on
   daniel-box:

    ```bash
    uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder,gitops_deploy
    ```

    Name only the tags the hold lists.
4. **Clear the hold.** A hand `ansible-playbook` run is not the deployer, so it clears
   nothing on its own. Once every held plane is applied, run:

    ```bash
    uv run python scripts/deploy_tools/gitops_state.py clear-hold <full hold_sha>
    ```

    The command refuses unless the SHA is the one held. It removes `hold_sha` and every
    `hold_plane` line together. [The GitOps pipeline](gitops-pipeline.md)'s *Which apply
    clears a hold* states the coverage rule.

Never `rm` `hold_sha`. That leaves its `hold_plane` lines in the ledger with nothing that
names them, and `clear-hold --orphaned` is then the only way to drop them.

## Host-plane pages

Two Alertmanager rules in observability's `host-checks` group page on a host's updater. Each
carries `severity="critical"` and the host's name as `service`, and the alert's description
names what to read first.

- **`HostReconcileFailed`** fires when `homelab_host_reconcile_ok` has read 0 on a host for 15
  minutes. The updater writes 0 on every run while the host holds after a failed reconcile,
  so the host installs nothing newer until the hold clears. Read `journalctl -u
  homelab-updater -u homelab-reconcile` on the host and fix the cause. Then clear the hold
  with one of the updater's two levers, run as root on the host:
    - `/usr/lib/homelab/homelab-updater --clear-hold` reruns reconcile against the installed
      packages and clears the hold if it passes;
    - `/usr/lib/homelab/homelab-updater --clear-hold --install-newest` clears the hold, then
      installs the newest packages and reconciles as a timer run does. Use it when the fix is
      a newer package.
- **`HostUpdateStale`** fires when a host listed in
  `ansible/roles/k8s/observability/defaults/main.yml:observability_expected_hosts` reports no
  successful update within its `limit_seconds`. It also fires when the host's exporter is up
  and serves no success timestamp. A failed fetch writes no metric, so an unreachable
  repository, a stopped `homelab-apt-repo.service`, a firewall rule, a keyring mismatch and a
  stopped timer all end here. Read `systemctl status homelab-updater.timer` and `journalctl -u
  homelab-updater` on the host. A dead exporter does not fire it: that host's `up` at 0 keeps
  the rule quiet.

Each limit exceeds the longest gap between two of the host's updater fires plus the updater's
`TimeoutStartSec`, so one failed fetch never pages.
`scripts/renderer/tests/test_renderer_host_update_limits.py` fails a limit the host's rendered
schedule outgrows. The map is empty until H3.9c adds daniel-pi.

## Clearing a host's hold

A failed reconcile makes a host's updater write `/var/lib/homelab-updater/hold`, and
`HostReconcileFailed` pages 15 minutes later. Each lever runs as root on the held host, from a
LAN session:

1. Read `journalctl -u homelab-updater -u homelab-reconcile` and fix the cause.
1. When the installed package is fine and the failure was transient, rerun reconcile. The
   command clears the hold only if reconcile passes:

    ```bash
    sudo /usr/lib/homelab/homelab-updater --clear-hold
    ```

1. When the installed package itself makes reconcile fail, wait until the builder has
   published the fix or the revert. Then clear the hold and install the newest versions:

    ```bash
    sudo /usr/lib/homelab/homelab-updater --clear-hold --install-newest
    ```

    A failure writes the hold again. With nothing newer published, this acts as
    `--clear-hold`.

## Stopping updates on one host

To stop one host installing anything, mask its updater timer on that host:

```bash
sudo systemctl mask --now homelab-updater.timer
```

Reconcile never starts or restarts a masked unit, so no later package undoes the mask.
`HostUpdateStale` fires for the host once its limit passes, which is expected. To resume:

```bash
sudo systemctl unmask homelab-updater.timer
sudo systemctl start homelab-updater.timer
```

## Stopping every publish: the builder hold

To stop the pipeline publishing anything to any host, create the hold on daniel-box:

```bash
sudo touch /etc/homelab-builder/hold
```

`sudo touch` makes the file root's, and neither service user can remove it. While it exists:

- the builder fetches and builds nothing. Its unbuilt-since series reads `reason="hold"`, so
  the metric still shows how far master has moved past the builder;
- the signer leaves `staged/` as it is and publishes nothing new. It still re-signs the
  published tree once its `Date` is 24 hours old, so a long hold never lets `Valid-Until`
  lapse on every host.

To confirm the hold took, read `journalctl -u homelab-repo-sign -n 5`. The newest run logs
`held by /etc/homelab-builder/hold`. To release the hold:

```bash
sudo rm /etc/homelab-builder/hold
```

The next builder run builds the newest green commit and stages again any set the hold kept.
The signer then publishes it. H3.5c's lag arm pages once a hold has stood for 2 hours.

To stop the pipeline for longer than an incident, disarm it instead. Set
`homelab_builder_enabled: false` in `ansible/roles/setup/homelab_builder/defaults/main.yml` and
merge that. The tick applies the teardown, which removes the units, the code, the settings and
`/var/lib/homelab-builder/`. It keeps the signer's GnuPG home and the published tree. The
repository is unreachable while disarmed, so each host's updater fails its fetch, and
`HostUpdateStale` fires once the host's limit passes.

## Rolling a host back

The usual rollback is a revert on master. A version counts first-parent commits, so the
builder builds the reverted tree at a higher version, and each host installs it at its next
updater fire. A held host needs `--clear-hold --install-newest` once the revert is published.

To put one host on an older version at once, use the copy the pool keeps. The signer never
deletes a published `.deb`. On the host, from a LAN session:

1. Stop updates, or the next fire reinstalls the newest version:

    ```bash
    sudo systemctl mask --now homelab-updater.timer
    ```

1. List the versions the repository serves, and install the one wanted. The package's `postinst`
   runs reconcile against the older version.

    ```bash
    apt-cache policy homelab-<host>
    sudo apt-get install --allow-downgrades homelab-<host>=<version>
    ```

1. Once master carries a fix, unmask the timer as *Stopping updates on one host* describes.

## Rotating the archive key, or recovering from a revoked one

`Valid-Until` bounds how long a host accepts an old index signed by a working key. It does not
help once the key itself is revoked, because whoever holds the revoked key can sign a fresh
`Release`. A planned rotation follows the same steps, because every host must re-pin the new
key. Every step is **operator-only**, and each host step runs from a LAN session.

1. On each host, stop updates, so no host installs anything while it still trusts the old key:

    ```bash
    sudo systemctl mask --now homelab-updater.timer
    ```

    `HostUpdateStale` fires for each masked host once its limit passes, which is expected.
1. On daniel-box, set the builder hold (`sudo touch /etc/homelab-builder/hold`). Then generate
   the replacement key in the signer's GnuPG home, as the role's install arm does:

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --batch \
      --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "homelab archive signing key <homelab-archive@daniel-box>" \
      rsa4096 sign never
    ```

1. In a worktree, export the new key's public half over the committed keyring. Take its
   fingerprint from `--list-keys`.

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg \
      --export <new fpr> > hosts/keys/homelab-archive-keyring.gpg
    ```

    In the same commit, update the fingerprint pin in `hosts/bootstrap-host.sh`, which PR
    H3.6 adds. Copy the new private key into the break-glass kit, as
    [the break-glass doc](break-glass.md) describes.
1. Delete the old key from the signer's home, because the role's apply refuses a home that
   holds more than one key. Then stop the agent the commands started:

    ```bash
    sudo -u homelab-signer gpg --homedir /var/lib/homelab-signer/gnupg --batch --yes \
      --delete-secret-and-public-key <old fpr>
    sudo -u homelab-signer gpgconf --homedir /var/lib/homelab-signer/gnupg --kill gpg-agent
    ```

    Until the apply in the step after next, the signer has no key to sign with. A re-sign
    that falls due before then fails, and pages once with `sign`.
1. Merge the PR. The tick applies nothing for a change under `hosts/`.
1. Rewrite `/etc/homelab-builder/archive-key.fpr` with a hand apply of the role. Run it from
   the primary checkout on daniel-box once that checkout holds the merge:

    ```bash
    uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder
    ```

1. Release the builder hold (`sudo rm /etc/homelab-builder/hold`). The builder's keyring check
   now accepts the render, so the builder publishes a `homelab-updater` that ships the new
   keyring. The signer's recorded key no longer matches `archive-key.fpr`, so it re-signs the
   whole tree with the new key.
1. On each host, run `sudo ./bootstrap-host.sh <hostname>` from the commit that carries the
   new pin. Its key check verifies the keyring against that pin. It then installs the newest
   packages, whose `homelab-updater` ships the same keyring. Then unmask and start the timer:

    ```bash
    sudo systemctl unmask homelab-updater.timer
    sudo systemctl start homelab-updater.timer
    ```

A host whose `HostUpdateStale` still fires after the last step has not recovered.

## Clearing a park caused by the role's `initial_setup.yml` entry

`initial_setup.yml` is a bring-up playbook, so adding or removing its `homelab_builder` entry
parks the GitOps deployer. The clear is **operator-only**, because the play's `load_secrets`
`pre_task` decrypts. On daniel-box, in the primary checkout:

1. Check the range the park holds:

    ```bash
    git log --oneline HEAD..origin/master
    git diff --name-only HEAD..origin/master
    ```

    Go on only when the range holds the entry's PR alone. Otherwise list each other change
    with the apply it owes, and run each after the last step.
1. `git merge --ff-only origin/master`.
1. `uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder`.

## The signer's page

`homelab-repo-sign.service` pages once per new failure through
`homelab-repo-sign-alert.service`. A repeat of the same failure exits 0, so the page does not
repeat every 2 minutes. Read `journalctl -u homelab-repo-sign` for the reason:

- **`refused staged/`.** A staged entry broke the refusal rule. It was a package not named
  `homelab-*`, a version that names a commit other than `staged/chosen`'s, a stray file, a
  symlink, or a malformed `chosen`. The signer published nothing, emptied `staged/`, and kept
  the set under `/var/lib/homelab-signer/refused/<UTC time>/`. Treat it as a builder defect,
  or as a render that tried to get something signed. Read the refused set and the builder's
  journal for that run before anything else. Once master carries the fix, reset the builder's
  published record, below, so the builder rebuilds the refused packages.
- **`key`.** `/etc/homelab-builder/archive-key.fpr` is missing or malformed. A hand apply of
  the role rewrites it.
- **`sign`.** `gpg` could not sign with the key `archive-key.fpr` names, usually because the
  signer's home no longer holds it. Compare `sudo -u homelab-signer gpg --homedir
  /var/lib/homelab-signer/gnupg --list-keys` with the file. The break-glass doc restores the
  key.
- **`index`.** `apt-ftparchive` failed. The journal line carries its stderr.

A signer failure other than a refusal keeps `staged/`, and the next successful builder run
starts the signer again.

## The builder's pages

The builder pages once per new failure through `homelab-builder-alert.service`. H3.5c's arms
page on a condition that persists. The `reason` label of
`homelab_builder_unbuilt_since_timestamp_seconds` names the case. Every procedure below that
edits the builder's state stops its timer first, so no run overwrites the edit:

```bash
sudo systemctl stop homelab-builder.timer
systemctl is-active homelab-builder.service   # repeat until it prints inactive
```

Start the timer again with `sudo systemctl start homelab-builder.timer`.

### `reason="ancestry"`: master was rewritten

The fetched tip does not descend by first parent from `last_built`, so the builder builds
nothing. Once the rewrite is understood and accepted, drop `last_built`, so the next run walks
from the new tip. The edit keeps the file's owner:

```bash
sudo python3 -c 'import json, pathlib; p = pathlib.Path("/var/lib/homelab-builder/state.json"); s = json.loads(p.read_text()); s.pop("last_built", None); s.pop("failure", None); p.write_text(json.dumps(s, indent=2, sort_keys=True))'
```

The next run builds the newest green commit within the walk bound. A version counts the
chosen commit's first-parent commits. A rewrite that shortened master therefore publishes
versions lower than the ones hosts hold, and hosts keep the newer ones until master's count
passes them. Compare `git rev-list --count --first-parent origin/master` with the published
versions before you start the timer.

### Resetting the published record after a signer refusal

The builder records each package's render under `/var/lib/homelab-builder/published/` when it
stages the package, before the signer accepts it. After a refusal those renders read as
published, so the builder does not rebuild them until their render changes again. With the
timer stopped, remove the record:

```bash
sudo rm -rf /var/lib/homelab-builder/published
```

Then drop `last_built` with the command in the `ancestry` section, so the next run rebuilds
every package. A package whose render did not change is published with the same content at the
new version, which costs each host one install.

### `reason="no-green"` that persists: a renamed CI job

The builder grades each commit over `CI_CONTEXTS`, which the role renders from
`gitops_deploy_ci_contexts`. When a CI job is renamed, no commit carries the old context, so no
commit is ever green. Change `gitops_deploy_ci_contexts` in
`ansible/roles/setup/gitops_deploy/defaults/main.yml` to the new name, spelled exactly as
`ci.yml`'s `name:`, and merge that. The tick re-applies both `gitops_deploy` and
`homelab_builder`, because that defaults file is a cross-role row naming this role.

### `reason="keyring"`

The keyring in `homelab-updater`'s render is absent, or its fingerprint differs from
`/etc/homelab-builder/archive-key.fpr`. The builder stages nothing, so no wrong keyring reaches
a host. Compare the fingerprint of the committed keyring with the file on daniel-box:

```bash
gpg --show-keys --with-colons hosts/keys/homelab-archive-keyring.gpg | grep '^fpr' | head -1
cat /etc/homelab-builder/archive-key.fpr
```

Restore a reverted keyring on master. Finish a key rotated on one side only with the rotation
procedure above. The first builder run after the fix builds.

### `reason="ci"`

The builder refuses to grade anything, because `CI_CONTEXTS` is empty or
`CI_ANCESTOR_WALK_MAX` is not a positive integer. Restore `gitops_deploy_ci_contexts` or
`gitops_deploy_ci_ancestor_walk_max` in `ansible/roles/setup/gitops_deploy/defaults/main.yml`
and merge it. The tick re-applies `homelab_builder`, which renders the builder's settings
again. If the settings file on the host was edited or lost, re-apply the role by hand instead
(**operator-only**):

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags homelab_builder
```

### `reason="token"` and `reason="build"`

`token` means the settings carry no `GITHUB_TOKEN`, or the check-runs API refused the token with
401 or 403. Replace `homelab_builder_github_token` with the `add-secret` skill and re-apply the
role. `build` is a render or an `nfpm` run that failed at the chosen commit. Its journal line
names the package, and the fix lands on master.
