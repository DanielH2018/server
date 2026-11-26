# Variable layout migration

The order of work for
[ADR-0042](adr/0042-each-variable-lives-with-what-it-describes.md), which dissolves `inventory/`.
The record holds the reasons. This page holds the steps, and each one can land and stop.

## Before step 1

- The service list's own move lands first
  ([PR 10093](https://github.com/DanielH2018/server/pull/10093) and the PR after it). It edits
  the same approval floor, and it fixes where `k8s/vars.yml` sits.

## Where the 86 shared keys go

This split rendered all 612 cluster files byte-identical on 2026-11-25.

| File | Keys | Which |
|---|---|---|
| `estate/vars.yml` | 26 | `domain`, `tz`, `puid`, `pgid`, `sys_user`, `lat`, `long`, `lan_subnet`, `lan_router_ip`, `wg_client_subnet`, `slzb_ip`, `cloudflare_ips`, `host_python_version`, `ups_host`, `weekly_reboot_*` (7), `wan_probe_urls`, `loki_streams`, `game_servers`, `ci_required_contexts`, `ci_ancestor_walk_max` |
| `hosts/vars.yml` | 17 | `has_*` (6), `unattended_upgrades_origins_patterns`, `docker_engine_packages`, `loki_route_witness_boot_grace_s`, `ops_cron_host`, `staging_net_cidr`, `etcd_drill_full_cron`, `etcd_drill_full_deadline_s`, `claude_agent_ssh_*` (3), `server_git_tree_lock` |
| `k8s/vars.yml` | 43 | every other key: `k3s_*`, `k8s_*`, `dns_k8s_*`, `crowdsec_k8s_*`, the VIPs and the Zigbee ids |

`has_github_cli` and `has_chezmoi` have no reader but one test. Step 1 settles whether they are
deleted instead of moved.

## Steps

### Step 1: one module reads the files

```diff
  scripts/lib/estate.py           shared_vars(), host_vars(host), Estate
- Python that opens inventory/    85 modules, 125 calls
+ Python that calls lib.estate    a listed few keep the path
```

Every Python read of an inventory file goes through `lib.estate`. The
`inventory-files-are-read-through-lib-estate` row of
`scripts/tests/test_census_rows_test_modules.py` refuses a call that takes an inventory path,
and a function whose parameter defaults to one. Its allow list names the modules that keep a
path, each with its reason:

- the readers themselves: `lib/estate.py`, `lib/render_guard.py`, `lib/inventory_lists.py`;
- the two shipped readers, which cannot import `lib.estate` and move in step 3;
- the tests that read a file's text or assert which file declares a key, which step 6 decides
  per key.

The row reads Python only. Path matchers that name `inventory/` as a string, the shell scripts,
the workflows, `renovate.json` and the prek `files` regexes move with the files in steps 3
and 6.

**Done when** the row is red on a planted reader, every render is byte-identical, and the
docs generators write no change.

### Step 2: a k8s role names the host it reads

Built first, ahead of step 1, because it touches no approval-listed file.

```diff
- supplementalGroups: {{ render_gid }}
+ supplementalGroups: {{ hosts[k8s_primary_node].render_gid }}
```

- `render_gid` (jellyfin, tdarr) and `claude_code_triage_enabled` (artifacts) read through
  `hosts`, and both names joined `_HOSTS_KEYS`. The artifacts read has no `default()`, so a
  missing key fails. The etcd scrape job in observability reads `server_ip` the same way.
- `k3s_agent_node_ips` is in the shared layer. The registry-verify template has no
  `default([])`.
- No k8s render takes a host: a role's templates, the renderer and the test helpers all lost
  the argument.

**Verified** by a cluster render and three host-package renders, all byte-identical. A clean
strict render does not prove it: two of the three keys changed output with no error. A shipped
render does not reach a branch that is off, so the whole suite runs too.

### Step 3: gates and installed readers accept both layouts

No file moves. A host package installs after its merge, so each reader must be live before the
move. The order is ADR-0030's.

| Reader | Change | Fails if late |
|---|---|---|
| `land_renovate_pr.py:INVENTORY` | refuse `inventory/`, `estate/`, `k8s/vars.yml`, `hosts/vars.yml` and `hosts/<host>/vars.yml` | open: a PR to the new files can land unattended |
| `t2-approval-floor` | add `estate/` and `k8s/vars.yml`; `hosts/` is on it | open: the same |
| `test_renovate_floor.py:OFF_FLOOR` | drop `inventory` in step 6 | closed: the test fails |
| `cluster_pins.py` | scan `estate/**` and `hosts/**/vars.yml` too | open: a pin override is accepted |
| `builder_python.py:PYTHON_PIN` | read `estate/vars.yml`, then the old path | falls back to the unit's interpreter |
| `bootstrap_anchor.py`, `bootstrap-host.sh` | read both paths | closed: bootstrap stops |
| `render-diff.yml`, `image-leg.yml`, `image-smoke.yml` | match both paths | open: a check does not run |
| the dotfiles repo's auto-mode text | check it for an `inventory/` path | outside this repo |

Several of these files are on the approval list, so the PR waits for the operator's approval.
The PR also adds `estate/CLAUDE.md`, which states the layer rules, and `!/estate` in
`.gitignore`.

**Done when** `homelab-daniel-box` and `homelab-daniel-server` report the installed version, and
each installed file names the new path. Check with `grep` on the host, not from the repo.

### Step 4: homelab-mcp serves the new directory

`homelab_mcp_source_dirs` gains `estate`. A hostPath to a missing directory holds the pod in
ContainerCreating, so this follows step 3's merge and `checkout-sync`'s next pull on
`k8s_primary_node`. `k8s` is served in full, so it covers `k8s/vars.yml`.

**Done when** the pod lists `estate/CLAUDE.md`.

### Step 5: host values move beside the packages

```diff
- inventory/host_vars/daniel-box.yml
+ hosts/daniel-box/vars.yml
- inventory/host_vars/daniel-server.yml
+ hosts/daniel-server/vars.yml
- inventory/host_vars/daniel-pi.yml
+ hosts/daniel-pi/vars.yml
- inventory/host_vars/_example.yml
+ hosts/_example/vars.yml
```

`host_names` derives from `hosts/*/package.yml`. The checks that read every directory under
`hosts/` must skip `_example`, which has no package. The fan-out pauses for this PR.

**Done when** each host package's content is byte-identical and `host_names` returns the same
three names.

### Step 6: the shared file splits, and inventory/ goes

```diff
- inventory/group_vars/all.yml
+ estate/vars.yml
+ k8s/vars.yml
+ hosts/vars.yml
```

The same commit moves the `renovate.json` entries, the prek regexes, the rule globs, the
citations in docs, agents and skills, and the `# DECIDED:` markers with their keys. It adds the
layer check with a clean and a flagged case per rule, and the marker ADR-0042 governs. A Renovate dry run (`scripts/validate/renovate_extract.py`) must list the same pins as
before, by name.

**Done when** the cluster and host renders are byte-identical, the extracted pins match, and
`git ls-files inventory` is empty.

### Step 7: the old paths go

The installed readers drop `inventory/`, and homelab-mcp stops mounting it. The readers that
compare two commits drop the old layout last: `render_diff_gates.py` and `cluster_pins.py` keep
it until no open PR and no `promote` revision predates step 6.

**Done when** the census row of step 1 finds no `inventory/` reader outside `docs/archive` and
the decision records.

## Undoing a step

Steps 1 to 4 revert alone. Steps 5 and 6 revert as one commit each, and step 3's readers accept
both layouts, so a revert needs no install. Step 7 ends that: revert it, and wait for its
install, before reverting an earlier step.

## Not tested yet

- Host-package renders under the split. Steps 5 and 6 gate on them.
- A Renovate extraction with moved files.
- The bootstrap path from a clean host.
