# `hosts/components/builder` — the host-package builder and its apt repository (daniel-box)

The builder half of target-architecture slice H3, whose plan is
`docs/target-architecture/host/h3-pi-pilot.md`. It builds the host packages from master, signs
them, and serves the apt repository the hosts' updater reads. H7.41b moved it from the
`homelab_builder` role into daniel-box's package. The `failure-alert` component ships the page that
a failed run opens. This file holds the rules for the component; the plan's *The builder and the
repository (daniel-box)* holds the design.

## What the component installs

`component.yml` and the files beside it declare everything the role used to write:

- The system users `homelab-builder` (uid 995) and `homelab-signer` (uid 994), nologin, declared by
  uid alone. The install adopts the live accounts, and a rebuilt host gets groups whose gid equals
  the uid. Nothing reads the old gids 983 and 982.
- `tmpfiles.d/homelab-builder.conf`, which keeps the directories the units' `StateDirectory=` lines do
  not create: `src/` and `uv-cache/` (0700), `staged/` (owned by the builder, group
  `homelab-signer`, 2770, because the builder writes it and the signer empties it), the signer's
  `gnupg/` (0700) and `repo/` (0755), and `/etc/homelab-builder/`. The builder's first
  `ExecStartPre=+` line runs `systemd-tmpfiles --create` on it. Both units set
  `StateDirectoryMode=0711`: the signer traverses the builder's directory to reach `staged/`, as
  `http.server` traverses the signer's to reach `repo/`.
- `/usr/lib/homelab/builder/`, root-owned: `host_package_builder.py`, `homelab_tool_fetch.py` (the
  `homelab-tools` step below), `builder_ci_grade.py`, `builder_rollout_gate.py`,
  `homelab_repo_sign.py`, `host_lib.py`, `kuma_push.py` and `builder.conf`. The sources are in
  `hosts/lib/`. The directory sits outside the builder's `ReadWritePaths`, so a builder run cannot
  rewrite the code the next run executes. `builder.conf` holds the deployer's CI contexts, its walk
  bound, the repo and the Prometheus URLs, and no secret.
- `/usr/lib/homelab/publish-textfile` and `/usr/lib/homelab/builder-fingerprint`, both root-owned.
- `homelab-builder.service` and `homelab-builder.timer` (every 2 minutes),
  `homelab-repo-sign.service` and `homelab-apt-repo.service`. The units keep the names the Ansible
  copies had, so the `legacy:` entries name each file under `/etc/systemd/system`, and reconcile's
  shadow handoff removes them, runs one `daemon-reload`, and restarts each unit whose file differed.
  The package's own enable links live under `/usr/lib/systemd/system`, and the handoff never
  touches them.
- Two ufw rules, one per peer's `server_ip`, on port 8791. The rule admits the two peer hosts and
  every pod on daniel-server, because flannel masquerades a pod's traffic to an address outside the
  pod CIDR to that node's LAN IP. That reach is accepted: the repository serves only signed, public
  packages, and nothing at L3 tells the host apart from its pods. The Ansible rules carried a
  comment, which a ufw spec cannot hold, so the `legacy:` entries name it and `replaced_by` the
  declared rules. The arm relabels the live rules in place.

**The builder's token is a credential.** The unit's `LoadCredential=` line puts it in
`$CREDENTIALS_DIRECTORY/github_token`, and `homelab-secrets` writes the source file from the SOPS key
`homelab_builder_github_token` in daniel-box's host file. The builder reads that file ahead of
`builder.conf`. A missing credential refuses the run with `reason="token"`.

**The fingerprint file is written on every run.** The builder cannot read the signer's 0700 GnuPG
home, so `builder-fingerprint` runs as root from an `ExecStartPre=+` line, reads the key as
`homelab-signer`, and writes `/etc/homelab-builder/archive-key.fpr` at 0644 with M3's write rule. A
home with no signing key removes the file and exits 0, so the builder then refuses with
`reason="keyring"`. A home with two keys fails the run. A restored key therefore regenerates the file
on the builder's next run, with no Ansible apply.

**The three tools come from packages.** nfpm and uv are pinned in `hosts/packages/homelab-tools/`, and
`hosts/lib/tests/test_builder_component.py::test_the_nfpm_pin_is_ci_s_and_uv_matches_initial_setup`
holds nfpm equal to CI's pin and uv equal to `initial_setup_uv_version`. The unit's second
`ExecStartPre=` installs the pinned `host_python_version` into the state directory. Only
`ExecStart=` sets `UV_PYTHON_DOWNLOADS=never`, through `env`, because the install itself downloads.

**The package does not generate the signing key.** The Ansible role generated it once.
`docs/break-glass.md` item 9 holds the restore, and the rebuild procedure that generates a fresh key.

**A broken builder package.** The builder builds `homelab-daniel-box`, which carries the builder.
When a new version fails to build, the builder publishes nothing, the host keeps the installed
version, and the failure pages once. When a new version builds and installs but breaks the builder,
nothing new publishes either. The way out is `docs/break-glass.md` item 9's by-hand build, or
`apt-get install homelab-daniel-box=<previous>`.

The component reads two values other roles own. `ci_required_contexts` and `ci_ancestor_walk_max`
come from `ansible/inventory/group_vars/all.yml`. The Prometheus ClusterIP in `builder_prom_urls`
repeats `observability_prometheus_cluster_ip`, and
`hosts/lib/tests/test_builder_component.py::test_the_settings_carry_the_deployers_ci_contexts_and_two_prometheus_urls`
holds the two equal.

## The builder's run

Each run fetches master into its own bare clone, chooses the newest commit on the first-parent
chain since the last built one whose CI is green, renders every host package there with `uv
run --frozen`, checks the rendered keyring against `archive-key.fpr`, and builds each package
whose render changed with nfpm. It writes `staged/chosen` and the `.deb` files, all at one
version, `0.<first-parent count>+g<12-hex sha>`. A staged set the signer has not taken yet is
rebuilt at the new commit, because the signer refuses a `staged/` whose versions name two
commits.

`state.json`'s `staged` names the packages whose `.deb` may still be unsigned. `stage()` saves
it before it empties `staged/` or copies a render into `published/`, so a run that fails or is
killed in between leaves the next run a record that matches the disk. `published/` keeps each
render as a copy that preserves symlinks, and the comparison reads a symlink's target text: a
link that dangles inside its own render, such as the Pi package's
`timers.target.wants/homelab-updater.timer`, once made `copytree` raise after the copy and made
the comparison skip the link. The Pi package was recorded as published and never signed
(2026-10-23). The signer's own refusals are a separate path: a refused set moves to
`refused/`, and the signer's alert pages it.

The verdict reads every page of the commit's check runs, because a failing re-run on page 2
would otherwise grade a red commit green. A read short of the listing's `total_count` grades
`pending`, which is never cached (#5991).

The anchor is master plus a green CI verdict, and nothing more. The `# DECIDED:` marker in the
script cites ADR-0021:
`hosts/lib/host_package_builder.py:DECIDED: the anchor is a commit`.

Every run past the fetch writes `homelab_builder.prom`, and the unit's `ExecStopPost=-+` line
copies it into the textfile directory. The `reason` label of
`homelab_builder_unbuilt_since_timestamp_seconds` is one of `ok`, `hold`, `ancestry`,
`keyring`, `ci`, `no-green`, `token`, `build`, `tools` and `canary`. `token` is a missing token, or a 401 or
403 from the check-runs read, which the anonymous fetch cannot see. `build` is a render or
nfpm failure at the chosen commit, a value the plan's list does not name. `tools` is a
`homelab-tools` pin that could not be fetched or whose sha256 differs from the pin (below). A worktree cleanup
that fails after a refusal is logged, and the run keeps the refusal's reason. A failing run exits 1 once per reason and tip,
then 0 on a repeat, so `OnFailure=` pages once for each condition. A refusal that came from an
exception writes one stderr line, which systemd puts in the journal: the reason, the exception's type
and message, its cause and the call chain that raised it (#6074). The journal keeps that line
only because both units set `SyslogLevel=notice`: initial_setup's journald drop-in stores notice
and above, and a unit's output defaults to info (#6127). ENFORCED:
`hosts/lib/tests/test_builder_component.py::test_both_units_keep_a_state_directory_at_0711_and_log_at_notice`.
A failed fetch exits 0 and writes nothing: the fetch timestamp then ages, which H3.5c's fetch-age arm pages on.

## The `homelab-tools` step (H3.11)

The render of `hosts/packages/homelab-tools/` holds pins and no binaries: one tree per
architecture, each with an `nfpm.yaml` and a `tools.json`. For each such tree the builder
copies it into `work/`, downloads every pinned file into `tools-cache/<sha256>`, checks the
digest, places each binary under the copy's `root/usr/lib/homelab/bin/`, and runs nfpm there.
A tool that ships as a `.tar.gz` names the file to install, and only that member is read.
The builder fetches and verifies every pin of every changed tree before the first nfpm run,
and before it empties `staged/`, so a digest mismatch builds and stages nothing and writes
`reason="tools"`. A cached file is hashed again on every use. After a build, the cache keeps
only the digests the render pins. `out/` and `published/` never hold the binaries, so
publish-on-change compares the pins.

The code is `hosts/lib/homelab_tool_fetch.py:install_tools`.
CI's `host_packages_build` job calls the same function through `host_packages.py fetch-tools`.
`ansible/roles/setup/homelab_builder/tests/test_host_package_builder_tools.py::test_a_binary_whose_digest_differs_from_its_pin_builds_nothing_and_reads_tools`
holds the refusal.

## The rollout gate (H3.12)

A run whose changed set holds more than one host package (`hosts/<host>/`) stages only the
canary's and defers the rest. The canary is the first of daniel-pi, daniel-server and daniel-box
whose package changed. The shared packages under `packages/` never wait. The run records the gate
`{host, sha}` and the deferred directories in `state.json`, and moves the gate to each newer
canary version it stages. While the gate is open the builder builds no other host package, and
the unbuilt-since series reads `reason="canary"` with the gate's commit as `sha`. A hold or a
failure keeps its own reason.

The gate closes at the start of a run, after the hold check, in one of two ways. The operator's
root-owned `/etc/homelab-builder/release` can name the gate's commit or a descendant, and then
Prometheus is not asked. Otherwise the builder reads `homelab_host_applied_info` and
`homelab_host_reconcile_ok` for the canary from the addresses in the settings file's
`PROMETHEUS_URLS`, loopback first and then the pinned ClusterIP. It closes the gate when the
applied SHA equals or descends from the gate's commit with reconcile at 1. Any other answer, an
unreadable file or an unreachable Prometheus leaves the gate open. After the gate closes with
nothing new on master, the next run rebuilds the deferred packages at the last built commit.

`PROMETHEUS_URLS` renders from `builder_prom_urls` in `component.yml`. The component cannot read
the observability role's `observability_prometheus_cluster_ip`, so the default repeats the address,
and `hosts/lib/tests/test_builder_component.py::test_the_settings_carry_the_deployers_ci_contexts_and_two_prometheus_urls`
holds the two equal.  The code is
`hosts/lib/builder_rollout_gate.py`, and `docs/host-packages.md`
has the override's runbook step.

## The signer and the publish (H3.5b)

`homelab-repo-sign.service` runs `hosts/lib/homelab_repo_sign.py` as `homelab-signer`. It has no
timer: the builder unit's `OnSuccess=` starts it after every builder run that exits 0, a held
run included. The builder unit also carries `After=homelab-repo-sign.service`, so a builder
start waits for a signer still running and never rewrites `staged/` under it.

- **The refusal rule.** The signer reads every `staged/` entry without following a symlink,
  and checks all of them before it copies anything. It accepts `chosen`, holding a 12-hex SHA,
  and `.deb` files whose `Package` starts with `homelab-` and whose `Version` ends in `+g` and
  that SHA, in the builder's `0.<count>+g<sha>` form exactly. One package may appear once per
  architecture (`homelab-tools` stages an `amd64` and an `arm64` build), and twice for one
  architecture is a refusal. Anything else is a refusal: the signer copies the set into
  a `refused/<UTC time>/` directory under the signer's state directory, empties `staged/`, publishes nothing and exits
  1. It names each pool file from the package's control fields, never from the builder's file
  name. A copy, never a rename, because `staged/` and the signer's directory are separate
  `ReadWritePaths=` bind mounts.
- **The publish.** `apt-ftparchive` writes the `amd64` and `arm64` `Packages` indexes and a
  `Release` whose `Valid-Until` is seven days after its `Date`. gpg signs `InRelease` and
  `Release.gpg` with the key whose fingerprint `archive-key.fpr` names. The suite is built in a
  fresh directory under `repo/.snapshots/`, and one rename of a symlink points `repo/dists` at
  it. Every published file is 0644 and every directory 0755, because
  `homelab-apt-repo.service` is a `DynamicUser` that reads through the other bits.
- **The daily re-sign.** With nothing staged, the signer re-signs the unchanged tree once the
  published `Release`'s `Date` is 24 hours old, or once `archive-key.fpr` names a key other
  than the one that signed it.
- **The hold.** With `/etc/homelab-builder/hold` present, the builder builds nothing and the
  signer leaves `staged/` as it is. The re-sign still runs, so a long hold does not let
  `Valid-Until` lapse on every host.
- **The metric.** `homelab_repo.prom` carries `homelab_repo_published_info{package,sha,version}
  1` for the newest published version of each package, read from the published indexes. The
  unit's `ExecStopPost=-+` line copies it through `publish-textfile`.
- **The interpreter.** The unit runs the host's `/usr/bin/python3`, because the signer cannot
  read the builder's pinned interpreter under its 0700 state directory. The script is
  stdlib-only, and `pyproject.toml` formats it for 3.12.
  `ansible/roles/setup/homelab_builder/tests/test_repo_signer.py::test_the_signer_parses_with_the_3_12_grammar`
  holds it to that grammar. The signer takes its `log` and its atomic write from the `host_lib.py`
  installed beside it, so that file and the `kuma_push.py` it imports are formatted for 3.12 too,
  and `hosts/lib/tests/test_host_lib.py::test_host_lib_and_kuma_push_parse_with_the_3_12_grammar`
  holds them to it (#6202).
- **The sandbox.** The plan's *Sandboxing* list plus `PrivateNetwork=yes`, writing only
  `/var/lib/homelab-signer` and `staged/`.

The signer opens `staged/` once with `O_DIRECTORY|O_NOFOLLOW` and reaches every entry through
that descriptor, because the builder owns the directory `staged/` sits in. A `staged/` that is
not a plain directory fails the run with reason `staged` and touches nothing.

A failure other than a refusal exits 1 once per reason (`key`, `index`, `sign`, `staged`, or
`io` for a filesystem or tool error), then 0 on a repeat, so
`OnFailure=homelab-alert@homelab-repo-sign.service` pages once. A refusal pages once per refused set.
A failure keeps `staged/`, and the next successful builder run starts the signer again. A
publish that fails before its index goes live removes the pool files it added. Once the
`dists` swap has happened, the live index lists those files, so a later failure keeps them.
`docs/host-packages.md` has each page's clear.

## Autonomous-role contract (the builder and the signer publish host packages with no human in the loop)

Every 2 minutes, `homelab-builder.timer` may build the host packages from a master commit and
replace the contents of `staged/`. After each such run, `homelab-repo-sign.service` may sign
and publish what was staged, so every host's updater installs it at its next fire.

- **Scope / exclusions:** the builder writes `/var/lib/homelab-builder/` alone, which its
  unit's `ReadWritePaths=` enforces, plus its metric through `publish-textfile`. It **never**
  signs, publishes or touches `/var/lib/homelab-signer/`, and never builds a commit off
  master's first-parent chain or one whose CI is not green. The signer writes
  `/var/lib/homelab-signer/` and empties `staged/`, with no network. It **never** publishes a
  package outside the refusal rule, and never runs code from the builder's clone.
- **Mode (explicit + reversible):** the timer's enable link arms the builder. `sudo systemctl mask
  --now homelab-builder.timer` pauses it, and `unmask` then `start` resumes it. Dropping the link
  from a package version stops the timer on the install that drops it.
- **Authoritative sources:** `origin/master` as fetched this run, GitHub's check-runs API
  graded over `CI_CONTEXTS`, and `archive-key.fpr`. The builder reads nothing from its clone
  to decide what to trust. The signer trusts only `staged/chosen` and the control fields of
  the packages it checks, and signs with the key `archive-key.fpr` names.
- **Abort valves:** an empty `CI_CONTEXTS` or a walk bound that is not a positive integer
  (`ci`), a tip that does not descend from the last built commit (`ancestry`), and a keyring
  that differs from `archive-key.fpr` (`keyring`). Each builds nothing. An open rollout gate
  (`canary`) builds no host package but the canary's. The signer's refusal
  rule publishes nothing from a set it refuses. The operator's root-owned
  `/etc/homelab-builder/hold` stops both from publishing anything new, and a
  masked timer stops both.
- **Required evidence:** `homelab_builder.prom` on every run past the fetch,
  `homelab_repo.prom` on every publish, a refused set kept under
  the signer's `refused/` directory, and one Discord page per new failure of either unit
  through `OnFailure=`.
- **Next-run review:** before widening scope, read `journalctl -u homelab-builder -u
  homelab-repo-sign` for the refusals since the last change, and the builder metric's `reason`
  label.
