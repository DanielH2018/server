# H4 — Secrets unit; NUT on daniel-server first

Status: IN PROGRESS
Depends on: H3
Unblocks: H5, H6

PR H4.1a1, the secrets script: decrypt and render: [#6056](https://github.com/DanielH2018/server/pull/6056).
PR H4.1a2, the secrets script: the restart queue: [#6059](https://github.com/DanielH2018/server/pull/6059).
PR H4.1a3, the host renderer's declared outputs and compose files: [#6060](https://github.com/DanielH2018/server/pull/6060).
PR H4.1d, the per-host SOPS files' tool: [#6058](https://github.com/DanielH2018/server/pull/6058). It adds the ADR-0022 amendment, which the operator accepted on 2026-10-23. It also corrects this plan: `domain` left SOPS in #5087, so no host file carries it.
PR H4.3b2, one reader for a role's leading `end_role` guard: [#6057](https://github.com/DanielH2018/server/pull/6057), merged 2026-10-23, `nothing-to-deploy`. It added `setup_gates.role_end_guard` and `role_end_hosts`, and the census, `land_reach.setup_role_hosts` and `glance_facts.setup_glance_lines` now read the guard. The live-tree outputs were byte-identical before and after.

This slice adds `homelab-secrets.service`, which decrypts a per-host SOPS file on the host
with the host's own age key. It proves the unit on the UPS shutdown chain of daniel-server,
the host whose config carries the most consequence per line. It then retires the Pi's interim
credential files from H3. Names and paths follow H3's *Shared names for the host slices*,
and every handoff follows H3's *host handoff rule*. Where this slice changes one of H3's
mechanisms, the *Open questions* name the change.

## Goal and exit criterion

Afterwards, `homelab-daniel-server` exists, and its NUT secondary is package-owned:

- `nut-monitor` reads `upsmon.conf` from `/var/lib/homelab-secrets/etc/nut/`. A package
  drop-in points upsmon there with `NUT_CONFPATH`, and `homelab-secrets` renders the file
  from ciphertext the package carries.
- The UPS watchdog runs as a package timer. It pushes daniel-server's own Kuma tile with
  `ups_secondary_daniel_server_push_token`, and renders `domain` into the script from inventory.
- daniel-box keeps its Ansible-owned secondary, cron and token until H7.8.

The Pi's two token heartbeats read their two push tokens from `/run/credstore`.
`homelab-secrets` decrypts them from a Pi-only SOPS file. Both scripts render `domain` from
inventory (H3.8b), so it is in no host file.

The exit criterion has five parts, all observed together:

1. On daniel-server, `upsc apc-ups@127.0.0.1 ups.status` returns a status, and
   `systemctl is-active nut-monitor` prints `active`. `upsc -c apc-ups@127.0.0.1` also lists
   the address that `ip -4 -o addr show cni0` prints on daniel-server.
    - `man upsc` says `-c` lists each client connected on the UPS. upsmon attaches to a UPS
      with `LOGIN` after sending its credentials, so the entry is evidence that
      daniel-server's upsmon logged in with credentials that match the rendered ones. A CI
      container case (*Verification*) proves that a wrong password keeps a client off the
      list.
    - The entry alone does not show which file upsmon read. `/etc/nut/upsmon.conf` holds the
      same credentials as the packaged file (H4.3a), so an upsmon that ignored
      `NUT_CONFPATH` would log in too. An **operator-only** read therefore shows that the
      running upsmon carries the variable. `systemctl show -p MainPID --value nut-monitor`
      prints the PID, and
      `sudo cat /proc/<pid>/environ | tr '\0' '\n' | grep -c -x 'NUT_CONFPATH=/var/lib/homelab-secrets/etc/nut'`
      prints `1`. The read prints a count, never the environment. It needs root because the
      process runs as root. The CI container case proves that upsmon honours the variable.
    - On 2026-10-11, `upsc -c` against the `nut` ClusterIP from daniel-box listed three
      clients: `10.42.0.0` (daniel-box's `flannel.1`), `127.0.0.1` (the pod's own primary)
      and `10.42.1.1`. That last address is inferred to be daniel-server's hostPort path,
      which reaches upsd from the node's `cni0` side. The check derives the address on the
      host rather than trusting this reading.
2. The watchdog's push arrives. The Kuma tile `UPS Secondary (daniel-server)` reads UP, fed by
   `homelab-ups-watchdog.timer`, and daniel-server has no `ups-secondary-health` cron file.
   `UPS Secondary (daniel-box)` stays UP, fed by daniel-box's Ansible cron.
3. `stat` prints `640 root nut` for `/var/lib/homelab-secrets/etc/nut/upsmon.conf` and
   `755 root root` for each directory above it. An **operator-only**
   `sudo find /run/credstore -exec stat -c '%a %U %n' {} +` on daniel-server and on the Pi
   prints `700 root` for `/run/credstore` and `600 root` for every file in it. The step needs
   root because the directory is root 0700. A `sudo stat /run/credstore/*` would not work,
   because the unprivileged shell expands the glob and cannot list the directory.
4. The `host_packages` job that H3 adds to `.github/workflows/ci.yml` runs gitleaks over
   every package root in its named set, and its red-proof fixture fails.
5. After the first `Weekly system restart` that follows H4.3c, part 1 holds again with no one
   acting.

## Baseline at `70591c307`

- **The shutdown chain is two Ansible halves.** `ansible/roles/setup/nut_host/tasks/main.yml`
  writes these things:
    - on `ups_host` (daniel-server) only, the udev rule at
      `/etc/udev/rules.d/99-nut-apc.rules`, then `udevadm control --reload-rules` and
      `udevadm trigger --subsystem-match=usb`;
    - on every host the role applies to, `/etc/nut/nut.conf` and `/etc/nut/upsmon.conf`,
      both root:nut 0640, with the second carrying `nut_monitor_password`;
    - on every such host while `nut_host_watchdog_armed` is true (the default),
      `/etc/nut/kuma-push.env`, `/usr/local/bin/ups-secondary-health.sh` at 0700, the cron
      file `ups-secondary-health` every `nut_host_watchdog_interval_minutes` (10), and the
      `nut-host` stamp from `stamp_render.yml`.
  The role applies to daniel-box too, so only the udev tasks are daniel-server's alone.
- **One push token per host.** `kuma-push.env.j2` picks the token by `inventory_hostname`:
  `ups_secondary_daniel_server_push_token` on daniel-server and `ups_secondary_push_token` on
  daniel-box (`ansible/roles/k8s/nut/CLAUDE.md`, issue #952).
    - `ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2` gates
      `UPS Secondary (daniel-server)` on `ups_secondary_daniel_server_push_token` and
      `UPS Secondary (daniel-box)` on `ups_secondary_push_token`. Its comment says one tile
      per host, because a shared token would let either host's `up` mask the other's `down`.
    - Each tile is a push tile with `interval` `nut_host_watchdog_interval_minutes * 60 * 2`
      (1200 s) and `max_retries: 0`, so a missing push turns it DOWN after 20 minutes.
    - `ansible/tests/setup/test_nut_host_secondary.py:TILES` maps each host to its tile and
      token.
- **The watchdog script.** `ups-secondary-health.sh.j2` sources
  `/usr/local/lib/kuma-push-lib.sh` and exits 1 if it is missing. It reads its token from
  `/etc/nut/kuma-push.env` and exits 0 without a push when the token is empty. It takes the
  upsd endpoint from `/etc/nut/upsmon.conf`. The `DECIDED` marker on the task that installs
  it explains the 0700 mode: `scripts/secrets_mgmt/secret_bearing_host_paths.py` derives the
  path from role tasks, and `scripts/secrets_mgmt/tests/test_secret_bearing_host_paths.py`
  pins it.
    - The script renders `domain` inline: `KUMA_HOST="uptime-kuma.local.{{ domain }}"`.
      `grep -c '^domain:'` on `ansible/vars/secrets.yml` prints 0, because #5087 moved `domain`
      to `ansible/inventory/group_vars/all.yml`. It is therefore not a SOPS key, and no host
      file carries it. `KUMA_RESOLVE_IP` reads `k3s_metallb_ingress_vip` from
      `ansible/inventory/group_vars/all.yml`, which is not a secret.
    - `test_nut_host_secondary.py:test_the_usb_half_is_confined_to_the_ups_host` asserts
      that the udev tasks still exist.
- **daniel-server needs no live read; daniel-box does.** In the role defaults,
  `nut_host_upsd_host` is `127.0.0.1` when `inventory_hostname == ups_host`. Any other host
  takes the `nut` Service's ClusterIP from a `kubectl` read at deploy time. This slice
  therefore moves daniel-server only, and H7.8 moves daniel-box after pinning the ClusterIP.
- **The render context has no `inventory_hostname`.** Ansible sets that magic variable per
  host. `scripts/lib/render_context.py:render_context` stacks `BASE_CONTEXT` stubs, role
  defaults, `all.yml`, host_vars, `playbook_dir` and overrides, and no module in
  `scripts/lib/` sets it. These calls were run in the worktree, on
  `ansible/roles/setup/nut_host/templates/host-upsmon.conf.j2` with `host='daniel-server'`:
    - with no override, `nut_host_upsd_host` resolves to `''`, and `'inventory_hostname' in
      ctx` is False;
    - with `strict=True`, it still resolves to `''` and nothing raises;
    - with `overrides={'inventory_hostname': 'daniel-server'}`, it resolves to `127.0.0.1`.

  `scripts/deploy_tools/setup_lib/setup_gates.py:eval_when_strict` sets `inventory_hostname` itself,
  so a census and a render can evaluate one comparison differently. Host mode must therefore
  supply the variable (*Render context* under *NUT on daniel-server*).
- **The playbook gate and the deployer.** `ansible/initial_setup.yml` runs `nut_host` when
  `inventory_hostname == ups_host or nut_host_secondary_armed | bool`. Only
  `ansible/inventory/host_vars/daniel-box.yml` sets `nut_host_secondary_armed: true`.
    - `deploy_changes.py:_BROAD_MANUAL_PREFIXES` lists `ansible/initial_setup.yml`. A change
      there parks the GitOps deployer with no fast-forward until someone applies it by hand,
      which stalls every session's landing.
    - `deploy_changes.py:_BROAD_SETUP_PREFIXES` lists `ansible/roles/setup/`. The tick applies
      a change to a setup role itself, on daniel-box.
    - `scripts/deploy_tools/setup_lib/setup_gates.py:eval_when_strict` evaluates an
      `inventory_hostname` comparison against a host's merged vars.
    - ansible-core is pinned at 2.21.5 in `pyproject.toml`. Its `meta: end_role` (2.18 and
      later) evaluates `when:` per host and skips the rest of the role.
    - `scripts/deploy_tools/land_lib/land_reach.py:setup_role_hosts` reads only the
      playbook gate when a role has one. `test_land_nut_host_reaches_both_hosts.py` pins
      `setup_role_hosts("nut_host")` to daniel-box and daniel-server, and a landing note
      that names daniel-server. `scripts/docs/catalog_lib/glance_facts.py:setup_glance_lines`
      prints the playbook entry's `when:` text as the glance block's *Applied by* line.
    - `deploy_defer.py:unapplyable_setup_roles` returns each changed setup role that
      `deploy_setup_roles.py:tick_applies_setup_role` rejects. `optimize_pi` is gated onto
      the Pi, so every change to it records a `manual_plane` line. H3.8b clears one with an
      **operator-only** `gitops_state.py clear-owed manual_plane optimize_pi --applied
      <tags>`.
- **nut-client owns the files Ansible overwrites.** These facts were measured on daniel-box,
  nut-client `2.8.1-3.1ubuntu2`, on 2026-10-11.
    - `dpkg-query -W -f='${Conffiles}' nut-client` lists `/etc/nut/nut.conf` and
      `/etc/nut/upsmon.conf` as conffiles.
    - `/sbin/upsmon` is a shell wrapper. It sources `/etc/nut/nut.conf` and execs
      `/lib/nut/upsmon` only when `MODE` is `standalone`, `netserver` or `netclient`.
    - `/lib/nut/upsmon` contains the string `NUT_CONFPATH` and no `nut.conf`.
    - `nut-monitor.service` has `Type=simple`, `Restart=on-failure`,
      `EnvironmentFile=-/etc/nut/nut.conf`, `ExecStart=/sbin/upsmon -F` and
      `ExecReload=/sbin/upsmon -c reload`. `systemctl show` reports `StartLimitBurst=5` over
      `StartLimitIntervalUSec=10s`.
    - `host-upsmon.conf.j2` sets no `RUN_AS_USER`. man upsmon, *RELOADING NUANCES*, says the
      process that handles a reload has dropped root, so it must be able to read the file.
- **Decryption happens on the controller.** ADR-0003
  (`docs/adr/0003-sops-with-age-for-secrets-at-rest.md`) decrypts at deploy time through
  `community.sops`.
    - `ansible/.sops.yaml` has one creation rule, `.*(vars|secrets)\/.*\.ya?ml$`, encrypting
      to daniel-box, daniel-server and an off-box recovery key.
    - daniel-server's own key is at `~/.config/sops/age/keys.txt`, which
      `ansible/roles/setup/sops_setup/tasks/main.yml` generates.
    - `sops_setup` installs `/usr/local/bin/sops` at `sops_setup_version` (`v3.13.3`) on
      every host, the Pi included. It generates an age key only where
      `ansible_connection` is `local`.
- **These push tokens rotate only by hand.** `ansible/secret_rotation.yml` records
  `ups_secondary_push_token`, `ups_secondary_daniel_server_push_token`,
  `pi_sd_health_push_token` and `pi_recovery_push_token` as tier `auto`,
  and `nut_monitor_password` as `assisted`.
    - All four push tokens are in `scripts/secrets_mgmt/consumers.py:CROSS_HOST_PUSH_TOKENS`,
      and `consumers.py:consumer_tags` returns `()` for each of them.
    - The unattended path of `scripts/secrets_mgmt/secret_rotation.py:cmd_rotate` keeps only
      rows with consumer tags. It prints `skip (manual: cross-host consumer)` for the rest.
    - The `Weekly secret rotation (auto tier)` cron therefore never rotates any key this slice
      puts in a host package.
- **Rotation dates come from git.** `cmd_rotate` advances `last_rotated` to
  `scripts/secrets_mgmt/git_dates.py:ciphertext_rotation_dates`, because a token rotated by
  hand can be committed without its registry date moving. `git_dates.py:derived_rotation_dates`
  wraps that function and returns `{}` when git cannot answer, for example in a shallow clone.
  The pytest job in `.github/workflows/ci.yml` checks out with `fetch-depth: 0`.
    - `git_dates.py:_revisions` runs `git log --date=short`, so every date is a day.
    - In a depth-1 clone `_revisions` returns one commit. The closing `setdefault` loop of
      `ciphertext_rotation_dates` then dates every key at that commit, and raises nothing.
    - `git_dates.py:_is_whole_file_reencrypt` skips a commit that changed at least 90% of
      the keys two revisions share, when they share at least `_REENCRYPT_MIN_SHARED` (10).
- **The weekly reboot is the common boot path.** `Weekly system restart` in
  `ansible/roles/setup/initial_setup/tasks/crons.yml` reboots each host on Sunday at 07:30,
  from `weekly_reboot_*` in `ansible/inventory/group_vars/all.yml`. The Kuma maintenance of
  the same name covers every monitor from 07:25 for 50 minutes
  (`ansible/roles/k8s/uptime-kuma/CLAUDE.md`). A tile that goes DOWN inside it pages only
  when the window closes.
- **Drift is stamped.** `setup-drift-check.sh` (initial_setup, armed on daniel-server through
  `setup_drift_check_hosts`) reads stamp fragments generically, `nut-host` among them.
- **gitleaks scans commits, not artifacts.** `.gitleaks.toml` allows one SOPS path,
  `ansible/vars/secrets\.yml`. Its header records that the default rules false-positive on
  ciphertext beside a suggestive key name, and that a line-regex allowlist does not work in
  the pre-commit `gitleaks git --staged` mode. `prek.toml` runs gitleaks on commits. Nothing
  scans a built artifact.
- **H3 leaves two interim files on the Pi.** Per H3's PR H3.8a, `optimize_pi` writes
  `/etc/homelab/interim/pi_sd_health_push_token` and `pi_recovery_push_token`. H3.8b renders
  `domain` into both scripts from inventory. The scripts read both tokens from
  `$CREDENTIALS_DIRECTORY/<key>`, and their
  units declare one `LoadCredential=` line per key. H3.8b also ships
  `/usr/lib/homelab/kuma-push-lib.sh` inside the `pi-heartbeats` component. H3's *Open
  questions* 6 says that H4's last PR retires the two files.
- **H3's reconcile, as planned.** Step 2 starts every enabled unit that is not active.
  Step 3 restarts the units whose file or drop-in hash changed. Step 6 writes `applied.json`
  and exits non-zero if any step failed, so only a killed run leaves `applied.json`
  unwritten. The builder publishes a package whenever its rendered tree differs from the
  last published one. H3.9's land host wait renders every host package at the merge base and
  at the merge, and waits on each host whose tree differs.
- **Who can read what.** The agent has a restricted ssh login on daniel-server and none on
  the Pi (`docs/claude-agent-user.md`). `probe.py monitors` decrypts:
  `scripts/diagnostics/probe_lib/monitors.py` imports `core.py:prom_endpoint`, which calls
  `core.py:k8s_endpoint`, which calls `core.py:sops_extract('domain')`. The age-key blocker
  that call causes goes away once domain PR 1 of R2's *Publish `domain`* item lands
  (operator ruling of 2026-10-14), because `domain` is then read from the inventory. H3's
  *Verification*
  therefore gives the agent a Prometheus query over loopback, with
  `ansible/roles/k8s/observability/defaults/main.yml:observability_prometheus_cluster_ip` as
  the fallback.

## Design

### `homelab-secrets.service`

The unit is `Type=oneshot` with `RemainAfterExit=yes`, `TimeoutStartSec=60` and
`Environment=SOPS_AGE_KEY_FILE=/etc/homelab/age.key`. It runs
`/usr/lib/homelab/homelab-secrets`, a stdlib Python 3.12 script, in these steps:

1. It reads `/usr/lib/homelab/secrets.json`, which the renderer writes. The manifest names:
    - the host's own SOPS file, which the package ships as
      `/usr/lib/homelab/secrets/<host>.sops.yaml` (*Which ciphertext each host carries*);
    - the credential keys to materialise, from the host's `package.yml`;
    - the secret-bearing outputs, each with a template, a destination, an owner, a group, a
      mode, a `persist` flag, its dependent units, and the key names its placeholders use.
2. It runs `sops --decrypt --output-type json <ciphertext>` once and keeps the result in
   memory. Plaintext is written only to the outputs below. If decryption fails, the run
   writes nothing, deletes nothing, and exits non-zero.
3. It writes each credential to `/run/credstore/<key>`. The directory is root 0700 and each
   file root 0600. It deletes any file there that the manifest no longer lists.
4. It renders each output on its own, so one failed output leaves the others written.
    - Substitution is the exact inverse of the renderer's escape, in one left-to-right pass
      (*Secret-bearing files*): `$$` becomes `$`, an unescaped `${<name>}` takes the value
      of `<name>`, and any other `$` stays as it is.
    - A placeholder whose key the output does not list, or the decrypted file does not hold,
      fails that output. So does an unescaped `${` that does not close as `${<name>}`. Flux
      would substitute an empty string; this unit refuses.
    - An output with `persist: false` goes to `/run/homelab-secrets/<destination>`. An output
      with `persist: true` goes to `/var/lib/homelab-secrets/<destination>`, so it survives a
      reboot and its consumer can start without a decrypt. A persisted output is plaintext
      at rest, on the Pi's SD card as on any other disk. C5 therefore admits one only for a
      consumer that must start when the decrypt cannot run, and *Persisted outputs* names
      each one.
    - The script creates or corrects every directory above an output as root:root 0755. The
      file itself takes the listed owner, group and mode, so the file mode alone protects the
      content. A consumer that drops root, such as upsmon on reload, can then reach the file.
    - It writes through a temporary file in the same directory, then renames it, so a reader
      sees either the old file or the complete one.
    - It deletes outputs the manifest no longer lists, under both roots, and their recorded
      HMACs (step 5).
5. It records which dependents owe a restart.
    - It keeps an HMAC of each output in `/var/lib/homelab-secrets/.digests`, under a random
      key in `/var/lib/homelab-secrets/.hmac-key`. Both files are root 0600, and both survive
      a reboot.
    - When an output's HMAC differs from the recorded one, or none is recorded, the script
      adds that output's dependents to `/run/homelab-secrets/pending.json`. It never removes
      an entry. Reconcile removes each entry after acting on it (*The restart contract*).
    - It holds an exclusive `flock` on `/run/homelab-secrets/pending.lock` while it reads
      and rewrites `pending.json`, and reconcile does the same.
    - A re-encryption that leaves values unchanged adds nothing. The boot run adds nothing
      either, because the recorded HMACs survived the reboot. A reboot empties
      `pending.json`, which is correct, because every dependent then starts after
      `homelab-secrets`. That answers, for the host plane, the design's open item about
      ciphertext hashes over-triggering restarts.
6. It calls no `systemctl`, so it restarts nothing itself. Reconcile owns every restart
   (below). The script exits non-zero if any output or credential failed.

The script runs the `sops` that `homelab-tools` ships. C5 makes that package the one
architecture-specific package, and H3 builds it for `amd64` and `arm64`. The `secrets-unit`
component declares `Depends: homelab-tools`, and the host package itself stays
`Architecture: all`. The script calls `sops` by the absolute path that `homelab-tools`
installs, `/usr/lib/homelab/bin/sops` (H3.11), and never through `PATH`. H3.11 also makes the
updater upgrade every installed `homelab-*` package, so an unversioned
`Depends: homelab-tools` still picks up later builds. The first publish of
`homelab-daniel-server` follows H3.12's staged rollout. `/usr/local/bin` comes first on `PATH`,
and it holds the binary `sops_setup` installs until H7.1 lists that file as a legacy entry.
H4.2 ships the first `secrets-unit` component to an `amd64` host, and H4.5 ships it to the Pi.
Each of the two waits on H3's `homelab-tools` build for its host's architecture.

### Persisted outputs

C5 admits a persisted output only for a consumer that must start before the decrypt can run.
This slice persists exactly one:

- **`/var/lib/homelab-secrets/etc/nut/upsmon.conf` on daniel-server** (H4.3a), root:nut
  0640. Its consumer is `nut-monitor`, the client half of the UPS shutdown chain. The chain
  must run at boot even when the decrypt cannot, such as after a lost age key or a broken
  `sops`. A `nut-monitor` that waited on `homelab-secrets` would leave daniel-server
  unprotected on exactly the boots where something else has already failed. The file
  carries the exposure `/etc/nut/upsmon.conf` already has (*Risks*).

Every other secret-bearing value that this slice and H6 add stays on tmpfs:

- the Pi's two credentials and daniel-server's one go to `/run/credstore`;
- H6's `k3s_readonly_token` goes to `/run/credstore` on both cluster nodes.

The Pi therefore persists no output, and its SD card holds no rendered plaintext. H5
names one conditional exception: if the operator declines alloy's password in the container
environment, H5's file route makes it a persisted output on the Pi. That route fails C5's
rule, because `homelab-compose` already starts after `homelab-secrets`. The route takes
`persist: false` instead, and the census keeps it off the list.

Two files under the persisted root are state, not outputs: `.digests` and `.hmac-key`
(step 5). They hold one HMAC per output and the random key the HMACs use, and never a value.
Every host that carries the `secrets-unit` component has them, the Pi included.

A census in `hosts/lib/tests/` holds the set of persisted outputs across every rendered
package equal to a named list. The list has one member, daniel-server's `upsmon.conf`, and
each entry carries its reason. A later slice that persists an output adds it to the list in
the same PR, with the consumer that must start without the decrypt.

### The restart contract: reconcile, and two restarters for k3s

This slice owns the rule for starting and restarting a unit on the host plane. H3's
reconcile steps 2 and 3, H5's `homelab-compose` and H6's `homelab-k3s-restart` adopt it, and
*Open questions* 5 lists the text each one changes.

`homelab-reconcile` is the only process that starts or restarts a unit outside the boot
path, unless the manifest marks the unit `windowed`. `homelab-secrets` calls no
`systemctl`. systemd's own `Restart=` policy is not a caller and stays outside this rule,
and so does an operator's hand command.

A `windowed` unit is `k3s.service` or `k3s-agent.service`, which H6 marks in each host's
`package.yml`. Reconcile neither starts nor restarts one (step 2 and the restart set below).
Two unattended processes restart it, each under its own condition (H6). Until H6.4a and
H6.5a delete them, an operator-run `k3s-bringup.yml` apply also restarts it, through the tasks
"Restart k3s so the log drop-in takes effect" in `ansible/roles/setup/k3s/tasks/server.yml`
and "Restart k3s-agent so the log drop-in takes effect" in
`ansible/roles/setup/k3s/tasks/agent.yml`.

- **SUC's upgrade container** restarts the node's k3s unit after it replaces the binary. It
  runs when `promote` applies a changed Plan, behind `prepare`'s gates, outside any window
  (H6, *Gates at apply time*).
- **`homelab-k3s-restart.service`** restarts it inside the host's `k3s_restart_window`,
  when the hash of the k3s config paths its package ships has changed. It runs behind
  `k3s_gates.py host`, and it makes one attempt per hash (H6, *`homelab-k3s-restart`*).
- **The two exclude each other on a node.** `prepare` refuses while
  `/run/homelab-k3s-restart/` exists. systemd creates that directory through the unit's
  `RuntimeDirectory=` before `ExecStart` runs, and removes it when the unit stops. The unit
  refuses while a Plan reads a non-empty `.status.applying`, or a Job in `system-upgrade`
  carries no `Complete` or `Failed` condition (H6, *`homelab-k3s-restart`*, step 6). Each
  side sets its own mark before it reads the other's.
- **Recovery is the operator's.** A k3s that a failed restart left down or hung is
  restarted by hand (H6, *Rollback*), never by reconcile.

H4.1b changes H3's reconcile in the ways below. Every host package ships reconcile (H3
*Shared names*), so the change reaches the Pi at H4.1b, long before any Pi package carries
`secrets-unit`.

- **The secrets rules apply only to a package that ships the `secrets-unit` component.**
    - The renderer writes `secrets_inputs_sha256` into the manifest, and `secrets.json` into
      the package, only for such a package. It fails the render of a package whose
      components list an output or a credential without the `secrets-unit` component.
    - Reconcile keys the secrets step, the existence gate and the legacy-entry gate below on
      that manifest field. Without the field, reconcile makes no `systemctl` call that names
      `homelab-secrets`, the existence gate passes every unit, and legacy entries retire
      under H3's handoff rule 3 alone.
    - The Pi carries no `secrets-unit` component until H4.5. Between H4.1b and H4.5, the Pi's
      reconcile differs from H3's only in the restart rules below.
- **The restart queue exists on every host.** Reconcile creates `/run/homelab-secrets/` as
  root:root 0755 when it is absent, and reads a missing `pending.json` as an empty queue. A
  unit whose start fails on a host without `secrets-unit` is therefore queued like any other.
- **A secrets step, between H3's steps 1 and 2, in a package that ships `secrets-unit`.**
    - Its manifest carries `secrets_inputs_sha256`, a hash over the ciphertext file,
      `secrets.json` and every file under `/usr/lib/homelab/secret-templates/`.
    - When that hash differs from `applied.json`, or `homelab-secrets` is not active,
      reconcile runs `systemctl restart homelab-secrets.service` without `--no-block`. The
      unit is a oneshot, so the call returns only after the run has written every output.
    - When that call fails, reconcile carries on with the outputs that exist, and the run
      exits non-zero.
- **One restart set, built before step 2.**
    - The set is the union of two lists: the units whose file or drop-in hash changed, and
      the units in `/run/homelab-secrets/pending.json`. Reconcile reads that file on every
      run, whoever ran `homelab-secrets` last. The `windowed` and `manual` exemptions from H3
      still apply, so the set never holds a `windowed` unit.
    - The renderer fails a package in which an output lists a `windowed` unit as a
      dependent. Such an entry in `pending.json` would never drain.
- **Step 2 starts only what the set does not hold.** For each enabled unit that is not
  active, not in the restart set and not `windowed`, reconcile runs
  `systemctl reset-failed <unit>`, then `systemctl start <unit>`. A unit in the set is left
  to step 3, whose `restart` also starts an inactive unit. Each unit therefore gets at most
  one start or restart per reconcile run, and a unit that has hit its start limit is reset
  before that one call.
    - The `windowed` skip is new in step 2. H3's step 2 starts every enabled unit that is
      not active. On daniel-box on 2026-10-11, `systemctl is-enabled k3s` printed `enabled`,
      and `systemctl show k3s` read `Type=notify`, `Restart=always` and
      `TimeoutStartUSec=infinity`. A k3s hung after a failed restart reads `activating`. A
      blocking `start` would then hold reconcile until its own 30-minute `TimeoutStartSec`
      killed it, on every run. Until H6 marks a unit `windowed`, the skip matches nothing.
- **Step 3 walks the set** in unit-name order. For each unit that is enabled or active, it
  runs `systemctl reset-failed <unit>`, then `systemctl restart <unit>`.
    - Neither call in step 2 or step 3 uses `--no-block`, so reconcile reads each result. It
      never uses `try-restart`, which does nothing to a failed unit. `reset-failed` clears
      the start limit of 5 starts in 10 s. Reconcile leaves a unit it does not touch in
      whatever state it found, so a failure stays visible in `systemctl --failed`.
    - *Enabled* means the manifest ships an enable link for a `homelab-*` unit. For another
      package's unit, such as `nut-monitor`, it means `systemctl is-enabled` prints
      `enabled`. Reconcile never asks `is-enabled` about a homelab unit, because systemd does
      not count a link under `/usr/lib/systemd/system/<target>.wants/` as enabled (*Review
      log*, X-09).
    - *Active* means `systemctl is-active` prints `active`. A timer-activated service
      between runs is neither enabled nor active, so a change to its file never fires an
      extra run.
- **The existence gate covers steps 2 and 3.** Reconcile starts or restarts a unit only
  when every output that lists the unit as a dependent, and every credential its component
  lists for it, exists. The gate applies whatever put the unit there: a hash change,
  `pending.json` or step 2.
    - After a failed secrets step the gate still passes a unit whose outputs exist, such as
      `nut-monitor` on a persisted `upsmon.conf`. It skips a unit whose output is missing,
      such as on a fresh host where no run has written it.
    - When the gate skips a unit, or the unit's start or restart fails, reconcile adds the
      unit to `pending.json` (or keeps it there) and the run exits non-zero. H3's step 6
      writes `applied.json` even after a failed step, so a unit that entered the set by a
      hash change would otherwise be missing from the next run's set, and its restart lost.
    - Reconcile removes a unit from `pending.json` after its start or restart succeeds,
      when the unit is neither enabled nor active, or when the manifest marks it `windowed`.
- **Legacy entries wait for the secrets.** In a package that ships `secrets-unit`, reconcile
  retires the legacy entries of a component that lists an output or a credential only when
  `homelab-secrets` is active and every output and credential that component lists exists.
  This gates H3's handoff rule 3 on the secrets as well as on unit starts. A component that
  lists neither, and every component of a package without `secrets-unit`, retires its legacy
  entries under rule 3 alone.

Three properties follow from these rules, and H5 relies on each one.

- **Order comes from sequence, not from one transaction.** The secrets step's call returns
  after `homelab-secrets` has finished, and steps 2 and 3 begin after that. Every dependent
  that either step starts or restarts therefore reads the re-rendered outputs. A dependent's
  `After=homelab-secrets.service` orders only the boot path and a start that pulls
  `homelab-secrets` in through `Wants=`.
- **An owed restart survives a failed run.** Reconcile removes an entry only after acting on
  it, and queues every unit it skipped or failed to restart. A run killed after the secrets
  step leaves the owed dependents in `pending.json`. A run can die that way at its own
  30-minute `TimeoutStartSec`, during a long dependent restart. With one call per unit, the
  longest single call is a `homelab-compose` restart. H5 derives that unit's
  `TimeoutStartSec` from the Pi's healthcheck variables as 1390 s, about 23 minutes, at
  `70591c307` (H5, *The wait timeout*). One such call fits inside reconcile's 30 minutes.
  The next run restarts the owed dependents, although its own `homelab-secrets` run adds
  nothing.
- **No unit in a restart set waits on reconcile.** Each call blocks until the unit's job
  finishes. A unit ordered `After=homelab-reconcile.service` would wait for the run that is
  waiting for it, until a timeout ended both. So no shipped unit carries that ordering, and
  `homelab-secrets` names reconcile in no ordering directive. A census on the rendered units
  enforces both rules (*Verification*).

At boot, systemd starts `homelab-secrets` before its dependents through their `After=`, so
nothing needs a restart. A person who restarts `homelab-secrets` by hand restarts no
dependent. That run queues the dependents of every changed output in `pending.json`, where
they wait for the next reconcile run. `systemctl start homelab-reconcile` is the hand path
that runs one now.

### Which ciphertext each host carries

Every host package ships one per-host SOPS file, `ansible/vars/hosts/<host>.sops.yaml`,
installed as `/usr/lib/homelab/secrets/<host>.sops.yaml`. It holds only the keys that host's
rendered tree names, as placeholders or as credentials.

- **Why per host, not a byte copy of `secrets.yml`.** A copy of `ansible/vars/secrets.yml`
  would change with every commit to that file. H3's builder publishes a package whenever its
  rendered tree differs, and H3.9's land host wait targets each host whose tree differs
  (Baseline). Every `/add-secret` PR and every rotation would then publish a new host
  package. Its landing would wait on that host's updater, and end `deploy-failed` whenever
  the host holds. A per-host file changes only when one of its own keys does, and it keeps
  each package to the ciphertext its host uses.
- **Recipients.** Each file is encrypted to its host's own key, daniel-box's key and the
  off-box recovery key. A creation rule for each path goes ahead of the general rule in
  `ansible/.sops.yaml`, whose pattern `.*(vars|secrets)\/.*\.ya?ml$` also matches
  `ansible/vars/hosts/`. The Pi stays off `secrets.yml`'s recipient list, which keeps the
  2026-10-03 removal that `docs/break-glass.md` records.
- **Contents in this slice.**
    - daniel-server: `ups_secondary_daniel_server_push_token` from H4.2,
      `nut_monitor_password` from H4.3a.
    - daniel-pi: `pi_sd_health_push_token` and `pi_recovery_push_token` from H4.4. Those are
      the two credentials H3.8b's heartbeats load (Baseline).
    - `domain` is in no host file. It is not a SOPS key since #5087, and a rendered template
      reads it from inventory.
    - daniel-box's file arrives with its package in H6 (*Open questions* 7).
- **`scripts/secrets_mgmt/host_secrets.py check`** keeps each copy in step. It runs in the
  pytest job without a key, and it fails on any of these:
    - the file's key names differ from the placeholders and credentials the host's rendered
      tree uses;
    - for a key the file carries, the newest commit that changed its ciphertext in the host
      file does not descend from, or equal, the newest commit that changed it in
      `secrets.yml`. `git merge-base --is-ancestor` decides. A `sync` in the same commit as
      the rotation therefore passes, and a stale copy fails even when both commits fall on
      one day, which day-level dates (Baseline) would pass;
    - a key for which git returns no commit;
    - a shallow clone, where `git rev-parse --is-shallow-repository` prints `true`. In a
      depth-1 clone the walk dates every key in both files at the one visible commit, so the
      comparison would pass. The pytest job checks out with `fetch-depth: 0`.
- **The walk.** H4.1d adds `git_dates.py:ciphertext_rotation_commits`, a sibling of
  `ciphertext_rotation_dates` that returns the commit SHA in place of the day, and gives both
  a path parameter that defaults to `secrets.yml`.
    - The walk over `secrets.yml` keeps the whole-file re-encrypt guard, so a merge-conflict
      re-encrypt does not read as a rotation that needs a `sync`.
    - The walk over a host file runs without that guard. Each `sync` rewrites every value of
      the file, and it is the event the check looks for. With the guard, a host file of 10
      keys or more would have its `sync` commits skipped, and a correct copy would fail.
- **`sync`** decrypts `secrets.yml` and rewrites one host file with the keys its tree names.
  That makes it **operator-only**. It runs in the same PR as the change that needs it: a PR
  that adds a key to a host's tree, or one that rotates a key a host file carries. `check` in
  CI is the gate. No cron calls it.
- **No unattended rotation of a host-file key.** H4.1d makes the unattended path of
  `secret_rotation.py:cmd_rotate` skip every key a host file carries, beside its
  `skip (manual: cross-host consumer)` rows. At `70591c307` the cron rotates none of this
  slice's keys (Baseline), and the skip keeps an unattended commit from leaving a host file
  stale once H5 adds keys to the Pi file.

### Secret-bearing files

A component declares each secret-bearing file under `outputs:` in its `component.yml`, with
the fields `secrets.json` carries. The renderer acts on that declaration and never scans a
rendered file to decide.

- **Placeholders render only in declared files.** In a declared output, host mode renders a
  reference to a secret key as `${<key>}`, where `<key>` is the key's name in
  `ansible/vars/secrets.yml`. It escapes the rest of the file by C1's cluster-mode rule: a
  literal `$` followed by `$` or by `{` renders as `$$`.
- **Compose files are the one other kind.** H5's compose component declares its compose
  files, which install at their real path under `/usr/lib/homelab/compose/`. Host mode
  renders their placeholders and escapes them the same way, because Compose reads `$$` as
  `$`. Compose itself substitutes them (H5), so routing exempts them.
- **Every other host file keeps H3's rule.** H3's *Host mode renders no secret* still holds
  there: secret keys are stripped, the render is strict, and every `$` stays as written. A
  template that reads a secret without being declared therefore fails its render, naming the
  key. A shell script with `${VAR}` expansions, such as the UPS watchdog, stays a plain file.
- **Where a declared output installs.** It installs under
  `/usr/lib/homelab/secret-templates/<path>`, and `homelab-secrets` renders it.
- **The inverse.** `homelab-secrets` reads a template once, left to right:
    - `$$` becomes `$`;
    - an unescaped `${<name>}` takes the value of `<name>`. The output fails when its key list
      does not name `<name>`, or the decrypted file does not hold it;
    - an unescaped `${` that does not close as `${<name>}` fails the output;
    - any other `$` stays as it is.

  The pass must be single. Two sequential replacements would read the `${key}` inside a
  literal `$` followed by a placeholder, `$${key}`, a second time. Because the renderer
  escapes every literal `${`, a template's shell `${VAR}` renders as `$${VAR}` and comes out
  as `${VAR}`. So a placeholder in a rendered template is always an unescaped `${<name>}`,
  and the renderer emits one only for a secret reference.

A declared output's consumer reaches it in one of two ways:

- **A symlink.** The package ships a symlink at `<path>` pointing to the output. This is the
  default, and it needs a real path that no other package owns.
- **A drop-in.** When another package owns the real path, as `nut-client` owns
  `/etc/nut/upsmon.conf` as a conffile, the package ships no symlink. A drop-in on the
  consumer's unit names the output's location instead. A package file over another package's
  file fails the CI container install, so a wrong choice cannot ship.

Every such file passes through exactly one substitution: compose files through Compose
itself (H5), declared outputs through `homelab-secrets`. These rules change C1 and H3's
*Host mode renders no secret* (*Open questions* 5 and 6).

### NUT on daniel-server: the `nut-secondary` component

- **One template source until H7.8.** The component renders `host-upsmon.conf.j2` and
  `ups-secondary-health.sh.j2` from `ansible/roles/setup/nut_host/templates/`, the same files
  the role renders for daniel-box. Nothing moves until H7.8 moves daniel-box. The role keeps
  owning the files, and `secret_bearing_host_paths.py` keeps reading them.
- **Render context.** `component.yml` names `nut_host` as its defaults source. The renderer
  then layers that role's defaults under the host's inventory for every template in the
  component, its own unit templates included, as `render_context` does for a role template
  by path.
    - The component's own `defaults:` map sets `nut_host_watchdog_packaged: true`. That map
      sits above the `nut_host` role defaults and below inventory, so H3's rule that a
      component value never sits above `host_vars` holds.
    - Host mode sets `inventory_hostname` to the host it renders, as Ansible does. It passes
      the value as an override, because it is a magic variable that no inventory layer may
      set (*Open questions* 5). Without it, `inventory_hostname == ups_host` is false on
      every host (Baseline). `nut_host_upsd_host` would resolve to `''`, the MONITOR line
      would render as `apc-ups@`, and `nut_host_watchdog_token_key` would name daniel-box's
      token.
- **udev.** `/usr/lib/udev/rules.d/99-homelab-nut-apc.rules` holds the role's rule text
  verbatim. Reconcile's udev reconciler runs `udevadm control --reload-rules` and
  `udevadm trigger --subsystem-match=usb` when a shipped rule is added or its hash changes.
  The legacy entry is `/etc/udev/rules.d/99-nut-apc.rules`.
    - A rule that the manifest drops is not a hash change. dpkg removes the file, and the
      reconciler makes no `udevadm` call for it.
    - A device then keeps the mode of its last udev event until the next one, such as a
      trigger, a replug or a reboot. *Rollback* orders H4.3c's revert around this rule.
- **Config, with no diversion.** `upsmon.conf` is a declared output with `persist: true`,
  rendered root:nut 0640 to `/var/lib/homelab-secrets/etc/nut/upsmon.conf`. The `upsd`
  endpoint renders as the literal `127.0.0.1`, and `nut_monitor_password` as the placeholder
  `${nut_monitor_password}`.
- **The drop-in.** `/usr/lib/systemd/system/nut-monitor.service.d/50-homelab.conf` carries:
    - `Wants=homelab-secrets.service` and `After=homelab-secrets.service`, never
      `Requires=`;
    - `Environment=NUT_CONFPATH=/var/lib/homelab-secrets/etc/nut`;
    - an empty `EnvironmentFile=`, then `ExecStart=` reset to `/lib/nut/upsmon -F`, and
      `ExecReload=` reset to `/lib/nut/upsmon -c reload`.

  The wrapper `/sbin/upsmon` exists only to gate on `MODE` in `/etc/nut/nut.conf`. The
  package is the decision that this host runs a netclient upsmon. Calling the daemon directly
  means no file that nobody manages can switch the chain off. The Ansible-era
  `/etc/nut/nut.conf` and `/etc/nut/upsmon.conf` stay in place as nut-client conffiles. They
  are not legacy entries, because they are the rollback path: when the drop-in goes, the
  vendor unit reads them again.
- **Dependents.** `secrets.json` lists `nut-monitor.service` as the dependent of
  `upsmon.conf`.
- **Watchdog.**
    - `homelab-ups-watchdog.service` and `.timer` replace the cron, every
      `nut_host_watchdog_interval_minutes` minutes. The timer's enable link ships only while
      `nut_host_watchdog_armed` is true, so the disarm path survives, as H3.8b does for the
      recovery timer.
    - The service runs as root and loads one credential:
      `LoadCredential=kuma_push_token:/run/credstore/{{ nut_host_watchdog_token_key }}`. It
      reads `domain` from inventory through the rendered script.
    - A role default, `nut_host_watchdog_token_key`, names the key per host:
      `ups_secondary_daniel_server_push_token` on `ups_host` and `ups_secondary_push_token`
      elsewhere. The component's test pins it to `TILES`.
    - `package.yml` lists `ups_secondary_daniel_server_push_token` in the
      credentials `homelab-secrets` materialises. A census fails a package whose unit loads a
      credential the package does not list.
- **The shared watchdog template.** `ups-secondary-health.sh.j2` gains one role default,
  `nut_host_watchdog_packaged` (false), which only the component's render context sets. When
  it is true, the template differs from the daniel-box render in three lines:
    - it sources `/usr/lib/homelab/kuma-push-lib.sh`;
    - it reads its token from `$CREDENTIALS_DIRECTORY/kuma_push_token`;
    - it reads the endpoint from `/var/lib/homelab-secrets/etc/nut/upsmon.conf`, the file
      upsmon uses.

  The script is not a declared output. It holds many shell `${...}` expansions, so it ships
  as a plain package file with every `$` as written. The daniel-box render stays
  byte-identical, and the textual assertions in `test_nut_host_secondary.py` keep holding on
  that branch.
- **`kuma-push-lib.sh` gets its own component.** A shared `kuma-push` component ships
  `/usr/lib/homelab/kuma-push-lib.sh`. The Pi package and the daniel-server package both list
  it, and `pi-heartbeats` stops shipping the file. On the Pi the path and the package stay
  the same, so dpkg changes nothing.
- **Legacy entries:** `/etc/cron.d/ups-secondary-health`,
  `/usr/local/bin/ups-secondary-health.sh`, `/etc/nut/kuma-push.env`,
  `/etc/udev/rules.d/99-nut-apc.rules` and the `nut-host` stamp fragment.
- **Depends:** `nut-client`, which is installed already. The H3 gate covered an added
  `Depends`.

### The role guard after this slice

`ansible/initial_setup.yml` is not edited, so this slice never parks the deployer.

- The role deletes its three udev tasks.
- Its first task becomes `ansible.builtin.meta: end_role` with
  `when: inventory_hostname == ups_host`, under a `# DECIDED` marker. The marker says why
  the exclusion lives in the role: an `initial_setup.yml` edit is in
  `_BROAD_MANUAL_PREFIXES` and parks every session's landing.
- The role defaults keep the `127.0.0.1` branch of `nut_host_upsd_host`, because the
  component's render reads it.
- A change under `ansible/roles/setup/nut_host/` is setup-plane, so the tick applies it on
  daniel-box. There the rendered files stay byte-identical, so the apply changes only the
  `nut-host` stamp, whose template hashes moved.
- Three readers evaluate a role's leading `end_role` guard with
  `setup_gates.py:eval_when_strict`, through one helper that H4.3b2 adds to
  `scripts/deploy_tools/setup_lib/setup_gates.py`:
    - H3's two-writer census sees `nut_host` applying to daniel-box only, so the
      daniel-server package's legacy entries collide with nothing;
    - `land_reach.setup_role_hosts` drops a host on which the guard passes, so a `nut_host`
      landing owes daniel-server no hand apply. A template change still makes daniel-server
      a host target through its package render (H3.9);
    - `glance_facts.py:setup_glance_lines` adds the guard's text to the *Applied by* line,
      so the role's glance block names the host the role ends on.

### Data flow

```text
package: secrets/<host>.sops.yaml + secrets.json + secret-templates/ + drop-in
   │  postinst ─▶ homelab-reconcile
   │                1. stop dropped units
   │                1a. inputs hash changed ─▶ systemctl restart homelab-secrets (blocks)
   ▼                                                  │
homelab-secrets (age key /etc/homelab/age.key)        │
   ├─▶ /run/credstore/<key>                     (LoadCredential= for timers)
   ├─▶ /var/lib/homelab-secrets/etc/nut/upsmon.conf ◀── NUT_CONFPATH (nut-monitor drop-in)
   └─▶ /run/homelab-secrets/pending.json ─▶ restart set = hash changes + pending
                                            reconcile 2: reset-failed + start, units
                                                         outside the set
                                            reconcile 3: reset-failed + restart, set
                                            both: only when outputs exist; a skipped or
                                                  failed unit is queued in pending.json
                                            reconcile 4: legacy entries, once secrets exist
```

### File tree this slice adds or changes

```diff
+ hosts/lib/homelab-secrets
+ hosts/components/secrets-unit/component.yml
+ hosts/components/kuma-push/component.yml            (kuma-push-lib.sh, out of pi-heartbeats)
+ hosts/components/nut-secondary/component.yml        (renders nut_host's templates until H7.8)
+ hosts/components/nut-secondary/templates/           (drop-in, watchdog units, udev rule)
+ hosts/daniel-server/package.yml
+ ansible/vars/hosts/daniel-server.sops.yaml          (operator-only content)
+ ansible/vars/hosts/daniel-pi.sops.yaml              (operator-only content)
+ scripts/secrets_mgmt/host_secrets.py
+ docs/adr/                                           (one ADR amending ADR-0003)
  scripts/renderer/                                   (host mode: inventory_hostname, declared
                                                       outputs, placeholders and escape)
  hosts/lib/homelab-reconcile                         (secrets step, restart contract, udev arm)
  hosts/components/pi-heartbeats/component.yml        (LoadCredential from /run/credstore)
  ansible/.sops.yaml                                  (per-host creation rules ahead of the
                                                       general rule)
  .gitleaks.toml                                      (per-host SOPS files, package ciphertext)
  scripts/secrets_mgmt/git_dates.py                   (path parameter, commit-returning walk)
  scripts/secrets_mgmt/secret_rotation.py             (unattended path skips host-file keys)
  scripts/deploy_tools/setup_lib/setup_gates.py       (leading end_role guard reader)
  scripts/deploy_tools/land_lib/land_reach.py         (setup_role_hosts reads the guard)
  scripts/docs/catalog_lib/glance_facts.py            (Applied by names the guard)
  ansible/roles/setup/nut_host/tasks/main.yml         (udev tasks removed, end_role guard)
  ansible/roles/setup/nut_host/defaults/main.yml      (two watchdog defaults)
  ansible/roles/setup/nut_host/templates/ups-secondary-health.sh.j2  (packaged branch)
  ansible/roles/setup/optimize_pi/tasks/main.yml      (interim credential tasks removed, H4.6)
  .github/workflows/ci.yml                            (host_packages job: scans, fixture key)
```

## Steps, as PRs

Each PR states its approximate size, tests included. H3 and H5 cite `H4.1b` for the
reconcile change, and H6 and H7 cite `H4.2` for the age-key copy, so those two ids keep their
meaning.

1. **H4.1a1 The secrets script: decrypt and render (about 200 lines, half of them tests).**
   Gate: the C5 amendment in *Open questions* 4. The pillar review fold applied it, so the
   gate is met.
    - Add `hosts/lib/homelab-secrets` with steps 1 to 4 and 6: the manifest read, the one
      decrypt, the credentials, and each output's single-pass substitution, directory modes
      and atomic write.
    - Unit tests in `hosts/lib/tests/` with a stub `sops`.
    - No host changes.
2. **H4.1a2 The secrets script: the restart queue (about 150 lines).**
    - Add step 5: the HMAC baseline under `/var/lib/homelab-secrets/`, `pending.json` and its
      lock, with unit tests.
    - No host changes.
3. **H4.1a3 The `secrets-unit` component and the host renderer (about 200 lines).** Gate: the C1
   amendment in *Open questions* 6. Its row must stand in contracts.md's *Requested
   amendments not yet applied*, or the amendment must be applied, before this PR merges.
    - The `secrets-unit` component: the unit, and the `secrets.json` schema with its `persist`
      flag and each output's key list. It is named `secrets-unit` because the operator's
      tooling guard denies agent writes under any `secrets/` directory.
    - The renderer reads each component's declared `outputs:`. It renders their secret
      references as placeholders with C1's escape and routes them to `secret-templates/`. It
      keeps a declared compose file at its real path (*Secret-bearing files*). It fails a
      package whose output lists a `windowed` unit as a dependent (*The restart contract*).
    - Host mode sets `inventory_hostname` to the host it renders (*Render context*).
    - Renderer tests in `hosts/lib/tests/` (*Verification*).
    - No host changes. The Pi's tree renders byte-identical: the Pi package declares no
      output, and no template under `ansible/roles/setup/optimize_pi/templates/` reads
      `inventory_hostname` at `70591c307`. If H3.9's host wait finds the Pi's tree changed
      all the same, the PR takes H4.1b's landing.
4. **H4.1b The restart contract in reconcile (about 200 lines). A Pi host change.**
    - Extend `hosts/lib/homelab-reconcile` with the `secrets-unit`-component condition, the queue
      directory, the secrets step, the restart set built before step 2, step 2's skip and
      `reset-failed`, step 2's `windowed` skip, the existence gate, the `pending.json` drain
      and refill, and the legacy-entry gate.
    - Stub-`systemctl` tests in `hosts/lib/tests/`, including a manifest without `secrets-unit`
      (*Verification*).
    - **What changes on the Pi.** Every host package ships reconcile (H3 *Shared names*), and
      the builder publishes a package whose rendered tree differs (H3 *Package rules*). The
      merge therefore republishes `homelab-daniel-pi`, and the Pi runs the changed reconcile.
        - The Pi's manifest carries no `secrets-unit`, so no secrets rule acts there.
        - The Pi's own units move from H3's `try-restart` to `reset-failed` and a blocking
          `restart`, and step 2 resets a unit before it starts it. This is the slice's first
          behaviour change on a live host.
    - **Landing.** H3.9's host wait renders every host package at the merge and at its first
      parent, so it waits on the Pi. The Pi settles when `homelab_host_applied_info{host="daniel-pi"}`
      names the merge SHA or a descendant and `homelab_host_reconcile_ok{host="daniel-pi"}`
      is 1. A failed reconcile holds the Pi and ends the landing `deploy-failed`. The agent
      reads both series through Prometheus (*Verification*).
5. **H4.1c The CI checks (about 150 lines).**
    - Extend the `host_packages` job that H3 adds to `.github/workflows/ci.yml`. This slice
      adds no workflow. The `prek` gate already lists that job in `needs` and reads its
      result, so the required context covers every step below. The job still needs no
      repository secret. It gains four steps:
        - the container installs the `homelab-tools` package that the job builds, so the
          tests run the `sops` the hosts run. Until H3's `homelab-tools` build exists, it
          installs `sops` at `sops_setup_version`, pinned by sha256;
        - each container test of a package that carries `secrets-unit` generates an age key,
          writes it to `/etc/homelab/age.key`, and builds the package with a fixture
          ciphertext encrypted to that key. The fixture holds the real key names with fake
          values. The test asserts that the fixture build differs from the real build only
          at the ciphertext path. Until H4.2 adds the first real one, a fixture package
          that carries `secrets-unit` exercises this path;
        - `gitleaks dir` over a named set of package roots, which starts as
          `homelab-updater` and `homelab-daniel-pi`;
        - the placeholder check over the same named set. It reads each root's declared
          outputs under `usr/lib/homelab/secret-templates/` and its compose files, and fails
          on a placeholder whose key the host's SOPS file does not carry. It imports nothing
          from R2, so H4 keeps its single edge to H3.
    - The two scans read only the rendered package roots. They therefore run after H3's
      render step and before its early exit for an unchanged tree. A PR that changes only
      `.gitleaks.toml` or the gitleaks pin still runs them.
    - Extend `.gitleaks.toml`'s SOPS path allowlist to `usr/lib/homelab/secrets/.*\.sops\.yaml`,
      the ciphertext's path inside a package root.
    - Add the census that nothing waits on reconcile (*Verification*).
    - No host changes.
6. **H4.1d Per-host SOPS files: the tool (about 200 lines).**
    - The PR carries the ADR that amends ADR-0003: each host decrypts its own per-host SOPS
      file with its own key. Approving it is **operator-only**, and the approval gates this
      merge. If D3's amendment lands first, this PR extends it.
    - Add `scripts/secrets_mgmt/host_secrets.py` with `check` and `sync`, and the
      `git_dates.py` changes *Which ciphertext each host carries* names. Wire `check` into the
      pytest job as a test, because that job checks out full history.
    - Make the unattended path of `secret_rotation.py:cmd_rotate` skip every key a host file
      carries.
    - Extend `.gitleaks.toml`'s SOPS path allowlist to `ansible/vars/hosts/.*\.sops\.yaml`.
    - `docs/secret-rotation.md` gains the host-file copy rule and the **operator-only** `sync`
      step in its hand-rotation and `assisted` runbooks.
    - No host file exists yet, so the check's live-tree members start at H4.2. Its fixtures
      prove each failure mode here.
    - No host changes.
7. **H4.2 daniel-server's age key, SOPS file and bring-up (about 120 lines).**
    - Add step 3 to `hosts/bootstrap-host.sh`: it creates `/etc/homelab/age.key` if missing
      and prints the public key.
    - `docs/break-glass.md` gains daniel-server's `/etc/homelab/age.key`, its restore, and
      the loss rule. A lost or exposed host key means rotating every value that host held,
      not only re-keying the host. The rotation list is derived, not remembered: every
      value in every SOPS file whose creation rule in `ansible/.sops.yaml` lists that host's
      public key.
    - Add the creation rule for `ansible/vars/hosts/daniel-server.sops.yaml` ahead of the
      general rule. Its recipients are the daniel-server, daniel-box and recovery keys that
      `ansible/.sops.yaml` already lists.
    - **Operator-only:** run `host_secrets.py sync`, which decrypts `secrets.yml` and writes
      daniel-server's file holding `ups_secondary_daniel_server_push_token`. Commit it in
      this PR.
    - Add `hosts/daniel-server/package.yml` with the `reconcile` and `secrets-unit` components,
      the enable link for `homelab-updater.timer`, and one credential,
      `ups_secondary_daniel_server_push_token`, which nothing reads yet.
    - Add `homelab-daniel-server` to the named sets of both CI scans, and daniel-server to the
      hosts `host_secrets.py check` must find.
    - **The updater is armed from the first version.** H4.3a and H4.3c rely on the timer.
      There is no daniel-server Depends gate. H3.7 proved apt's handling of an added
      `Depends` through `homelab-updater`, which is the same package and code on every host.
      The one `Depends` this slice adds, `nut-client` at H4.3c, is installed already.
    - **Landing.** The builder publishes before daniel-server has the package, so
      `homelab_host_applied_info{host="daniel-server"}` is absent and H3.9's host wait ends
      `deferred` at its deadline. That deadline comes from the shared timer's schedule,
      because daniel-server ships no updater drop-in (*Open questions* 5). That is the
      expected result, and the operator's bring-up follows.
    - **Gate:** H3's `homelab-tools` build for `amd64` is published, because this PR ships
      the first `secrets-unit` component to a host.
    - **Operator-only**, after merge, on daniel-server:
        1. copy the key from `~ubuntu/.config/sops/age/keys.txt` to `/etc/homelab/age.key`
           (root 0600) instead of generating one. The recipient lists stay unchanged;
        2. run `sudo hosts/bootstrap-host.sh daniel-server`, which installs
           `homelab-updater` and `homelab-daniel-server`. Reconcile's step 2 then starts
           `homelab-updater.timer`.
8. **H4.3a Render daniel-server's `upsmon.conf`, read by nothing (about 110 lines).**
    - Add the `nut-secondary` component with only its declared output: `host-upsmon.conf.j2`
      from the role's templates, `persist: true`, root:nut 0640, with no dependent.
    - Add the persisted-output census with its one-member list (*Persisted outputs*). Its
      member first renders in this PR, so the census lands here rather than in H4.1a3.
    - **Operator-only:** run `host_secrets.py sync`, which adds `nut_monitor_password` to
      daniel-server's file. `check` fails the PR until it does.
    - Two-writer check: no Ansible task writes under `/var/lib/homelab-secrets/`.
    - After the updater installs it, the operator compares
      `sudo sha256sum /etc/nut/upsmon.conf /var/lib/homelab-secrets/etc/nut/upsmon.conf`.
      The step is **operator-only** because both files are readable only by root and `nut`.
      The command prints digests, never content. A mismatch stops H4.3c.
9. **H4.3b1 Shared pieces: the `kuma-push` library and the udev reconciler (about 150
   lines). A Pi and daniel-server host change.** This PR keeps the Pi package's churn out of
   the shutdown-chain PR.
    - Add the `kuma-push` component, list it in both packages, and drop
      `/usr/lib/homelab/kuma-push-lib.sh` from `pi-heartbeats`. On the Pi the file keeps its
      path and its package, so dpkg keeps it in place.
    - Add the renderer test that flags two components in one package shipping one path.
    - Add reconcile's udev reconciler arm (H3's step 5), with stub tests. No package ships a
      udev rule yet, so the arm makes no `udevadm` call on any host. A stub case with no rule
      asserts exactly that and exit 0, and a case that drops a rule asserts no call
      (*Verification*).
    - **Landing.** The arm edits `hosts/lib/homelab-reconcile`, which every host package
      ships. The builder therefore republishes `homelab-daniel-pi` and
      `homelab-daniel-server`, and H3.9's host wait waits on both hosts. The agent reads both
      hosts' `homelab_host_applied_info` and `homelab_host_reconcile_ok` through Prometheus,
      as for H4.1b.
10. **H4.3b2 One reader for a role's leading `end_role` guard (about 180 lines).**
    - Add the helper to `scripts/deploy_tools/setup_lib/setup_gates.py`. It returns the `when:` of a
      role's leading `meta: end_role` task, and evaluates it per host with
      `eval_when_strict`.
    - H3's two-writer census, `land_reach.setup_role_hosts` and
      `glance_facts.py:setup_glance_lines` read it (*The role guard after this slice*).
    - Red fixtures for each reader. No role carries the guard yet, so no result changes on
      the live tree.
11. **H4.3b3 The watchdog's packaged branch (about 150 lines).**
    - Add the packaged branch to `ups-secondary-health.sh.j2`, and the two role defaults.
    - Add the one-template-source test and the packaged-render test (*Verification*).
    - No package renders the branch yet. On merge, the tick applies `nut_host` on
      daniel-box. The rendered files stay byte-identical there, so the apply changes only the
      `nut-host` stamp, because the template text changed.
    - **daniel-server owes a hand apply.** No role carries the `end_role` guard until H4.3c,
      so `nut_host` still applies to daniel-server, and its stamp records the old template
      hash.
        - `setup-drift-lib.sh` arm 3 compares that stamp with the template in daniel-server's
          checkout. `Setup Plane Drift (agent hosts)` therefore reads DOWN from its first run
          that sees the edited template until the apply.
        - The landing note names this apply already:
          `scripts/deploy_tools/tests/test_land_nut_host_reaches_both_hosts.py::test_the_landing_owes_daniel_server_the_same_run`
          pins it at `70591c307`.
        - **Operator-only**, after merge, on daniel-server, from its own checkout at the merge
          commit or later: `uv run ansible-playbook ansible/initial_setup.yml --tags nut_host -e target=daniel-server`.
          It rewrites the byte-identical config, env file and script, runs the udev reload
          and trigger that every apply runs, and refreshes the stamp. The tile reads UP on
          its next run.
12. **H4.3c NUT on daniel-server (about 200 lines).** This one PR applies the handoff rule.
    - Add the rest of the component: the drop-in, the dependent, the udev rule, the watchdog
      service and timer, and the legacy entries. The watchdog renders `domain` from inventory,
      so daniel-server's SOPS file does not change and no `sync` runs.
    - In the role, delete the udev tasks and add the `end_role` guard.
      `ansible/initial_setup.yml` stays untouched.
    - Rewrite `test_the_usb_half_is_confined_to_the_ups_host` and the two `land_reach` tests
      (*Tests and docs that change in the same PR*).
    - Two-writer check: the census sees `nut_host` applying only to daniel-box. daniel-box
      keeps the Ansible-era paths, because those are not in its package (it has none until
      H6).
    - On merge, the tick applies `nut_host` on daniel-box. The rendered files stay
      byte-identical there, so the apply changes only the `nut-host` stamp.
    - Rollout on daniel-server, within 5 minutes of the builder publishing:
        1. dpkg unpacks, and the postinst reloads systemd. The running upsmon keeps the
           config it loaded at start.
        2. The SOPS file and the credentials are unchanged since H4.3a, so reconcile's secrets
           step does not restart `homelab-secrets`, and `pending.json` stays empty.
        3. Step 2 starts `homelab-ups-watchdog.timer`, which is enabled, inactive and outside
           the restart set.
        4. Step 3 runs `reset-failed` on `nut-monitor`, then restarts it once, because its
           drop-in is added. The existence gate passes on the persisted file. upsmon then
           reads it through `NUT_CONFPATH`, and the operator's environ read from exit
           criterion 1 prints `1` (**operator-only**).
        5. Step 4 retires the cron, the script, the env file, the old udev rule and the stamp
           fragment.
13. **H4.4 The Pi's own SOPS file (about 80 lines).**
    - **Operator-only:**
        - run step 3 of `bootstrap-host.sh` on the Pi;
        - add the creation rule for `ansible/vars/hosts/daniel-pi.sops.yaml` ahead of the
          general rule, with the Pi's printed key, daniel-box's key and the recovery key;
        - run `host_secrets.py sync`, which writes the file holding `pi_sd_health_push_token`,
          `pi_recovery_push_token`.
    - Add daniel-pi to the hosts `host_secrets.py check` must find.
    - `docs/break-glass.md` gains the Pi's `/etc/homelab/age.key`, that the Pi is a
      recipient of its own file only, and H4.2's loss rule. A lost SD card is a lost host
      key, so the derived rotation list covers every value in the files the Pi's key
      decrypts.
14. **H4.5 The Pi switches to `/run/credstore` (about 60 lines).**
    - **Gate:** H3's `homelab-tools` build for `arm64`, the Pi pilot, is published.
    - Add the `secrets-unit` component to the Pi package.
    - Repoint both `LoadCredential=` lines of the two heartbeat units, one per push token, at `/run/credstore/<key>`, with `After=` and `Wants=` on
      `homelab-secrets.service`.
    - Keep the interim files and their `optimize_pi` tasks. Nothing is a legacy entry yet.
15. **H4.6 Retire the interim files (about 40 lines).**
    - Start it only after both Pi tiles have read UP for a day on the H4.5 version. The
      agent reads that from `monitor_status` (*Verification*). The operator's
      `ssh daniel-pi systemctl is-active homelab-secrets` must also print `active`
      (**operator-only**: the agent has no login on the Pi).
    - Delete the interim tasks from `optimize_pi`, and list `/etc/homelab/interim/` as a
      legacy entry. Reconcile retires it only once both credentials exist in
      `/run/credstore`.
    - The tick records a `manual_plane` line for `optimize_pi`, because the role is gated
      onto the Pi (Baseline). No apply is needed: the deleted tasks only wrote the interim
      files, and reconcile retires those. Once the Pi reports this SHA, the operator clears
      the line with `gitops_state.py clear-owed manual_plane optimize_pi --applied <tags>`
      (**operator-only**), naming the tags the row lists, as H3.8b does. If the row holds
      another PR's change too, the operator applies that first.

## Verification

The agent runs the daniel-server block, the Prometheus block and the `gh` block. It has a
restricted login on daniel-server and none on the Pi (Baseline). `probe.py monitors`
decrypts `domain`, so the agent reads the tiles from Prometheus instead, through the
loopback-then-ClusterIP transport H3 uses. The operator runs every `sudo` command and every
command on the Pi. Each ssh call is one batched, read-only command, which keeps it under the
ssh rate limiter.

```bash
# Agent, H4.2 to H4.3c on daniel-server: one batched read-only call.
ssh daniel-server 'dpkg-query -W homelab-daniel-server nut-client; systemctl is-active homelab-secrets nut-monitor homelab-ups-watchdog.timer homelab-updater.timer; systemctl show -p DropInPaths -p NRestarts nut-monitor; upsc apc-ups@127.0.0.1 ups.status; ip -4 -o addr show cni0; upsc -c apc-ups@127.0.0.1; stat -c "%a %U %G %n" /var/lib/homelab-secrets /var/lib/homelab-secrets/etc /var/lib/homelab-secrets/etc/nut /var/lib/homelab-secrets/etc/nut/upsmon.conf; ls /etc/cron.d/ups-secondary-health /usr/local/bin/ups-secondary-health.sh /etc/nut/kuma-push.env /etc/udev/rules.d/99-nut-apc.rules 2>&1'

# Agent: both UPS Secondary tiles and, from H4.5, both Pi heartbeat tiles. The fallback
# address is observability_prometheus_cluster_ip.
Q='monitor_status{job="uptime-kuma",monitor_name=~"UPS Secondary.*|Daniel Pi (SD Health|Recovery)"}'
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$Q" \
  || curl -s --max-time 5 http://10.43.39.218:9090/api/v1/query --data-urlencode "query=$Q"

# Agent, after each PR that republishes a host package (H4.1b and H4.3b1 included): each
# host's applied SHA and reconcile verdict.
Q2='{__name__=~"homelab_host_applied_info|homelab_host_reconcile_ok",host=~"daniel-pi|daniel-server"}'
curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode "query=$Q2" \
  || curl -s --max-time 5 http://10.43.39.218:9090/api/v1/query --data-urlencode "query=$Q2"

# Operator-only, on daniel-server and, from H4.5, on the Pi: the credential store is root
# 0700. find runs under sudo, because an unprivileged shell cannot expand a glob there.
sudo find /run/credstore -exec stat -c '%a %U %n' {} +

# Operator-only, H4.3c on daniel-server: the running upsmon carries NUT_CONFPATH. Take <pid>
# from `systemctl show -p MainPID --value nut-monitor`. It prints a count, never the environment.
sudo cat /proc/<pid>/environ | tr '\0' '\n' | grep -c -x 'NUT_CONFPATH=/var/lib/homelab-secrets/etc/nut'

# Operator-only, H4.5 and H4.6 on the Pi.
ssh daniel-pi 'systemctl is-active homelab-secrets; systemctl list-timers "homelab-pi-*" --no-pager; ls /etc/homelab/interim 2>&1'

# Agent. CI: the host_packages job in ci.yml (workflow "CI") passed on the PR's head commit.
gh run list --workflow ci.yml --branch <pr-branch> --limit 3
gh run view <run-id> --json jobs --jq '.jobs[] | select(.name | startswith("host_packages")) | .conclusion'
```

The expected results are:

- `upsc` prints `OL` or another valid status, and `upsc -c` lists the `cni0` address printed
  just before it;
- `homelab-updater.timer` reads `active`;
- `DropInPaths` names `50-homelab.conf`;
- the persisted path reads `755 root root` for each directory and `640 root nut` for the
  file;
- the operator's `find` reads `700 root` for `/run/credstore` and `600 root` for each file;
- the operator's environ read prints `1`;
- for each host whose package the PR republished, `homelab_host_reconcile_ok` reads 1 and
  `homelab_host_applied_info` names the merge SHA or a descendant. At H4.1b that host is the
  Pi, and at H4.3b1 it is both hosts;
- the legacy paths report `No such file or directory`;
- both `UPS Secondary` series, and from H4.5 both Pi heartbeat series, read 1;
- after H4.6, `/etc/homelab/interim` reports `No such file or directory`.

**Shutdown-chain proof.** The upsmon config moved, so the chain is drilled once after H4.3c
and before the slice closes.

- *Pre-drill check, which powers nothing off.* On daniel-server, `upsc -c
  apc-ups@127.0.0.1` lists the host's `cni0` address. From daniel-box, the same query against
  the `nut` ClusterIP also lists daniel-box's `flannel.1` address. Both secondaries are
  therefore logged in.
- *The drill, **operator-only**.* It needs a maintenance window and console access to both
  nodes. The operator runs `kubectl -n homelab exec deploy/nut -- upsmon -c fsd`, the drill
  `ansible/roles/k8s/nut/CLAUDE.md` names in *Timer vs. safety net*. The pod's primary sets
  FSD, and both secondaries power their hosts off within about 15 s. daniel-box is the
  control plane, so the whole cluster goes down with it. After power-on, exit criterion part 1
  and both tiles must read as above.
- The command is a `kubectl exec`, which the agent's read-only ServiceAccount cannot run.
  Running `upsmon -c fsd` on daniel-server's host would not exercise the chain, because
  `fsd` acts only on primary-mode UPSes and that upsmon is a secondary.

**Red proofs for the checks this slice adds.**

- *gitleaks over package roots.* A fixture root holding a fake token in gitleaks' default
  format is flagged. The real roots, with their `ENC[` ciphertext, are clean. The check
  asserts that it scanned exactly its named set, and fails naming any member it did not
  find.
- *Placeholder check.* A fixture template naming `${no_such_key}` is flagged, and the real
  trees are clean. It asserts the same named set.
- *The host renderer (H4.1a3).* Renderer tests in `hosts/lib/tests/`:
    - a declared output renders its secret reference as `${<key>}`, escapes its literal
      `$$` and `${` by C1's rule, and installs under `secret-templates/` with its symlink or
      drop-in;
    - a fixture compose component's compose file keeps its placeholders at its real path
      under `/usr/lib/homelab/compose/`, while a declared output in the same component is
      routed;
    - an undeclared template that reads a secret fails the render and names the key. The
      test asserts the raise, because `lib.render_guard.BASE_CONTEXT` stubs `domain` as
      `example.com`, and a strip that missed the stub would render it;
    - the host context carries `inventory_hostname`. A fixture render with it removed is
      flagged by the component test below;
    - a fixture package whose `package.yml` marks a unit `windowed`, and whose declared
      output lists that unit as a dependent, fails the render and names the unit. The same
      package without the mark renders.
- *Persisted outputs (H4.3a).* The census has two red fixtures. A fixture component that
  declares an unlisted output with `persist: true` fails it and names the output. A fixture
  list whose member no package renders also fails it, so a renamed output cannot shrink the
  census to nothing.
- *`homelab-secrets` itself.* Unit tests with a stub `sops` cover these cases:
    - a missing key fails that output, writes the others, and exits non-zero;
    - a failed decrypt writes and deletes nothing, and a persisted output keeps its content;
    - the round trip: a template whose literal text holds `$$`, a shell `${VAR}` and a `$`
      directly before a placeholder goes through the renderer's escape and the script's
      single pass. The output holds that literal text byte for byte, with the placeholder's
      value in place. In the template, `$${` is never written by hand; the renderer's escape
      writes it, and it comes out as `${`. A fixture script that replaces `$${` first and
      `${<key>}` second is flagged on the `$$` case and the `$`-before-placeholder case;
    - an unescaped `${` that does not close as `${<name>}`, or a placeholder the output's
      key list does not name, fails that output;
    - an output no longer listed is deleted, under both roots;
    - an unchanged value adds nothing to `pending.json`;
    - a run after a simulated reboot, which empties `/run` and keeps
      `/var/lib/homelab-secrets/`, adds nothing;
    - a changed value adds only its own dependents, and keeps the entries already pending;
    - with no HMAC recorded, every output's dependents are added;
    - the directories above an output are root 0755, and the file has its listed mode.
- *Reconcile.* Stub-`systemctl` tests drive the H4.3c transition:
    - the calls run in the order `restart homelab-secrets`, `reset-failed nut-monitor`,
      `restart nut-monitor`, none of them with `--no-block`, and `nut-monitor` restarts
      exactly once. A fixture call log that restarts `nut-monitor` before
      `homelab-secrets`, or passes `--no-block` to either restart, is flagged;
    - a failed but enabled dependent is restarted, not try-restarted. One case is a foreign
      unit whose stub `is-enabled` prints `enabled`. The other is a `homelab-*` unit whose
      enable link the manifest ships while its stub `is-enabled` prints `disabled`;
    - one call per unit per run: a failed, enabled fixture service whose hash changed, in
      the shape of H5's `homelab-compose`, gets exactly one `start` or `restart` in the
      run, after a `reset-failed`. A fixture call log in which step 2 starts it and step 3
      restarts it is flagged;
    - a failed, enabled unit with no hash change that has hit its start limit: step 2 runs
      `reset-failed` before its `start`, and the start succeeds. A fixture step 2 that
      starts without the reset is flagged;
    - a fixture manifest marks a foreign unit `windowed`. The unit's stub `is-enabled`
      prints `enabled`, its stub `is-active` prints `activating`, its drop-in hash changed,
      and `pending.json` lists it. Reconcile makes no `reset-failed`, `start` or `restart`
      call that names it, drops it from `pending.json`, and exits 0. A fixture step 2
      without the `windowed` skip is flagged;
    - a pending unit that is neither enabled nor active is not restarted, and leaves
      `pending.json`;
    - a run killed after the secrets step leaves `nut-monitor` in `pending.json`. The next
      run's `homelab-secrets` adds nothing, and that run still restarts `nut-monitor` once.
      A fixture reconcile that reads only what its own `homelab-secrets` run added is
      flagged;
    - the secrets step fails while the persisted `upsmon.conf` exists, and `nut-monitor`'s
      drop-in hash changed: `nut-monitor` restarts once on the persisted file, the legacy
      entries stay, and reconcile exits non-zero;
    - the secrets step fails on a fresh host with no persisted output, and `nut-monitor`'s
      drop-in hash changed: `nut-monitor` is neither started nor restarted, lands in
      `pending.json`, the legacy entries stay, and reconcile exits non-zero. The next run,
      with the output written, restarts it once although no hash changed. A fixture
      reconcile that drops a skipped hash-change unit without queuing it is flagged;
    - a component whose credential is missing from `/run/credstore` keeps its legacy entries;
    - a manifest without the `secrets-unit` component, the Pi's shape until H4.5. The case starts
      from an `applied.json` in H3's format, with one legacy entry unretired, one changed
      active unit, and no `/run/homelab-secrets/`. Reconcile makes no `systemctl` call that
      names `homelab-secrets`, retires the legacy entry, restarts the unit once after
      `reset-failed`, and exits 0. A fixture reconcile that runs the secrets step on every
      manifest is flagged: its stub `systemctl restart homelab-secrets.service` fails, and
      the run exits non-zero;
    - the udev arm (H4.3b1). A manifest with no rule makes no `udevadm` call and exits 0. A
      manifest that adds a rule, or changes its hash, runs `udevadm control --reload-rules`
      and then `udevadm trigger --subsystem-match=usb`. A manifest that drops a rule makes no
      `udevadm` call. A fixture arm that triggers on a dropped rule is flagged.
- *The host name in the render context, and the watchdog's token.* A component test in
  `hosts/lib/tests/` renders daniel-server's package and asserts two things:
    - field 2 of the rendered `upsmon.conf` MONITOR line is `apc-ups@127.0.0.1`. The test
      parses field 2 only, as the watchdog's extractor does;
    - the watchdog unit's `LoadCredential=` source names the token that
      `test_nut_host_secondary.py:TILES` maps to daniel-server.

  A render built without `inventory_hostname` is flagged on both assertions, because it
  yields `apc-ups@` and `ups_secondary_push_token`. A fixture unit naming
  `ups_secondary_push_token` is flagged too.
- *The packaged watchdog reads no secret (H4.3b3).* A component test renders
  `ups-secondary-health.sh.j2` in host mode for daniel-server with the packaged branch on.
  The strict render succeeds with every secret stripped, and the watchdog unit loads
  `kuma_push_token` alone. A fixture template that reads a SOPS key inside the packaged branch
  is flagged: the host-mode render raises and names the key.
- *One template source (H4.3b3).* A test in `ansible/tests/setup/` asserts that daniel-box's
  role render and daniel-server's component render resolve the same two template files under
  `ansible/roles/setup/nut_host/templates/`. It also asserts that daniel-box's render of
  each is byte-identical to its render at the merge base, which catches whitespace that the
  new `{% if %}` blocks would add.
- *The USB half (H4.3c).* The rewritten test in `test_nut_host_secondary.py` asserts that no
  `nut_host` task writes under `/etc/udev/rules.d/` or runs `udevadm`. A fixture task list
  holding the old udev task is flagged. A component test asserts that daniel-server's package
  ships `/usr/lib/udev/rules.d/99-homelab-nut-apc.rules` with the role's rule text.
- *Boot order on the rendered units.* A test on daniel-server's rendered package asserts that
  the drop-in carries `Wants=` and `After=` on `homelab-secrets.service` and no `Requires=`,
  and that `NUT_CONFPATH` points under `/var/lib/homelab-secrets/`. In the CI container,
  `systemd-analyze verify` passes on the rendered units.
- *Nothing waits on reconcile.* A census in `hosts/lib/tests/` reads every rendered unit and
  drop-in in every host package. It asserts that none carries
  `After=homelab-reconcile.service`, and that `homelab-secrets.service` names
  `homelab-reconcile.service` in no ordering directive. It lands in H4.1c, and its named
  members grow with the packages: `homelab-reconcile.service` from H4.1c, which every host
  package ships, `homelab-secrets.service` from H4.2, and `nut-monitor`'s drop-in from H4.3c.
  It fails naming any member it did not find. A fixture unit ordered after reconcile is
  flagged.
- *NUT in the CI container.* The container installs `nut-client` and `nut-server` from the
  archive, then seeds `/etc/nut/nut.conf` with `MODE=netclient` and a fixture
  `/etc/nut/upsmon.conf`, as the Ansible role leaves them. The seeded `upsmon.conf` holds the
  working fixture password.
    - The seeded file logs in. An upsmon that ignored `NUT_CONFPATH` would read it and stay
      listed. The wrong-password case below therefore goes red only when upsmon reads the
      packaged file through `NUT_CONFPATH`.
    - The test upsd and the `dummy-ups` driver run from their real binaries, which the job
      finds with `dpkg -L nut-server`, under their own `NUT_CONFPATH`. The job never starts
      them through the vendor units or the `/sbin` wrappers. `/sbin/upsmon` gates on `MODE`
      in `/etc/nut/nut.conf` (Baseline), and that file holds the client's `netclient`. The
      server-side wrappers were not inspected, because daniel-box has no `nut-server`, so
      the job does not depend on how they read `MODE`.
    - upsd listens on the container's loopback, which the rendered `127.0.0.1` endpoint
      reaches.

  With the fixture ciphertext, the cases are:
    - install: `nut-monitor` is active and reads the fixture config through `NUT_CONFPATH`;
    - login: `upsc -c` lists the container's upsmon. A fixture ciphertext with a wrong
      `nut_monitor_password` keeps it off the list while the seeded `/etc/nut/upsmon.conf`
      still holds the working one. That proves exit criterion part 1 can go red, and that
      upsmon reads its config through `NUT_CONFPATH`;
    - reload: after `systemctl reload nut-monitor`, the journal shows no error naming the
      config path. A fixture with `etc/nut/` at root 0700 shows one;
    - upgrade N to N+1, keeping the component: `nut-monitor` restarts at most once;
    - upgrade to a version without the component: `/etc/nut/upsmon.conf` still holds the
      seeded content, and `nut-monitor` is active on it through the vendor unit;
    - `apt-get install --reinstall nut-client` with the drop-in in place: `nut-monitor` is
      active on the persisted config afterwards;
    - age key removed, on an upgrade that changes `nut-monitor`'s drop-in:
      `homelab-secrets` fails, `nut-monitor` restarts once on the persisted file, and
      reconcile exits non-zero. This matches the reconcile stub case;
    - age key removed on a first install, with no persisted file: `nut-monitor` keeps
      running on the config it loaded, is not restarted, and is listed in `pending.json`.
- *`host_secrets.py check` (H4.1d).* These fixtures are flagged:
    - a host file with an extra key, or one missing a key its tree names;
    - a host key whose newest change does not descend from the newest change of that key in
      `secrets.yml`. One case puts both commits on the same day, which day-level dates would
      pass. Another has the registry's `last_rotated` lag git;
    - a key for which git returns no commit;
    - a shallow clone, where `git rev-parse --is-shallow-repository` prints `true`.

  A host file whose `sync` rewrote ten or more keys at once is clean, because the host-file
  walk runs without the re-encrypt guard. The live files are clean. The test names the
  members it must find: daniel-server's file from H4.2, holding
  `ups_secondary_daniel_server_push_token`, then `nut_monitor_password` from H4.3a; and the
  Pi's file from H4.4, holding `pi_sd_health_push_token` and `pi_recovery_push_token`. An empty
  host list or a missing key cannot pass.
- *No unattended rotation of a host-file key (H4.1d).* A `secret_rotation.py` test runs the
  unattended path over a fixture registry. An `auto`-tier key with consumer tags that a host
  file carries is skipped with its reason. A fixture `cmd_rotate` that rotates it is
  flagged.
- *The guard reader (H4.3b2).* For a fixture role whose first task is `meta: end_role` with
  `when: inventory_hostname == ups_host`:
    - H3's two-writer census sees it applying to daniel-box only;
    - `land_reach.setup_role_hosts` returns daniel-box only, and
      `land_reach.remaining_setup_hosts_note` does not name daniel-server;
    - `glance_facts.py:setup_glance_lines` names the guard on its *Applied by* line.

  The same fixture without the guard reaches daniel-server in all three readers, so each
  assertion fails on it. After H4.3c, the live `nut_host` reads as the guarded fixture does.
- *One path per package.* A renderer test in `hosts/lib/tests/` flags a fixture package in
  which two components both ship `/usr/lib/homelab/kuma-push-lib.sh`.

## Rollback

- **H4.1a1, H4.1a2, H4.1a3 and H4.1c:** revert. They touch no host.
- **H4.1b:** revert. The revert edits reconcile again, so the builder republishes
  `homelab-daniel-pi` with H3's reconcile, and H3.9's host wait waits on the Pi. The agent
  reads `homelab_host_reconcile_ok{host="daniel-pi"}` back at 1 (*Verification*). If the Pi
  already holds, it installs nothing newer until `--clear-hold` succeeds (H3), so the
  operator follows H3's hold runbook in `docs/host-packages.md` (**operator-only**).
- **H4.1d:** revert. No host file exists yet. The ADR stays as the record; if H4 is
  abandoned, a later ADR supersedes it.
- **H4.2:** revert, then the operator runs
  `sudo apt-get remove homelab-daniel-server homelab-updater` on daniel-server. The revert
  removes daniel-server's SOPS file and its creation rule. The copied age key at
  `/etc/homelab/age.key` stays until deleted by hand.
- **H4.3a:** revert. The next version lacks the output, so `homelab-secrets` deletes the
  persisted file. Nothing read it. The revert also restores daniel-server's SOPS file
  without `nut_monitor_password`, which `check` accepts, because the tree no longer names
  it.
- **H4.3b1:** revert. The library moves back into `pi-heartbeats` at the same path, so dpkg
  keeps the file on the Pi, and daniel-server loses a file nothing reads yet. The udev arm
  had nothing to act on. The revert edits reconcile again, so the builder republishes both
  host packages, and the landing waits on both hosts as the forward PR's did.
- **H4.3b2:** revert. No role carries the guard yet, so no reader's result changes.
- **H4.3b3:** revert. The tick applies `nut_host` on daniel-box, which changes only its
  stamp. Nothing rendered the packaged branch. daniel-server's stamp is stale again, so the
  operator repeats H4.3b3's apply there (**operator-only**), and
  `Setup Plane Drift (agent hosts)` reads DOWN until then.
- **H4.3c:** apply first, then revert. This order keeps a udev rule for the APC device on
  daniel-server at every moment, and rewrites the vendor unit's config before upsmon reads
  it again.
    1. **Operator-only**, before the revert merges: on daniel-server, from a checkout of the
       revert PR's head commit, run
       `uv run ansible-playbook ansible/initial_setup.yml --tags nut_host -e target=daniel-server`.
        - At that commit the role has its udev tasks and no `end_role` guard. The apply
          writes `/etc/udev/rules.d/99-nut-apc.rules`, then reloads and triggers udev.
        - It rewrites `/etc/nut/nut.conf` and `/etc/nut/upsmon.conf` with the current
          `nut_monitor_password`, so a rotation after H4.3c leaves no stale password there.
        - It restores the cron, the script, the env file and the stamp.
        - Reconcile does not remove those files again. H3's reconcile repeats no action that
          succeeded (*The reconcile unit*), and it retired those legacy entries at H4.3c.
        - Until the revert lands, the cron and the package timer both push daniel-server's
          tile, which is harmless. `nut-monitor` keeps reading the persisted file through
          the drop-in.
    2. The revert merges. It does not touch `ansible/initial_setup.yml`, so it parks
       nothing. The tick applies `nut_host` on daniel-box, which changes only its stamp. The
       landing note names daniel-server again, and step 1 has already run that apply.
    3. The builder publishes a version without the drop-in, the timer and the packaged udev
       rule. Reconcile restarts `nut-monitor` once, onto the vendor unit, which reads the
       `/etc/nut/` files that step 1 rewrote. dpkg removes the packaged rule, and the udev
       arm makes no `udevadm` call for a dropped rule (*NUT on daniel-server*, udev). The
       Ansible rule from step 1 is the one in force.
    - **If step 1 is skipped or fails**, daniel-server has no udev rule for the APC device
      once the revert lands. The next udev event for the device, or the Sunday reboot, then
      applies no `MODE="0666"` to it. The signals for that window are the `nut` pod's tile,
      `k3s nut (upsd)`, and `UPS Secondary (daniel-box)`, whose watchdog pushes `down` when
      `upsc` returns an empty `ups.status` from the pod. `UPS Secondary (daniel-server)` goes
      DOWN at its 1200 s deadline, because no watchdog runs there.
- **H4.4:** revert. The Pi file and its creation rule go. The Pi keeps its interim files,
  because H4.6 has not run.
- **H4.5:** revert. The heartbeats point back at `/etc/homelab/interim/`, which still holds
  both files.
- **H4.6:** revert, then the operator re-applies H3.8a's tags on the Pi to recreate the
  interim files, and clears the `manual_plane` line the revert records with
  `--applied` naming those tags (**operator-only**).

**What a partial failure leaves live:**

- **`homelab-secrets` fails at boot**, for a bad key or a missing `sops`.
    - `nut-monitor` starts anyway on the persisted `upsmon.conf`, so the chain still works.
    - The watchdog's credential is missing, so `homelab-ups-watchdog.service` fails at start
      and pushes nothing. The tile goes DOWN at its 1200 s deadline.
    - After the Sunday reboot that deadline falls inside the Kuma maintenance window, so the
      page arrives when the window closes, about 45 minutes after the reboot.
    - The Pi heartbeats fail the same way.
- **One output's key is missing.** That output fails and the others are written.
  Reconcile restarts a dependent of that output only while its previous file still exists,
  queues the rest in `pending.json`, and keeps the component's legacy entries.
- **Reconcile fails after H4.3c unpacks but before it restarts `nut-monitor`.** upsmon keeps
  the config it loaded at start. The legacy cron and the timer then both push
  daniel-server's own tile, which is harmless.
- **Reconcile dies between its secrets step and its restarts**, for example at its 30-minute
  `TimeoutStartSec`. The dependents that the secrets run queued stay in `pending.json`. The
  updater holds the host, and the reconcile run that
  `sudo /usr/lib/homelab/homelab-updater --clear-hold` starts restarts them. A reboot
  instead empties `pending.json` and starts every dependent after `homelab-secrets`.

## Risks

- **The shutdown chain is the highest-consequence config on the fleet.**
    - H4.3a renders the file before anything reads it, and the operator compares digests.
    - H4.3c moves one host, and the endpoint stays `127.0.0.1`.
    - The operator runs the drill before the slice closes.
    - The watchdog tile is the runtime signal.
- **Failure class 2, the check observes a proxy.** `upsc ups.status` talks to the pod's upsd
  and never reads `upsmon.conf`, and `systemctl is-active` stays `active` for an upsmon whose
  login fails. The watchdog reads only those two. The exit criterion therefore adds
  `upsc -c`, which lists daniel-server only after its login passes.
    - A login alone does not show which file upsmon read, because `/etc/nut/upsmon.conf`
      holds the same credentials as the packaged file. After a later `nut_monitor_password`
      rotation the two files differ, and an upsmon that read the wrong one would fail its
      login with both watchdog reads still green.
    - Two checks close that gap. The CI container seeds a working `/etc/nut/upsmon.conf`
      beside a wrong packaged password, and the operator's environ read shows
      `NUT_CONFPATH` on the running upsmon.
- **H4.1b and H4.3b1 change the live Pi.** Every host package ships reconcile, so each edit
  to it republishes the Pi package (H3 *Shared names* and *Package rules*). The secrets rules
  key on the `secrets-unit` component, which the Pi lacks until H4.5. The stub case of a manifest
  without `secrets-unit` is the red proof. A reconcile failure there holds the Pi, and H3.9's
  landing reports it as `deploy-failed`.
- **H4.3c's rollback removes the packaged udev rule.** The rollback restores the Ansible rule
  first, and the udev arm never triggers on a dropped rule.
- **A decrypt failure at boot.** Persisting `upsmon.conf` makes `nut-monitor`'s start
  independent of the decrypt, which runs every week through the reboot cron. The persisted
  file has the exposure `/etc/nut/upsmon.conf` already has: root:nut 0640 at rest. It is
  the one output C5's persisted-output rule admits here (*Persisted outputs*), and the
  census refuses a second one that carries no reason.
- **The drop-in calls the daemon directly.** A nut-client release that moves
  `/lib/nut/upsmon` would break the start. The watchdog's `is-active` check reports it. The CI
  container case that reinstalls `nut-client` runs against the archive's current version.
- **The Ansible-era config stays at rest.** `/etc/nut/upsmon.conf` keeps the last
  Ansible-rendered credentials, root:nut 0640, the same exposure it has before this slice.
  It is the rollback path, and H7.8's design decides its end.
- **Copying the age key.** H4.2 copies daniel-server's key rather than adding a
  recipient. One key then lives at two paths on one host. H7 removes the user-home copy when
  `sops_setup` retires.
- **Coupling between `secrets.yml` and the host packages.** A byte copy of `secrets.yml` in
  each package would publish a new package for every host on every secrets commit, and make
  each `/add-secret` PR wait on every host's updater in `land.sh` (*Which ciphertext each host
  carries*). This slice takes the per-host file instead: a host package changes only when
  one of its own keys changes. The cost is an **operator-only** `sync` in each PR that adds a
  key to a host's tree or rotates one a host file carries. `check` makes a missed `sync` fail
  that PR's CI.
- **Value drift in a host file.** These keys rotate only by hand. `host_secrets.py check`
  gates the rotation PR in CI by commit ancestry rather than by registry or calendar dates,
  and refuses a shallow clone. The unattended rotation path skips every host-file key, so no
  `--no-verify` cron commit can leave a host file behind `secrets.yml`.
- **Failure class 7, the execution context.** The renderer is not Ansible. Ansible sets
  `inventory_hostname` per host, and `render_context` sets nothing in its place (Baseline).
  A render for daniel-server without it would take the non-`ups_host` branch of every role
  default: an empty upsd host, and daniel-box's push token on daniel-server's watchdog.
  Host mode sets the variable. The component test asserts the MONITOR endpoint and the
  credential name, and a render without the variable is its red fixture. H4.3a's digest
  compare is the second stop, before anything reads the file.
- **Failure class 3, empty read as clean.** `homelab-secrets` fails on a missing key, unlike
  Flux's empty substitution. `host_secrets.py check` fails on a key with no commit and on a
  shallow clone. The gitleaks and placeholder checks fail on a missing package root.
- **Failure class 4, selector drift.** The two CI checks assert a named set of package
  roots. A renamed or missing package therefore fails them, rather than shrinking what they
  scan to nothing.
- **Failure class 5, sequences treated as transactions.** Reconcile retires the legacy
  cron and udev rule only after the package's units are active and its secrets exist. The
  handoff is package-first by construction. The secrets run and the restarts it owes are two
  steps as well. Reconcile drops a unit from `pending.json` only after acting on it, and
  queues every unit it skipped or failed. A run that dies between the steps, or fails a
  step and still writes `applied.json`, therefore loses no restart.
- **A unit started twice in one run.** H3's step 2 would start a failed, enabled unit that
  step 3 then restarts, which doubles a long `homelab-compose` run against reconcile's
  30-minute limit. Step 2 skips every unit in the restart set, and the stub test fails on a
  call log with two calls for one unit.
- **A third restarter of k3s.** Without the `windowed` skip, H3's step 2 would start an
  enabled k3s that reads `activating`, outside the window and behind none of H6's gates.
  Its unit's infinite start timeout would hold every reconcile run until the 30-minute
  kill. Step 2 skips a `windowed` unit, and a stub case with a red fixture pins it. The two
  restarters H6 adds exclude each other through their own gates (*The restart contract*).
- **Failure class 6, restart is not recreate.** `LoadCredential=` is read at unit start. The
  heartbeat and watchdog services are oneshots that start fresh every run, so a rotated token
  takes effect on the next run.
- **Failure class 8, prose asserts facts nothing re-derives.** The watchdog's token name is
  pinned to `TILES` by a test, not stated in prose.
- **A stale landing note.** Until H4.3b2, `land_reach` read only the play gate, which still
  names `ups_host`. Without the guard reader, every `nut_host` landing would tell the
  operator to run a hand apply on daniel-server that does nothing. H4.3b2 makes the landing,
  the census and the glance block read the same guard.

## Tests and docs that change in the same PR

- **H4.1a1:** `hosts/lib/tests/` covers the render path of `homelab-secrets`, including the
  round trip.
- **H4.1a2:** `hosts/lib/tests/` covers the HMAC baseline and `pending.json`.
- **H4.1a3:** `hosts/lib/tests/` gains the renderer tests: declared outputs, the compose
  exemption, an undeclared secret read, `inventory_hostname` in the host context, and an
  output whose dependent is `windowed`.
- **H4.1b:**
    - `hosts/lib/tests/` covers the reconcile secrets step, the restart set, step 2's skip
      and reset, step 2's `windowed` skip, the existence gate, and the `pending.json` drain
      and refill. It also covers a manifest without `secrets-unit`, starting from an
      `applied.json` in H3's format;
    - H3's reconcile test that expects a changed service to be try-restarted expects
      `reset-failed` and `restart` instead, and its step 2 test expects `reset-failed`
      before `start`;
    - `docs/host-packages.md` (H3) states the restart contract, the hand path
      `systemctl start homelab-reconcile`, and that reconcile neither starts nor restarts a
      `windowed` unit. The two restarters of such a unit do not exist yet, so H6.3b adds
      them and their mutual exclusion to that page.
- **H4.1c:** the CI container cases cover the fixture key; `hosts/lib/tests/` gains the
  census that no unit waits on reconcile; the two scans carry their red fixtures.
- **H4.1d:**
    - the ADR amending ADR-0003;
    - `scripts/secrets_mgmt/tests/` covers `host_secrets.py`, the commit-returning walk and
      the unattended skip;
    - `docs/secret-rotation.md` gains the host-file copy rule and the `sync` step;
    - `.gitleaks.toml` gains the per-host path.
- **H4.2:** `docs/break-glass.md` gains daniel-server's `/etc/homelab/age.key` and the loss
  rule; the
  `host_secrets.py check` test gains daniel-server as a member it must find.
- **H4.3a:** a component test asserts the output's mode, owner and `persist` flag; the
  persisted-output census joins `hosts/lib/tests/` with its two red fixtures.
- **H4.3b1:**
    - a renderer test in `hosts/lib/tests/` flags two components shipping one path;
    - H3's heartbeat component tests stop expecting `kuma-push-lib.sh` in `pi-heartbeats`;
    - `hosts/lib/tests/` covers the udev reconciler arm, with the no-rule and dropped-rule
      cases.
- **H4.3b2:** red fixtures for the guard reader in the two-writer census
  (`ansible/tests/setup/`), in `scripts/deploy_tools/tests/` for `land_reach`, and in
  `scripts/docs/tests/` for the glance line.
- **H4.3b3:** `ansible/tests/setup/` gains the one-template-source test, and
  `hosts/lib/tests/` the packaged-render test. `test_nut_host_secondary.py` keeps its
  textual assertions, which hold on the daniel-box branch.
- **H4.3c:**
    - `ansible/tests/setup/test_nut_host_secondary.py` keeps its daniel-box assertions.
      `test_the_usb_half_is_confined_to_the_ups_host` becomes the assertion that no
      `nut_host` task writes under `/etc/udev/rules.d/` or runs `udevadm`.
    - Component tests in `hosts/lib/tests/` cover the daniel-server half: the host name and
      the token, the udev rule the package ships, and the boot order.
    - In `scripts/deploy_tools/tests/test_land_nut_host_reaches_both_hosts.py`,
      `test_nut_host_reaches_daniel_box_and_daniel_server` expects daniel-box only, and
      `test_the_landing_owes_daniel_server_the_same_run` asserts that the note does not
      name daniel-server. The module docstring and the two test names change with them.
    - `ansible/roles/setup/nut_host/CLAUDE.md` says the role ends on `ups_host` and why, and
      that the `ups_host` half is package-owned. Its generated glance block is regenerated,
      and its *Applied by* line names the guard.
    - `ansible/roles/k8s/nut/CLAUDE.md` updates its pointer to the host half and its
      *Editing* section, which tells the reader to apply `nut_host` on both hosts.
    - `scripts/secrets_mgmt/secret_bearing_host_paths.py` and its test stay unchanged,
      because the task that installs `/usr/local/bin/ups-secondary-health.sh` still applies
      to daniel-box.
- **H4.4:**
    - `docs/break-glass.md` gains the Pi's key and the loss rule;
    - the `host_secrets.py check` test gains daniel-pi, with its three keys, as members it
      must find.
- **H4.5:** H3's heartbeat component tests change their three `LoadCredential=` assertions.
- **H4.6:** H3's two-writer census sees no `optimize_pi` task writing `/etc/homelab/interim/`.

## Complexity delta

- **Added:**
    - one shared unit and its script;
    - a reconcile step, one start-and-restart rule, and the `pending.json` queue it drains
      and refills;
    - a `windowed` skip in reconcile's step 2, and a renderer refusal of a `windowed`
      dependent;
    - declared secret outputs in the renderer, with host-mode placeholders, C1's escape and
      a persisted output root that holds one output;
    - one persisted-output census with a one-member list;
    - a `Depends: homelab-tools` on the `secrets-unit` component;
    - `inventory_hostname` in the host render context;
    - two per-host SOPS files, one each for daniel-server and the Pi, and the `sync` step
      each key change costs;
    - `host_secrets.py`, and a commit-returning sibling of `ciphertext_rotation_dates`;
    - gitleaks over artifacts;
    - one drop-in on `nut-monitor`;
    - one udev reconciler arm;
    - one `end_role` guard, one reader for it shared by three callers, and two role
      defaults.
- **Not added:** a byte copy of `secrets.yml` in any package, and the per-secret landing
  waits it would have caused on every packaged host. No read-only dependency on the
  Ansible-installed `sops`, and no persisted output on the Pi.
- **Removed on daniel-server:** the udev tasks, the cron, the env file, the stamp and the
  0700 script. daniel-box keeps all but the udev tasks until H7.8.
- **Removed on the Pi:** H3's interim credential files and their tasks, and the
  0700-because-it-embeds-a-token rule for two Pi scripts.

## Open questions

1. **The design names an hc-ping as the exit signal.** The design text says "the hc-ping
   arrives". At `70591c307` the UPS watchdog pushes to the Kuma tile through
   `kuma-push-lib.sh`, and no hc-ping exists on that path. This plan uses the Kuma push.
   Resolution: if M6 retires Kuma before H4.3c, the exit signal becomes the M-area replacement
   for that tile.
2. **Where does `sops` come from?** Resolved by the C5 amendment that the pillar review
   fold applied. `homelab-tools` is the one architecture-specific package, and H3 builds it
   for `amd64` and `arm64`. The `secrets-unit` component depends on it and calls its `sops` by
   absolute path (*`homelab-secrets.service`*). H4.2 and H4.5 each wait on the build for
   their host's architecture.
3. **Does upsmon honour `NUT_CONFPATH` on daniel-server's nut-client?** The string is present
   in `/lib/nut/upsmon` on daniel-box, which is evidence, not proof. Resolution: the CI
   container cases above prove it, with only `upsmon.conf` in the directory, for start and
   for `systemctl reload`. Before H4.3c merges, `ssh daniel-server dpkg-query -W nut-client`
   must print the version those cases ran. If the versions differ, three checks on
   daniel-server stand in:
    - after H4.3a has rendered the file, `ssh daniel-server grep -c NUT_CONFPATH /lib/nut/upsmon`
      prints a non-zero count;
    - after H4.3a, the operator's `sudo -u nut test -r /var/lib/homelab-secrets/etc/nut/upsmon.conf`
      succeeds, which proves the reload path can read the file. This is **operator-only**,
      because it needs root;
    - after H4.3c's restart, the operator's environ read from exit criterion 1 prints `1`
      (**operator-only**). A `0` starts H4.3c's rollback.

   The first two do not show which file the running process read. The environ read runs in
   every case, because it is part of exit criterion 1. The H4.3c PR body records every
   output.
4. **C5 names one output location.** Resolved by the pillar review fold (2026-10-12). C5
   carries the three locations, the symlink and drop-in routes, one per-host SOPS file per
   host package, and the rule that a persisted output needs a consumer that must start
   before the decrypt can run. H4.1a1's gate is met. *Persisted outputs* lists the one output
   this slice persists, with its reason.
5. **The H3, H5 and H6 mechanisms that this slice changes.** *The restart contract* is the
   one rule for starting and restarting a unit on the host plane, and this slice owns it. H3,
   H5 and H6 state rules that conflict with it or extend it, so their text changes.

   In H3:
    - *Ordering on the secrets unit* says `homelab-secrets` restarts its dependents.
      Reconcile restarts them instead, from `pending.json`.
    - Reconcile step 3 runs `try-restart` on a service whose hash changed. It adopts the
      restart set instead: for a unit that is enabled or active, under the contract's
      definitions, `reset-failed` and then a blocking `restart`. H3's reconcile test changes
      to match.
    - Reconcile step 2 starts every enabled unit that is not active. Under the contract it
      builds the restart set first, skips every unit in it, and runs `reset-failed` before
      each `start`. Without the skip, a failed, enabled unit whose hash changed is started in
      step 2 and restarted in step 3, and a unit at its start limit fails step 2 before step
      3's reset could recover it. H5's *Open questions* 5 raised the first case.
    - Step 2 also skips a unit the manifest marks `windowed`, as step 3 already does. H3's
      `windowed` exemption names step 3 only. Without the step-2 skip, reconcile would start
      an enabled k3s that reads `activating` (*The restart contract*).
    - Steps 2 and 3 start or restart a unit only when its outputs and credentials exist, and
      queue a unit they skip or fail in `pending.json`. Step 6 writes `applied.json` even
      after a failed step, so the queue is what carries the owed restart.
    - *Host mode renders no secret* gains two exceptions: a component's declared outputs and
      its compose files render secret references as `${<key>}` with C1's escape (*Secret-bearing
      files*). Every other host file keeps the rule. This waits on the C1 amendment in
      *Open questions* 6.
    - Host mode sets `inventory_hostname` to the host it renders, as an override, in H3.1's
      host context. `render_context` sets none (Baseline), so without it every
      `inventory_hostname` comparison in a host render is false. H3's *Component variables*
      adds only a defaults layer, which does not supply it.
    - H3.9's land host wait derives its deadline from the host's rendered updater drop-in.
      daniel-server ships none and runs the shared timer's schedule, 5 minutes after boot
      and every 5 minutes. A host with no drop-in therefore takes that default schedule for
      its deadline. H4.2's landing, which ends `deferred` at that deadline, relies on it.
    - *Package rules* let `preinst` and `postrm` run `dpkg-divert` lines from H4 on. This
      slice ships no diversion, because nut-client owns both NUT files as conffiles
      (Baseline). The divert lines leave H3's maintainer-script whitelist, and a later slice
      that needs a diversion adds the lines and their lifecycle itself.
    - Handoff rule 3 also waits for the component's secrets and credentials to exist.
    - *Shared names* gain the persisted root `/var/lib/homelab-secrets/`, the queue
      `/run/homelab-secrets/pending.json`, and the per-host SOPS file
      `ansible/vars/hosts/<host>.sops.yaml`, shipped as
      `/usr/lib/homelab/secrets/<host>.sops.yaml`.
    - `component.yml` gains a defaults-source field and an `outputs:` declaration, which the
      renderer honours.
    - `kuma-push-lib.sh` moves from `pi-heartbeats` into a shared `kuma-push` component.
    - The two-writer census evaluates a role's leading `end_role` guard.
    - The `host_packages` job gains `sops`, the fixture-key build and the two scans.

   In H5:
    - *Restart ordering* has `homelab-secrets` queue `systemctl --no-block try-restart`
      calls, and has reconcile issue every `try-restart` in one call. Under the contract,
      `homelab-secrets` calls no `systemctl`, and reconcile restarts `homelab-compose` in its
      per-unit loop, after `reset-failed`. A failed `homelab-compose` is therefore restarted
      once its enable link ships.
    - `homelab-compose` runs behind `homelab-secrets` because of sequence, not because one
      transaction holds both: the secrets step's blocking call returns before step 3
      starts. H5's test that reconcile issues its restarts in one call becomes a test that
      `restart homelab-secrets` returns before `reset-failed homelab-compose` is called.
    - The list of what starts `homelab-compose` names reconcile's restart set, fed by a
      compose file's hash or by `pending.json`, in place of `homelab-secrets`.
    - H5.2a's quiet window still holds: reconcile skips a unit that is neither enabled nor
      active, so an unenabled `homelab-compose` does not run.
    - A blocking `restart` of `homelab-compose` can take its whole derived
      `TimeoutStartSec`, 1390 s or about 23 minutes (H5, *The wait timeout*). Reconcile's
      30-minute limit covers one such call, and step 2's skip keeps it to one call per run. A run killed at that limit never writes `applied.json`,
      and it has dropped from `pending.json` only the units it restarted, so the next run
      owes the rest.
    - *The inputs line* and *What starts it* name `changed.json`. The queue is
      `pending.json`.
    - H5's *Open questions* 5 asks four things of H4. The answers:
        - the routing rule exempts compose files, which install at their real path under
          `/usr/lib/homelab/compose/` (*Secret-bearing files*). H4.1a3's renderer test pins
          it;
        - `host_secrets.py check` keeps running on every PR rather than only on PRs that
          touch the Pi file or `ansible/secret_rotation.yml`. A stale host file is a fact of
          the tree, a rotation PR that skips `sync` fails itself, and the unattended rotation
          path skips host-file keys, so master never carries a stale file;
        - the hand path restarts the owed dependents. Reconcile reads `pending.json` on every
          run, whoever ran `homelab-secrets` last, so its secrets step skipping an unchanged
          inputs hash loses nothing;
        - step 2 no longer starts a unit that step 3 restarts in the same run (the H3 item
          above).

   In H6:
    - *`homelab-k3s-restart`* says H4 makes reconcile the only process that starts or
      restarts a unit, and its *Open questions* 8 asks H4 to name two more restarters.
      *The restart contract* now names SUC's upgrade container and `homelab-k3s-restart`,
      each with its condition, and their mutual exclusion. That text is the gate on H6.3b,
      which H6.3b's step list carries. H6's text keeps its citation and drops the "only
      process" paraphrase.
    - Where H6 says reconcile never restarts `k3s` or `k3s-agent`, the rule is that
      reconcile neither starts nor restarts them, through the step-2 skip above.

   Resolution: H3's and H5's text is amended to match before H4.1b merges, except the two
   renderer items, which gate H4.1a3. H6's text is amended before H6.3b merges. H7's *In-place edits* cites a divert pattern from H4,
   which this slice no longer has, so H7 defines that lifecycle itself.
6. **C1 forbids host-mode placeholders.** C1's *Secret references, cluster mode only* says
   that host and package modes render no secret and keep every `${` literal, and its escape
   rule is cluster mode only. This slice needs host mode to render a secret reference as
   `${<key>}` in two kinds of file, a component's declared outputs and its compose files, and
   to escape those files by the cluster-mode rule. Every other host file keeps C1's rule.
   Resolution: amend C1 before H4.1a3 merges, together with H3's *Host mode renders no secret*
   (*Open questions* 5). contracts.md's *Requested amendments not yet applied* still does not
   list this request after integration round 2 (X2-11). The round c1 review and that round
   hand the row to contracts.md. The row must stand in the register before H4.1a3 merges,
   and H4.1a3's gate says so.
7. **The other hosts' ciphertext.** This slice ships per-host SOPS files for daniel-server and
   the Pi. H6 ships daniel-box's package with `secrets-unit`, and H7's rebuild runbook adds a
   rebuilt host's key as a recipient. Resolution: H6 adds
   `ansible/vars/hosts/daniel-box.sops.yaml` with the keys its tree names, and its
   `add-secret` step for `k3s_readonly_token` gains the **operator-only** `sync`. H7's
   runbook re-encrypts the rebuilt host's own file to its new key. Both slices confirm before
   their first PR that ships ciphertext.

## Review log

### Round 1 (per-slice adversarial review)

Raised 19 findings: 1 critical, 7 high and 11 medium. All 19 were fixed. Refuted: none.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H4-R1-01 | critical | The watchdog wired daniel-box's token, not `ups_secondary_daniel_server_push_token` | Fixed |
| H4-R1-02 | high | H4.3 edited `ansible/initial_setup.yml`, which parks the deployer | Fixed |
| H4-R1-03 | high | `git mv` of the NUT templates stranded daniel-box's half | Fixed |
| H4-R1-04 | high | The diversion lifecycle broke upgrades and rollback, and diverted conffiles | Fixed |
| H4-R1-05 | high | The auto-tier cron never rotates the Pi or UPS tokens | Fixed |
| H4-R1-06 | high | The shutdown drill was misdescribed | Fixed |
| H4-R1-07 | high | A tmpfs-only `upsmon.conf` made every boot depend on a decrypt | Fixed |
| H4-R1-08 | high | The H4.3 restart sequence was unspecified and restarted twice | Fixed |
| H4-R1-09 | medium | Parent directory modes were unstated, and reloads drop root | Fixed |
| H4-R1-10 | medium | Exit criterion 1 checked the pod's upsd, not the rendered config | Fixed |
| H4-R1-11 | medium | Host decryption began before its ADR and break-glass docs | Fixed |
| H4-R1-12 | medium | The gitleaks allowlist covered only `secrets.yml` | Fixed |
| H4-R1-13 | medium | The package-root checks named no member they must find | Fixed |
| H4-R1-14 | medium | No daniel-server component shipped `kuma-push-lib.sh` | Fixed |
| H4-R1-15 | medium | H4.5 could delete the Pi's credentials before a decrypt | Fixed |
| H4-R1-16 | medium | `host_secrets.py check` read registry dates that lag git | Fixed |
| H4-R1-17 | medium | The CI container tests could not decrypt | Fixed |
| H4-R1-18 | medium | The C5 extension was not flagged for amendment | Fixed |
| H4-R1-19 | medium | The census and the complexity delta ignored daniel-box | Fixed |

How each was fixed, with the evidence measured at `70591c307`:

- **R1-01.** `static-monitors.yaml.j2` and `test_nut_host_secondary.py:TILES` confirm one
  token per host. The watchdog loads its key through the role default
  `nut_host_watchdog_token_key`, and a component test pins it to `TILES` with a red fixture
  that uses the daniel-box token.
- **R1-02.** `deploy_changes.py:_BROAD_MANUAL_PREFIXES` lists `ansible/initial_setup.yml`.
  The plan takes option (a): an `end_role` guard inside the role, which the tick applies on
  daniel-box as a no-op. The two-writer census reads that guard, with a red fixture.
- **R1-03.** Four tasks in `nut_host/tasks/main.yml` apply to daniel-box. The component
  renders from the role's template paths, nothing moves until H7.8, and a test asserts that
  both renders resolve the same files.
- **R1-04.** `dpkg-query` on daniel-box lists both files as nut-client conffiles. The plan
  drops the diversions and uses a `NUT_CONFPATH` drop-in. `/lib/nut/upsmon` carries the
  variable, and `/sbin/upsmon` is a shell wrapper around it. The CI container covers the
  upgrade, drop and `nut-client` reinstall cases.
- **R1-05.** `consumers.py:CROSS_HOST_PUSH_TOKENS` holds all four push tokens, and
  `cmd_rotate` skips them. `sync` becomes an operator-only runbook step, and the cron wiring is
  dropped.
- **R1-06.** The drill is the operator-only `kubectl exec` from
  `ansible/roles/k8s/nut/CLAUDE.md`, which powers off both nodes. A pre-drill `upsc -c` check
  was added.
- **R1-07.** The output persists under `/var/lib/homelab-secrets/`, and outputs render
  independently. The page timing is corrected to the 1200 s deadline, and to the maintenance
  window after the Sunday reboot. A boot-order test was added.
- **R1-08.** Reconcile owns every restart. It runs `homelab-secrets` synchronously on an
  inputs hash, then runs `reset-failed` and `restart` once per unit. A stub test includes the
  red ordering case.
- **R1-09.** Parent directories are root 0755, and the CI container proves a reload.
- **R1-10.** `upsc -c` joins the exit criterion and the Verification block. The check matches
  daniel-server's `cni0` address, derived on the host. A CI container case with a wrong
  password proves the check can go red. Failure class 2 is named in Risks.
- **R1-11.** H4.2 carries the ADR, approved before merge, and the break-glass entry for
  daniel-server. The Pi's entry is in H4.4.
- **R1-12.** `.gitleaks.toml` gains the per-host and package-ciphertext paths.
- **R1-13.** Both checks assert a named set of package roots.
- **R1-14.** A shared `kuma-push` component ships the library, and a renderer test flags two
  components shipping one path. Both land in H4.3b, ahead of the shutdown-chain PR.
- **R1-15.** H4.6 retires the interim files after H4.5 has run. Reconcile gates legacy
  entries on the credentials existing.
- **R1-16.** The check compares git dates through `ciphertext_rotation_dates`, and fails on a
  missing date.
- **R1-17.** Each container test generates an age key and builds with a fixture ciphertext.
  The container gets `sops` at `sops_setup_version`.
- **R1-18.** Open question 4 amends C5.
- **R1-19.** The census and its pin stay unchanged, and the complexity delta separates
  daniel-server from daniel-box.

### Round 2 (integration review)

Raised 2 findings: 1 high and 1 medium. Both were fixed in this slice. The H3, H5 and H7
halves of X-09 are text changes in those slices, recorded in *Open questions* 5. Refuted:
none.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| X-09 | high | Unit-restart ownership contradicts itself across H3, H4 and H5, so a failed compose unit is never restarted on the Pi | Fixed |
| X-18 | medium | H4 extends a `host-packages.yml` workflow that H3 deliberately does not create | Fixed |

How each was fixed, with the evidence measured at `70591c307`:

- **X-09.** The finding holds. H5's *Restart ordering* has `homelab-secrets` queue
  `--no-block try-restart` calls. H3's reconcile step 3 also uses `try-restart`, which does
  nothing to a failed unit. This plan's reconcile used a per-unit `reset-failed` and
  `restart` loop. The fix has four parts.
    - The Design section is now *The restart contract*, which names one owner and the text
      H3 and H5 adopt. *Open questions* 5 lists each sentence that changes in H3 and H5, and
      the `dpkg-divert` lines that leave H3's whitelist. H7's divert reference goes too.
    - H5's ordering argument is re-derived under the per-unit loop. The order comes from the
      blocking secrets call, which returns before step 3 starts. A blocking call adds one
      hazard, a unit ordered after reconcile itself, so a census on the rendered units
      forbids that ordering.
    - Re-deriving the argument exposed a lost restart. `changed.json` was emptied on every
      run, and any run advanced the per-boot HMACs. A reconcile run that died between its
      secrets step and its restarts therefore lost the restart for good: the next run's
      `homelab-secrets` found nothing changed. `pending.json` is now a queue that reconcile
      drains per unit, and the HMAC baseline persists under `/var/lib/homelab-secrets/`, so
      the boot run queues nothing. A stub test kills a run between the steps.
    - *Enabled* is defined so that a failed homelab unit is still restarted. On daniel-box,
      which runs systemd `255.4-1ubuntu8.17`, `systemctl is-enabled
      remote-cryptsetup.target` prints `disabled`. That unit has `WantedBy=multi-user.target`
      in its `[Install]` section, and its only link is
      `/usr/lib/systemd/system/initrd-root-device.target.wants/remote-cryptsetup.target`.
      systemd therefore does not count a vendor-path link as enablement. H3 ships every
      enable link under `/usr/lib/systemd/system/<target>.wants/` (*Shared names*), so
      reconcile reads a homelab unit's enablement from the manifest. For a foreign unit,
      `systemctl is-enabled nut-monitor.service` on daniel-box prints `enabled`.
- **X-18.** The finding holds. H3's *CI (builds, never signs)* makes the host-package checks
  the `host_packages` job in `.github/workflows/ci.yml`, whose `name:` is `CI`. At
  `70591c307`, `ls .github/workflows/` prints `ci.yml`, `image-smoke.yml` and
  `renovate-config-canary.yml`. The tree diff, H4.1b, exit criterion part 4 and
  *Verification* now name that job, and the `gh` commands read the `ci.yml` runs and the
  job's conclusion. The two scans also moved ahead of H3's early exit for an unchanged tree.
  Otherwise a PR that changes only `.gitleaks.toml` would skip them.

### Round 10 (per-slice adversarial review)

Raised 16 findings: 1 critical, 5 high and 10 medium. All 16 were fixed. Refuted: none. Each
was checked against the tree at `70591c307` before the fix.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H4-R10-01 | critical | The renderer never sets `inventory_hostname`, so daniel-server's `upsmon.conf` renders an empty upsd host and the watchdog key falls back to daniel-box's token | Fixed |
| H4-R10-02 | high | Secret-template routing needs host mode to emit C1 placeholders, which C1 and H3 forbid | Fixed |
| H4-R10-03 | high | The packaged watchdog still renders `{{ domain }}`, a SOPS key | Fixed |
| H4-R10-04 | high | H4 treated the Pi's interim credentials as two, but H3 ships three, `domain` included | Fixed |
| H4-R10-05 | high | H3's step 2 could start a failed unit that step 3 restarts again in the same run | Fixed |
| H4-R10-06 | high | Verification had an agent run a decrypting `probe.py monitors` and Pi ssh commands | Fixed |
| H4-R10-07 | medium | H4.3c turned `test_the_usb_half_is_confined_to_the_ups_host` red unlisted | Fixed |
| H4-R10-08 | medium | `land.sh` and the glance generator read the play gate, not the `end_role` guard | Fixed |
| H4-R10-09 | medium | H4.6 never handled the `manual_plane` line the tick records for `optimize_pi` | Fixed |
| H4-R10-10 | medium | No step said when daniel-server's package ships the updater-timer enable link | Fixed |
| H4-R10-11 | medium | A byte copy of `secrets.yml` republished daniel-server's package on every secrets change | Fixed |
| H4-R10-12 | medium | The existence gate covered only pending units, and two tests contradicted each other | Fixed |
| H4-R10-13 | medium | "A shallow clone cannot pass" was false, and day-level dates passed a same-day stale copy | Fixed |
| H4-R10-14 | medium | The routing rule contradicted the compose exemption H5 depends on | Fixed |
| H4-R10-15 | medium | Several PRs far exceeded the 100–200 line guide | Fixed |
| H4-R10-16 | medium | The CI NUT container started upsd through wrappers that may gate on `MODE=netclient` | Fixed |

How each was fixed, with the evidence measured at `70591c307`:

- **R10-01.** Reproduced in the worktree. `render_context` on `host-upsmon.conf.j2` with
  `host='daniel-server'` resolved `nut_host_upsd_host` to `''`, with and without
  `strict=True`, and reported no `inventory_hostname` in the context. With
  `overrides={'inventory_hostname': 'daniel-server'}` it resolved `127.0.0.1`. The Baseline
  now states this. Host mode sets `inventory_hostname` as an override in H4.1a3, and
  *Open questions* 5 records the H3 change. A component test asserts the MONITOR endpoint
  and the watchdog's credential name, with a render without the variable as its red fixture.
  Risks names failure class 7.
- **R10-02.** C1 limits secret references to cluster mode, and H3 strips secrets in host mode
  and renders strictly. *Secret-bearing files* now routes by declaration: a component lists
  its outputs, and only those, plus compose files, render placeholders with C1's escape.
  `homelab-secrets` inverts the escape in one left-to-right pass, and the unit test's
  round trip covers a literal `$$`, a shell `${VAR}` and a `$` before a placeholder.
  *Open questions* 6 requests the C1 amendment before H4.1a3, and *Open questions* 5 lists
  the H3 change.
- **R10-03.** `ups-secondary-health.sh.j2` holds `KUMA_HOST="uptime-kuma.local.{{ domain }}"`,
  and `grep -c '^domain: ENC\['` on `secrets.yml` prints 1. The packaged branch reads
  `$CREDENTIALS_DIRECTORY/domain`, a fourth differing line. The unit loads `domain`, and
  `package.yml` and daniel-server's SOPS file list it. The packaged-render test asserts a
  strict render with secrets stripped, and an inline `{{ domain }}` fixture must raise.
- **R10-04.** H3.8a writes three interim files, `domain` among them, and H3.8b's units load
  all three. The Baseline, the Pi file's contents, H4.4, H4.5, H4.6, Rollback and the
  `check` test's named members now carry three keys.
- **R10-05.** H3's *The reconcile unit* step 2 starts every enabled unit that is not active.
  The contract now builds the restart set before step 2, which skips the set's units and
  resets each unit before its start. A stub test asserts one call per unit per run, with a
  red call log. *Open questions* 5 lists the H3 amendment and answers H5's item.
- **R10-06.** `probe_lib/monitors.py` imports `core.py:prom_endpoint`, which reaches
  `core.py:sops_extract('domain')`. The agent now reads `monitor_status` from Prometheus on
  loopback, with `observability_prometheus_cluster_ip` as the fallback. The age-key blocker
  goes away once domain PR 1 of R2's *Publish `domain`* item lands (operator ruling of
  2026-10-14). The Pi ssh block
  and the H4.6 precondition are the operator's. `docs/claude-agent-user.md` gives the agent
  a restricted login on daniel-server, so that block stays the agent's. The credential-store
  check uses `sudo find`, because the old `sudo stat /run/credstore/*` glob expands in an
  unprivileged shell that cannot list the directory.
- **R10-07.** `test_the_usb_half_is_confined_to_the_ups_host` asserts
  `usb, "expected the udev tasks to still exist"`. H4.3c rewrites it as an assertion that
  no `nut_host` task writes under `/etc/udev/rules.d/` or runs `udevadm`, paired with a
  component test that the package ships the rule.
- **R10-08.** `land_reach.setup_role_hosts` evaluates only the playbook gate when a role has
  one, and `glance_facts.py:setup_glance_lines` prints that gate. The fix takes option one:
  H4.3b2 adds one guard reader in `setup_gates.py`, used by the census, `land_reach` and the
  glance line, with red fixtures. H4.3c updates the two tests in
  `test_land_nut_host_reaches_both_hosts.py`.
- **R10-09.** `deploy_defer.py:unapplyable_setup_roles` returns `optimize_pi`, which is gated
  onto the Pi. H4.6 now has the operator-only `clear-owed manual_plane optimize_pi --applied`
  step, in H3.8b's form, and says no apply is needed.
- **R10-10.** H4.2 ships the enable link from daniel-server's first version. No daniel-server
  Depends gate is needed: H3.7 proved the shared updater's `Depends` handling, and the only
  `Depends` added, `nut-client`, is installed. H4.2's own landing ends `deferred` until the
  operator's bootstrap, which the step states.
- **R10-11.** H3's *Package rules* publishes on any tree change, and H3.9's host wait targets
  every host whose tree differs. The fix takes option one: every host ships a per-host SOPS
  file. Risks and *Complexity delta* state the choice and its `sync` cost. `host_secrets.py`
  moved ahead of H4.2 as H4.1d. *Open questions* 7 hands daniel-box's file to H6.
- **R10-12.** The existence gate now covers every start or restart in steps 2 and 3, and a
  skipped or failed unit is queued in `pending.json`. H3's step 6 writes `applied.json` even
  after a failed step, so without the queue a skipped hash-change unit would lose its
  restart. The stub tests and the container cases now agree, and a fresh-host case was
  added.
- **R10-13.** `ciphertext_rotation_dates` ends with a `setdefault` loop that dates every
  remaining key at the oldest visible commit, and `_revisions` uses `--date=short`. `check`
  now compares commit ancestry through a commit-returning sibling, refuses a shallow clone,
  and walks a host file without the whole-file re-encrypt guard. The false prose claim is
  gone.
- **R10-14.** H5's *Secrets* and its *Open questions* 5 ask for the compose exemption. The
  routing rule now treats compose files as a declared second kind that installs at its real
  path, and H4.1a3's renderer test pins both kinds.
- **R10-15.** Every PR states a size. H4.1a became H4.1a1 to H4.1a3, the old H4.1b became
  H4.1b and H4.1c, and H4.3b became H4.3b1 to H4.3b3, which takes the watchdog's template
  branch out of H4.3c. The ids `H4.1b` and `H4.2`, which H3, H6 and H7 cite, keep their
  meaning.
- **R10-16.** `/sbin/upsmon` on daniel-box is a wrapper that gates on `MODE`, and
  `nut-server` is not installed there, so `/sbin/upsd` could not be inspected. The container
  now starts the test upsd and driver from the binaries `dpkg -L nut-server` lists, under
  their own `NUT_CONFPATH`, never through the vendor units or wrappers.

### Round c1 (per-slice adversarial review)

Raised 5 findings: 1 high and 4 medium. Four were fixed here. H4-C1-05 needs an edit to
contracts.md, so it is handed to that page. Refuted: none. Each was checked against the
tree at `70591c307`, or against H3 and contracts.md in this worktree, before the fix.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| H4-C1-01 | high | H4.1b changes the reconcile script that the live Pi package ships, and its secrets step as written holds the Pi | Fixed |
| H4-C1-02 | medium | R1-10's fix does not discriminate: `upsc -c` passes whether upsmon reads the packaged file or the byte-identical /etc copy | Fixed |
| H4-C1-03 | medium | H4.3b3 leaves daniel-server's nut-host render stamp stale, so its setup-drift check reports drift until H4.3c or a hand apply | Fixed |
| H4-C1-04 | medium | The H4.3c rollback removes the packaged udev rule before the hand apply restores the Ansible rule | Fixed |
| H4-C1-05 | medium | H4's C1 and C5 amendments are missing from contracts.md's register of requested amendments | Handed to contracts.md |

How each was handled, with the evidence:

- **C1-01.** H3's *Shared names* says `homelab-reconcile.service` ships in every host
  package. H3.6 gives the Pi package the `reconcile` component, and H3's *Package rules*
  publishes on any change to a rendered tree. So H4.1b republishes `homelab-daniel-pi`, and
  H4.3b1's udev arm does the same to both packages. The restart contract now keys the
  secrets step, the existence gate and the legacy-entry gate on the manifest's
  `secrets_inputs_sha256`, which the renderer writes only for a package that ships
  `secrets-unit`. Reconcile creates the queue directory itself. A stub case of a manifest without
  `secrets-unit`, from an `applied.json` in H3's format, asserts no `homelab-secrets` call and
  exit 0, with an unconditional secrets step as its red fixture. H4.1b and H4.3b1 are
  restated as host changes with an H3.9 landing, a Prometheus read of both hosts'
  `homelab_host_reconcile_ok` and `homelab_host_applied_info`, and a Rollback that
  republishes. H4.1a3 keeps "No host changes": no template under
  `ansible/roles/setup/optimize_pi/templates/` reads `inventory_hostname`.
- **C1-02.** The CI container now seeds `/etc/nut/upsmon.conf` with the working fixture
  password, so the wrong-password case goes red only when upsmon honours `NUT_CONFPATH`. An
  **operator-only** read of the running upsmon's `/proc/<pid>/environ` prints a count of
  the `NUT_CONFPATH` line. It is part of exit criterion 1, H4.3c's rollout, Verification,
  the failure-class-2 risk and *Open questions* 3's fallback.
- **C1-03.** `setup-drift-lib.sh` arm 3 hashes `${REPO_DIR}/${tpl}` against the stamp,
  `setup_drift_check_hosts` names daniel-server, and `nut_host/CLAUDE.md` says the check
  reports drift until the role is re-applied. H4.3b3 keeps its scope and adds the
  **operator-only** `--tags nut_host` apply on daniel-server, which the landing note
  already names
  (`scripts/deploy_tools/tests/test_land_nut_host_reaches_both_hosts.py::test_the_landing_owes_daniel_server_the_same_run`).
  The tile `Setup Plane Drift (agent hosts)` reads DOWN until then. The template branch did
  not move into H4.3c, which would undo R10-15's split. The Rollback mirrors the apply.
- **C1-04.** `nut_host/tasks/main.yml` writes the rule with `MODE="0666"`, and H4.3c retires
  `/etc/udev/rules.d/99-nut-apc.rules` as a legacy entry. The Rollback now runs the
  **operator-only** apply from the revert PR's head commit before the revert merges. That
  restores the Ansible rule, and it rewrites `/etc/nut/upsmon.conf` with the current
  password, which closes the watchdog gap and the stale-password gap. The udev arm makes no
  `udevadm` call for a dropped rule, and a stub case pins it. The signals if the apply is
  skipped are `k3s nut (upsd)` (`static-monitors.yaml.j2`, `nut-k8s.json`) and
  `UPS Secondary (daniel-box)`, whose watchdog pushes `down` on an empty `ups.status`.
- **C1-05.** contracts.md's *Requested amendments not yet applied* lists rows for C1 image
  contexts, C2, C3, C5 signature trust, C7, the slice index and the document format, and
  none for H4. This slice may not edit contracts.md. The two rows are handed over: C1
  host-mode placeholders for declared outputs and compose files, gated before H4.1a3
  merges, and C5 output locations, consumer access and per-host ciphertext, gated before
  H4.1a1 merges. *Open questions* 4 and 6 say the register lacks them.

### Round integration-2 (integration review)

Raised 2 findings against this slice, both medium. X2-15 was fixed here. X2-11's H4 part
was fixed here as far as this page reaches, and the two register rows are handed to
contracts.md again. Refuted: none. Each claim was checked against the tree at `70591c307`,
against H3, H5, H6 and contracts.md in this worktree, or against a read-only command on
daniel-box.

| ID | Severity | Title | Outcome |
|---|---|---|---|
| X2-11 | medium | The register and slice index lack about 20 requested rows and sub-edges that slices name as merge gates | H4's part fixed; rows handed to contracts.md |
| X2-15 | medium | H4's restart contract still names reconcile as the only restarter; H6.3b is gated on an amendment H4 lacks | Fixed |

How each was handled, with the evidence:

- **X2-11.** Confirmed for H4. contracts.md's *Requested amendments not yet applied* holds
  nine rows, and none is H4's C1 or C5 row. H4.1a3 already named its C1 gate, but H4.1a1
  named no gate, although *Open questions* 4 required the C5 amendment before it. Both PRs
  now carry a gate line: the row stands in the register, or the amendment is applied,
  before the PR merges. *Open questions* 4 and 6 say the register still lacks both rows.
  This slice may not edit contracts.md, so the two rows go there as cross-slice notes, each
  with its gate. The finding names no index sub-edge for H4.
- **X2-15.** Confirmed. The heading read "reconcile is the only owner", and the text said
  `homelab-reconcile` is the only process that starts or restarts a unit outside the boot
  path. H6's *Open questions* 8 and H6.3b's "Gate before merge" bullet ask H4 to name two
  more restarters of a `windowed` unit. The H5 citation read "15-minute `homelab-compose`
  restart", while H5's *The wait timeout* derives 1390 s.
    - *The restart contract* now excepts `windowed` units from reconcile and names SUC's
      upgrade container and `homelab-k3s-restart`, each with its condition, in H6's own
      terms. It states their mutual exclusion through `RuntimeDirectory=homelab-k3s-restart`
      and host mode's Plan and Job check, and that recovery from a failed restart is the
      operator's. The heading keeps the prefix "The restart contract", which H5 and H6 cite.
    - `git grep` at `70591c307` finds "Restart k3s so the log drop-in takes effect" in
      `ansible/roles/setup/k3s/tasks/server.yml` and its `k3s-agent` twin in `agent.yml`.
      The contract therefore says "two unattended processes", and names the operator-run
      `k3s-bringup.yml` apply as a third restarter until H6.4a and H6.5a delete those tasks.
    - Naming the two restarters exposed a third path. H3's step 2 starts every enabled unit
      that is not active, and its `windowed` exemption covers step 3 only. On daniel-box,
      `systemctl is-enabled k3s` printed `enabled`, and the k3s unit read `Type=notify`,
      `Restart=always` and `TimeoutStartUSec=infinity`. Reconcile would therefore block on
      a hung k3s until its own 30-minute kill, on every run. Step 2 now skips a `windowed`
      unit, the drain drops one from `pending.json`, and the renderer refuses an output
      whose dependent is `windowed`. A stub-`systemctl` case and a renderer case carry red
      fixtures, and *Risks*, *Tests and docs* and *Complexity delta* name them. The skip
      matches nothing until H6 marks a unit.
    - The owed-restart property and *Open questions* 5's H5 item cite H5's derived
      `TimeoutStartSec`, 1390 s or about 23 minutes, which fits inside reconcile's 30.
    - *Open questions* 5 gains an H3 item for the step-2 skip and an H6 list. H6's text is
      amended before H6.3b merges, and `docs/host-packages.md` gains the two restarters in
      H6.3b, because they do not exist at H4.1b.

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-09.** *Design* gains *Persisted outputs*. It lists the one persisted output,
  daniel-server's `upsmon.conf`, with the reason C5's rule requires: the shutdown chain must
  start when the decrypt cannot. It states that the Pi persists nothing, and it names the
  HMAC state files as state, not outputs. None of H4's was cut, because none fails the
  rule. H5's conditional file route for alloy's password would persist one on the Pi and
  fails the rule, so H4 sets it to `persist: false` (a cross-slice note to H5). H4.3a
  adds a persisted-output census with two red fixtures (*Verification*), and *Risks* cites
  the rule. H4.2's and H4.4's `docs/break-glass.md` entries gain the loss rule. A lost
  host key means rotating every value in every SOPS file whose creation rule lists that
  key. *Open questions* 4 is resolved by the pillar fold, and H4.1a1's gate reads as met.
  *Complexity delta* gains the census.
- **F-15.** The `sops` paragraph in *`homelab-secrets.service`* reads `sops` from
  `homelab-tools`, by absolute path, with `Depends: homelab-tools`. The old rationale, that
  C5 allowed no architecture-specific package, no longer holds. H4.2 and H4.5 wait on
  H3's build for their architecture, and each carries a `Gate:` line. H4.1c's container installs the built `homelab-tools`.
  *Open questions* 2 is resolved by the C5 amendment. *Complexity delta* gains the
  `Depends` and drops the read-only dependency on the Ansible-installed binary.
