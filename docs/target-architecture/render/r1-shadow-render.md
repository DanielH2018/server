# R1 — Shadow render with zero diff

Status: IN PROGRESS

PR 1a, the stub module and the stub file: [#4700](https://github.com/DanielH2018/server/pull/4700).

Depends on: none

Unblocks: [R2](r2-secret-placeholders.md), [R3](r3-remove-live-reads.md) and H3 (the host
packager builds on the same CLI). [D2](../deploy/d2-first-leaf.md) needs this renderer and
R2's placeholder mode together. R1 renders only with stub values, and a stub render is never
published (*Stub output is never published*).

## Goal and exit criterion

After R1, `homelab-render cluster` renders every k8s service that
`scripts/deploy_tools/render_targets.py` lists for daniel-box. It reads the git tree alone and
writes the C1 layout. For the same commit and the same secret stub values, its output is byte
for byte what the Ansible manifests role renders. Nothing live changes in R1: the renderer is
a shadow, and Ansible stays the only writer to the cluster.

**Exit criterion.** Three checks, all green at one master commit:

1. **The shadow pair is zero-diff.** This check is **operator-only**, because its Ansible half
   decrypts SOPS and escalates with `become`. At a merged master commit X, on daniel-box, the
   operator runs a stubbed `deploy.sh --dry-run` that keeps its render directories. The
   operator then renders X with `homelab-render cluster` and the same stub file, and runs
   `shadow_diff.py compare`. It exits 0 and prints `<t> tags, <n> files, 0 differences`, with
   no missing and no extra file. `<t>` is the number of Ansible-owned tags at X, which is 56
   at the baseline. The comparison covers each role's applied directory, its deferred
   directory and its volume-claim files. The operator posts that output on the PR that closes
   R1.
2. **The render is deterministic and reads nothing outside the repo.** Two CI test modules
   each render half the tags twice, each time in its own subprocess with a different
   `PYTHONHASHSEED`. An audit hook in each subprocess refuses every file open outside the
   repo, the output directory and the interpreter's own install, and every socket connect.
   The two trees of each half must be identical. Each module writes one digest line per
   rendered directory to its CI job summary. `shadow_diff.py digest` on daniel-box prints
   the same line for every directory, for the same commit, as the push run on master shows.
   Per-directory lines keep that comparison valid however the tags are split across
   modules.
3. **What the render does not cover is named.** A census row lists every role task file that
   applies, creates, patches, writes through `exec` into, or runs a cluster object itself. It
   fails when an unlisted file appears.

## Baseline at `70591c307`

- **56 services, one tag each.** `uv run python scripts/deploy_tools/render_targets.py
  daniel-box` prints 56 names, and `daniel-server` prints none. All 56 `platform: k8s` entries
  in `ansible/inventory/host_vars/daniel-box.yml` carry exactly one tag, equal to the entry
  name, as `scripts/lib/render_guard.py:entry_tags` reads them.
- **63 role directories under `ansible/roles/k8s/`.** The other 7 are utility roles reached
  only through `include_role`: `manifests`, `image-builder`, `cronjob-gate`,
  `arr-notification`, `volume-snapshot`, `volume-revert` and `longhorn-api`. 57 task files
  in 56 roles name `k8s/manifests` in an `include_role`.
- **The template corpus.** 273 `.j2` files under the k8s roles' `templates/` directories hold
  21,644 lines. `ansible/templates/` holds 21 shared macro files.
- **No dry-run render survives the play.** Under `k8s_dry_run`, the task "Create a throwaway
  render directory" in `ansible/roles/k8s/manifests/tasks/main.yml` makes an
  `ansible.builtin.tempfile` directory. "Discard the throwaway render directory" deletes it
  at the end of the role. A deferred directory is a subdirectory of that temporary directory.
- **The file-list rule has an offline copy.** The task "Resolve the manifest file lists"
  derives `manifests_files_resolved` and `manifests_secret_files_resolved`.
  `scripts/lib/k8s_roles.py:resolved_manifest_files` restates it, and
  `ansible/tests/k8s/test_derived_manifest_files.py` holds the two in step.
- **Prune-armed roles render through a wrapper.** `manifests_prune` defaults to `true` in
  `ansible/roles/k8s/manifests/defaults/main.yml`, and an armed role renders every document
  through `ansible/templates/role-labelled.yaml.j2`. The opt-outs are listed in
  `test_manifests_prune.py` in `ansible/tests/deploy/`.
- **Three callers defer an apply.** `pihole`, `headlamp` and `netpol-baseline` pass
  `manifests_deferred_dir_name` as `pihole-instance-2`, `headlamp-observability` and
  `netpol-baseline-observability`.
- **Templates read the play's view of `containers_list`.** The k8s play in `ansible/deploy.yml`
  filters the list with `filter_by_platform('k8s')` and sorts it with
  `ansible/filter_plugins/toposort.py:toposort_containers`. 70 template and defaults files
  read `containers_list`, so list order reaches rendered bytes (homepage tiles, Kuma
  monitors, scrape jobs).
- **Templates read other hosts through `hostvars`.** `scripts/lib/render_context.py:_hostvars`
  keeps only a literal `server_ip` and `containers_list` for each host, and its docstring
  says daniel-box's `containers_list` is left out because it holds Jinja. Three reads need
  more than that:
    - `uptime-kuma/templates/deployment.yaml.j2` and
      `pihole/templates/config/pihole-dnsmasq.conf.j2` read
      `hostvars[k8s_primary_node].containers_list`. In the play that is the k8s-filtered,
      toposorted `set_fact` view.
    - `pi-peer-backup/templates/cronjob.yaml.j2` reads `hostvars['daniel-pi'].ansible_user`,
      which is an inline variable in `ansible/inventory/hosts.ini`.
    - The same template reads `hostvars['daniel-pi'].ansible_host`, which daniel-pi's
      host_vars set to the Jinja `"{{ server_ip }}"`.
- **Templates read no gathered fact.** A grep of the k8s templates, `ansible/templates/` and
  the k8s role defaults for `ansible_*` names finds `ansible_user`, `ansible_host` and
  `ansible_parent_role_names`. The last one derives `manifests_service` in
  `ansible/roles/k8s/manifests/defaults/main.yml`, and the renderer names the service
  itself. `inventory_hostname` renders as `daniel-box`. `playbook_dir` appears only inside
  lookups, never in rendered bytes.
- **The renderer's Jinja environment lacks two filters the templates use.** A `jinja2` parse
  of all 294 templates (the 273 role templates and the 21 shared macros) through
  `scripts/lib/ansible_jinja_env.py:make_ansible_env` collects every filter and test name.
  Two filters are not registered. `homelab_role_label` comes from
  `ansible/filter_plugins/role_label.py`, and `role-labelled.yaml.j2` uses it. `from_json`
  is used by `home-assistant/templates/config/secrets.yaml.j2`. No template uses `from_yaml`.
  The content-tag expression in the task "Compute the content tag" in
  `ansible/roles/k8s/image-builder/tasks/main.yml` does use it. `template_env` defaults to
  `StubUndefined`.
- **One template reads a secret's structure.** `home-assistant/templates/config/secrets.yaml.j2`
  parses `google_assistant_service_account` with `from_json`. It then reads
  `ga.project_id`, `ga.client_email` and `ga.private_key`, and each goes through `to_json`.
  `home-assistant/templates/secret.yaml.j2` pulls that file in through `lookup('template')`.
  The template reads no other name than that key and `domain`, and both are secret keys.
  A grep of the k8s templates and `ansible/templates/` for a secret key name followed by `.`
  or `[` finds no other structured read; its one other hit is the `email.json:` key in
  `uptime-kuma/templates/static-monitors.yaml.j2`. Through ansible-core 2.21.5's `Templar`,
  the old one-key stub form `{"stub": …}` raises `AnsibleUndefinedVariable: object of type
  'dict' has no attribute 'project_id'`. An object holding the three keys renders. The
  template text must be wrapped in `trust_as_template`; an untrusted string comes back
  unrendered, and both forms then pass.
- **Task-computed values reach templates.** A grep of every `set_fact` and `register` name in
  `ansible/roles/k8s/*/tasks/*.yml` against the templates finds these inputs:
    - derivable from the tree: `k8s_built_image_tags` (11 reads), set by the "Compute the
      content tag" task in `ansible/roles/k8s/image-builder/tasks/main.yml`; four script
      checksums (`monitor_bridge_script_checksum`, `autofix_bridge_script_checksum` and the
      two `game_stats_*_script_checksum`); `kuma_declarations` in `uptime-kuma`;
      `janitorr_dry_run`;
    - read from live state: `pi_peer_backup_known_hosts_line`, the three
      `authelia_*_password_hash` facts, and `k8s_built_images` in n8n's two Deployments.
      [R3](r3-remove-live-reads.md) removes these. Three tags read them:
      `pi-peer-backup/templates/secret.yaml.j2`, `authelia/templates/config-secret.yaml.j2`
      (`password: '{{ authelia_password_hash }}'` and its two siblings), and n8n's
      `deployment.yaml.j2` and `deployment-runners.yaml.j2`. n8n passes the digest to
      `ansible/templates/checksum-annotation.yml.j2:checksum_annotation`, which ends in
      `default('unstaged')`. An empty `k8s_built_images` therefore renders
      `checksum/image: "unstaged"`, a value that names no stub.
- **The content tag hashes a render.** `image_builder_content_tag` is `sha-` plus the first 12
  hex digits of `sha256` over `context-configmap.yaml.j2`'s rendered `data`, through
  `dictsort | to_json`. `ansible/tests/deploy/test_image_builder_content_tag.py` evaluates
  that expression through ansible-core's own `Templar`. Ten `include_role` sites in nine task
  files reach `k8s/image-builder`; n8n builds two images.
- **Secrets.** `ansible/vars/secrets.yml` holds 184 top-level keys. Key names are plaintext.
  The SOPS metadata marks 183 values `type:str` and one, `mullvad_account`, `type:int`. Key
  names match 459 times across 78 template files.
- **Tasks that send a secret value.** A YAML walk of every task in
  `ansible/roles/k8s/*/tasks/*.yml` finds the tasks whose body names a secret key. A second
  pass looks for variables derived from one. It collects every key in the k8s role defaults,
  `group_vars/all.yml` and the host_vars whose value references a secret key, transitively.
  It finds one, `headlamp/defaults/main.yml:headlamp_k8s_oidc_issuer_url` (`https://auth.{{
  domain }}`), and no task names it. The tasks fall into three groups:
    - **guarded:** arr-notification's "Declare the Discord notification in …" on
      `k8s_dry_run`. Four tasks are guarded on `k8s_no_mutate`: authelia's "Seed the
      claude-ui TOTP registration", crowdsec's "Register the remote agent machines on the
      LAPI", observability's "Sync the live Grafana admin password with SOPS" and pihole's
      "Reconcile the declared adlists and regex denies into gravity.db". crowdsec's
      `verify.yml` is guarded at its `import_tasks` site on `not k8s_no_mutate`.
      `group_vars/all.yml` sets `k8s_no_mutate` to `ansible_check_mode or (k8s_dry_run |
      bool)`.
    - **side-effect-free:** authelia's two credential asserts and one `set_fact`.
    - **unguarded and acting:** authelia's three hashing tasks ("Hash the Authelia password"
      and its two siblings) start a pod when their hash fact is undefined.
      `sonarr/tasks/verify.yml` holds the second case, the two `uri` reads "Read the cluster
      copy's root folders" and "Read the cluster copy's series". Each sends `X-Api-Key: "{{
      sonarr_api_key }}"` with `check_mode: false` and no `status_code`. Its comment says the
      read is "worth evaluating on a dry run rather than skipping". `sonarr/tasks/main.yml`
      includes `verify.yml` with no `when`. With a stub key Sonarr answers 401, the task
      fails, and the play stops for daniel-box.
- **The Ansible half needs the operator.** `ansible/pre_tasks/load_secrets.yml`, task "Load
  encrypted secrets" (`community.sops.load_vars`), runs in both plays of `ansible/deploy.yml`
  whatever `-e` file is passed. The manifests role's create and render tasks use `become:
  true`. [The agent-user page](../../claude-agent-user.md), *What the `claude` user cannot
  do*, lists `--dry-run` before a merge as decrypting SOPS, with no age key for the agent. Its
  *Rules the design follows* rule out a brokered `--dry-run` over the agent's clone. Its slice
  7, a pre-merge dry run without secrets, is optional and planned.
  `ansible/roles/setup/claude_code/templates/claude-rc.service.j2` sets `PrivateTmp=yes` and
  `ProtectHome=yes` for a non-operator user. An agent session therefore sees neither the
  operator's `/tmp` and `/var/tmp` nor `/home`. `ansible/inventory/group_vars/all.yml` sets
  `sys_user` to `ubuntu`.
- **Ten plays in seven playbooks import `load_secrets.yml`.** The importers are both plays
  of `ansible/deploy.yml`, `initial_setup.yml`, the three plays of `k3s-bringup.yml`,
  `k3s-storage-smoke.yml`, `preflight.yml`, `prune_backups.yml` and `seed_volume_backup.yml`.
- **A deploy-plane change is deferred, not applied.**
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:_BROAD_PLAY_PREFIXES`
  derives to `ansible/deploy.yml`, `ansible/pre_tasks/`, `ansible/tasks/`,
  `ansible/post_tasks/` and `ansible/filter_plugins/`, since `_BROAD_CENSUS_PREFIXES` holds
  only `inventory/` and `templates/`.
    - `scripts/deploy_tools/narrow_lib/broad.py:broad_path_tags` maps a filter plugin first,
      through `_filter_tags`, to the roles that name its filters. It raises `CannotNarrow` for
      any other play prefix.
    - `deployer_lib/deploy_narrow.py:_deploy_plane` then returns `_deferred`. Its `DECIDED a
      deferral on any doubt, never the whole play (#4333)` explains why. The tick
      fast-forwards and runs no playbook. It records a `k8s_unapplied` line for every declared
      service and writes no receipt, so `land.sh` reports `needs-manual-apply`.
      [The GitOps pipeline page](../../gitops-pipeline.md), *A deploy-plane change is narrowed
      before it is applied*, says the same.
    - A change under `ansible/roles/k8s/<role>/` that is not an image-pin bump goes into
      `ChangeSet.k8s`, which defers and records a line for that role.
    - Each service's next deploy discharges its line. A `manifests` line discharges by
      `deploy_release.render_proof` once a render holding the change matches the applied
      digests. A service's own line takes the same proof when
      `scripts/deploy_tools/digest_provable.py` finds the role acts only through the digest.
      *The `k8s_deferred` and `k8s_unapplied` markers, in full* on the same page owns these
      rules.
- **The offline pieces exist.** `scripts/lib/render_context.py:render_context` layers role
  defaults, `group_vars/all.yml` and host_vars in Ansible's precedence.
  `scripts/lib/ansible_jinja_env.py:register_ansible_filters` registers six ansible-core
  filters, a `to_json` stand-in (`scripts/lib/k8s_yaml.py:to_json_stub`), the `search` test
  and 16 repo filters. `scripts/lib/k8s_yaml.py:make_lookup` implements the `file`, `pipe`
  (base64 only, in process), `template` and `vars` lookups.
  `scripts/validate/k8s_manifests.py:main` renders every role with `StubUndefined`, which
  prints `STUB` for any undefined name. It is a structural check, not a byte oracle.
- **Roles apply objects outside their render directory.** The dry-run guard census's own write
  rule is `ansible/tests/deploy/test_k8s_dry_run.py:_chunk_mutates`, applied to each task
  chunk from `ansible/tests/_k8s_guards.py:_task_chunks`. That rule, plus `kubectl run`,
  finds 22 roles: `authelia`, `autofix-bridge`, `cronjob-gate`, `crowdsec`, `game-stats`,
  `headlamp`, `image-builder`, `janitorr`, `jellyfin`, `media-volume`, `monitor-bridge`,
  `n8n`, `netpol-baseline`, `observability`, `pihole`, `prowlarr`, `qbittorrent`,
  `registry`, `tdarr`, `traefik`, `volume-revert` and `volume-snapshot`. Examples: `kubectl
  create configmap --from-file` in `monitor-bridge`; probe Jobs in `n8n` and `prowlarr`;
  `kubectl patch` in `jellyfin`; `kubectl run` hashing pods in `authelia` and a reach probe
  in `qbittorrent`; `exec` writes in `tdarr` and `janitorr`. A plain regex over the task
  text disagrees with that rule in both directions. It matches four roles the rule does not
  count as writes (`configarr`, `karakeep`, `pi-peer-backup`, `wg-easy`). It misses six the
  rule finds (`cronjob-gate`, `crowdsec`, `janitorr`, `qbittorrent`, `tdarr`,
  `volume-revert`), because `-n {{ k8s_namespace }}` holds a space.
- **Nine roles write host files.** `artifacts`, `configarr`, `crowdsec`, `janitorr`,
  `observability`, `pi-peer-backup`, `qbittorrent`, `registry` and `traefik` install scripts,
  timers or files outside `/etc/rancher/k3s/manifests/`.
- **A fleet dry run costs about six minutes.** The cycle record measured 5m38s for 45
  services on 2026-09-25 (`docs/k8s-manifest-cycle-record.md`, *The digest is the staleness
  verdict*).
- **The CI floor.** `.github/workflows/ci.yml` runs job `pytest` on six shards. Its comment
  calls the job "the pole of every run" and puts the heaviest single module at 17.8s, the
  floor no shard can go below. That number is stale. `scripts/dev/pytest_shard_weights.json`
  records `scripts/lib/tests/test_entry_points_answer_help.py` at 30.78s, then
  `scripts/docs/tests/test_gen_reference_scripts.py` at 17.77s.
  `scripts/validate/tests/test_validate_k8s_manifests.py`, which renders every k8s role once
  in process, weighs 15.46s. `pyproject.toml`'s `addopts` runs `--dist loadscope`, which
  keeps one module on one xdist worker. `ansible/tests/repo/test_every_ci_job_is_gated.py`
  requires every job to feed the gate. The agent's token cannot push `.github/workflows/`
  edits (*What the `claude` user cannot do*).
- **Every docs page needs a nav entry.** A comment in `mkdocs.yml` states the rule, and
  `scripts/tests/test_mkdocs_config.py:test_every_doc_is_either_in_the_nav_or_explicitly_excluded`
  enforces it: every `docs/**.md` appears in the nav or matches `exclude_docs`.

The commands behind the counts above, run from the repo root at `70591c307`. The first line
writes key names only, never values. The 459 matches include comments and
`ansible/templates/`, which is why they differ from the design's 444 in 76 templates. The
filter census, the secret-task walk and the write-rule census are short Python reads over
the same tree. PR 2 and PR 5 turn the first two into tests, and PR 8 turns the third into a
census row.

```bash
grep -oE '^[A-Za-z0-9_]+:' ansible/vars/secrets.yml | tr -d ':' | grep -vx sops > /tmp/keys.txt
wc -l < /tmp/keys.txt                                                      # 184 keys
grep -E '^[A-Za-z0-9_]+: ENC\[' ansible/vars/secrets.yml \
  | grep -oE 'type:[a-z]+\]$' | sort | uniq -c                             # 183 str, 1 int
grep -rowFf /tmp/keys.txt ansible/roles/k8s/*/templates ansible/templates | wc -l  # 459
grep -rlwFf /tmp/keys.txt ansible/roles/k8s/*/templates ansible/templates | wc -l  # 78
grep -rlwFf /tmp/keys.txt ansible/roles/k8s/*/tasks | wc -l                # 11 task files
ls -d ansible/roles/k8s/*/ | wc -l                                         # 63 roles
grep -lE '^\s*name:\s*k8s/manifests\s*$' ansible/roles/k8s/*/tasks/*.yml | wc -l   # 57
grep -lE '^\s*name:\s*k8s/image-builder\s*$' ansible/roles/k8s/*/tasks/*.yml | wc -l  # 9
find ansible/roles/k8s -path '*/templates/*' -name '*.j2' | wc -l          # 273
find ansible/roles/k8s -path '*/templates/*' -name '*.j2' -print0 \
  | xargs -0 wc -l | tail -1                                               # 21644
grep -rlw containers_list ansible/roles/k8s/*/templates ansible/templates \
  ansible/roles/k8s/*/defaults | wc -l                                     # 70
grep -lE 'kubectl.*(apply|create|patch|delete|run|exec)' ansible/roles/k8s/*/tasks/*.yml \
  | grep -v /manifests/ | cut -d/ -f4 | sort -u | wc -l                    # 25, reads included
grep -lE 'ansible.builtin.(cron|systemd|systemd_service)|kuma_check_timer|dest: /(usr|etc/systemd|etc/cron|opt|var/lib|home)' \
  ansible/roles/k8s/*/tasks/*.yml | cut -d/ -f4 | sort -u | wc -l          # 9
# Task-computed names that templates read:
grep -h -A3 'ansible.builtin.set_fact:' ansible/roles/k8s/*/tasks/*.yml \
  | grep -oE '^\s{4,6}[a-z_0-9]+:' | tr -d ' :' > /tmp/taskvars.txt
grep -h 'register:' ansible/roles/k8s/*/tasks/*.yml | awk '{print $2}' >> /tmp/taskvars.txt
grep -v '^manifests_' /tmp/taskvars.txt | sort -u > /tmp/taskvars.sorted
grep -rhoFwf /tmp/taskvars.sorted ansible/roles/k8s/*/templates ansible/templates \
  | sort | uniq -c
# Structured reads of a secret key (key followed by "." or "["):
sed 's/.*/\\b&(\\.[a-z_]|\\[)/' /tmp/keys.txt > /tmp/keypat.txt
grep -rnoEf /tmp/keypat.txt ansible/roles/k8s/*/templates ansible/templates  # 1, email.j
grep -rlF 'stub-' ansible/roles/k8s ansible/templates ansible/inventory | wc -l  # 0
```

## Design

### What the renderer is

A new directory, `scripts/renderer/`, holds the CLI and its library. The directory is not
named `render` because `scripts/infra_map/render.py` sits on `pythonpath`, and a regular
module shadows a namespace package of the same name
([python code organization](../../python-code-organization.md)). Like `scripts/validate/`,
the directory stays off `pythonpath`. Its modules import as `renderer.renderer_lib.<module>`
after the usual `scripts/` bootstrap. pytest names test modules by basename, so every test
module under `scripts/renderer/tests/` starts `test_renderer_`.

- `homelab_render.py` is the C1 entry point. R1 implements one mode:
  `homelab_render.py cluster --out DIR [--role TAG ...] --secret-stubs FILE`. Without
  `--secret-stubs`, R1's `cluster` exits 2 before it writes anything under `--out`, and says
  that placeholder rendering arrives with R2. The `host` mode belongs to H3.
- The renderer builds its environment with `template_env` and a strict undefined, never the
  `StubUndefined` default. An input the renderer fails to supply then stops the render
  instead of printing `STUB` into both trees of the determinism test.
  `register_ansible_filters` gains `homelab_role_label` and `from_json` in PR 2, and
  `from_yaml` in PR 3, where the content-tag expression is evaluated.
- `renderer_lib/context.py` builds the play-time context for one role.
    - It calls `render_context` for the role and the host.
    - It replaces `containers_list` with the play's view: the k8s-filtered, toposorted list
      that `deploy.yml`'s pre_tasks build, using the same two filter functions.
    - It builds `hostvars` itself. `hostvars['daniel-box']` is the rendered context, so it
      carries the play's `containers_list` view. Every other host gets its inline variables
      from `ansible/inventory/hosts.ini` and its own host_vars, and any Jinja in them
      resolves against that host's own context. daniel-pi's `ansible_host` therefore
      resolves to daniel-pi's `server_ip`.
    - It lays the secret stub map over every secret key.
- `renderer_lib/task_inputs.py` reads what Ansible computes in role tasks.
    - It reads the `vars:` of each `include_role` of `k8s/manifests` and `k8s/image-builder`
      statically from `tasks/*.yml`.
    - It computes the content tags with the role's own expression, the four script
      checksums from the staged source files, `kuma_declarations` from its own render, and
      `janitorr_dry_run` from defaults.
    - It takes the five live-read facts from the stub file until R3 removes them. It owns
      `PINNED_LIVE_FACTS`, which maps each fact to its stand-in value and to the tags whose
      templates read it: `authelia`, `n8n` and `pi-peer-backup`. A string stand-in is
      `stub-` plus the fact name with underscores turned to hyphens, so it carries the same
      marker as a secret stub. `k8s_built_images` pins to `[]`, which renders no marker.
      A PR 1a test derives the reader tags with a word grep of the templates for each fact
      name and asserts they equal the constant's, so a new reader cannot be missed.
- `renderer_lib/cluster.py` renders one role into `DIR/cluster/<tag>/`.
    - It applies the file-list rule through `resolved_manifest_files`, the two shared
      default templates, the `k8s_claims` claim files and the `role-labelled.yaml.j2`
      wrapper for an armed role.
    - It writes deferred files to `DIR/cluster/<tag>-deferred/`.
    - It writes a `kustomization.yaml` that lists the files in each directory in lexical
      order, the order `kubectl apply -f <dir>/` reads them.
- `ansible/filter_plugins/secret_stubs.py` holds the stub form, so the renderer and the
  Ansible preamble read one definition. C1 already has the renderer reuse
  `ansible/filter_plugins/`.
    - The filter `homelab_stub_keys` takes the parsed `secrets.yml` mapping. It returns the
      sorted key names without `sops` and without the keys in `NEVER_STUB`.
    - The filter `homelab_secret_stub` maps a variable `K` to `stub-` plus `K` with underscores
      turned to hyphens. That shape is a valid DNS label, so a stubbed host name still passes
      `--dry-run=server`.
    - `google_assistant_service_account` is the one key a template parses with `from_json`,
      and home-assistant's `secrets.yaml.j2` then reads three of its fields (*Baseline*). Its
      stub is a JSON object string with exactly those keys, `project_id`, `client_email`
      and `private_key`. Each value is `stub-google-assistant-service-account-` plus the
      field name with underscores turned to hyphens, such as
      `stub-google-assistant-service-account-project-id`. The plugin serializes it with
      sorted keys, so it is one fixed string. `homelab_secret_stub` returns exactly that
      string for that key, so the preamble's content assert compares against it. R2
      removes that parse.
    - The filter `homelab_unstubbed_keys` takes the key list and the run's values for those
      keys. It returns the keys whose value is not exactly its `homelab_secret_stub`, as
      names only. The preamble's content assert and the manifests role's assert both call
      it, so the comparison has one definition, and neither assert loops over values.
    - `NEVER_STUB` holds one variable, `become_password`, with its reason:
      `ansible/pre_tasks/load_secrets.yml` hands it to privilege escalation, and no template
      reads it.
    - The plugin lands complete in PR 1a, with all three filters and the home-assistant
      shape. Its `FilterModule.filters()` holds one `return` of a literal dict, because
      `scripts/deploy_tools/narrow_lib/filters.py:filter_names` refuses any other shape, and
      a refusal would defer every service. Until PR 5, no file that
      `scripts/deploy_tools/narrow_lib/broad.py:_grep` reads names these filters, so
      `_filter_tags` maps the plugin to no tag. PR 5 names
      them in `ansible/pre_tasks/load_secrets.yml`, a `PLAY_PREFIXES` path, and
      `broad.py:_sort_hits` then raises `CannotNarrow` for that hit. From PR 5 on, every
      edit to the plugin lands like PR 5 itself (*Risks*).
    - Ansible loads every module under `filter_plugins/` in every play, so the plugin uses
      the standard library only. It never imports ansible-core, which also keeps R4's
      ansible-core grep over the renderer's filter plugins clean.
- `shadow_diff.py` is a second entry point with four subcommands.
    - `stubs --out FILE` writes an Ansible extra-vars file. It holds the stub map, the
      sentinel `homelab_secret_stubs: true` and the five pinned live-read facts, which
      `PINNED_LIVE_FACTS` lists. Each pinned fact takes a fixed stub value, never a live one
      (`k8s_built_images` pins to `[]`). The file is a function of the tree alone, so an
      agent, CI and the operator write the same file. A JSON object is also a YAML flow
      mapping, so `stubs` writes the home-assistant stub as a quoted YAML string, and `-e
      @FILE` loads it as a string.
    - `compare A B` compares two trees. Each side is either an Ansible shadow tree, with one
      directory per service at the top, or a C1 tree, with `cluster/` at the top. The
      expected tag set comes from the commit under test. For an Ansible side it is
      `render_targets daniel-box` minus the entries marked `owner: flux` (C4). For a C1 side
      it is all of `render_targets`. When one side is an Ansible tree, `compare` checks the
      Ansible-owned set and ignores the C1 side's flux-owned directories. Two C1 trees are
      checked over the full set. `compare` maps `<ansible>/<svc>/` to `<c1>/cluster/<svc>/`
      and `<ansible>/<svc>/<deferred>/` to `<c1>/cluster/<svc>-deferred/`. It ignores
      `kustomization.yaml` and reports every differing, missing and extra file. It refuses
      to report success over an empty set. When one side is an Ansible tree, it runs
      `stubcheck`'s rule first. It prints a diff hunk only for a file that exists on both
      sides and passes that rule. A missing file, an extra file, or a file that fails the
      rule is named, and its content is never printed.
    - `stubcheck ANSIBLE C1` checks that the Ansible tree holds stub values where the C1
      stub render does. For each file present on both sides, it collects every stub token
      in the C1 file: `stub-` followed by lower-case letters, digits and hyphens. It
      decodes the `data` values of a `kind: Secret` document on both sides before it looks.
      Every token the C1 file holds must also appear in the Ansible file. A file that
      fails is named with the tokens it lacks, and `stubcheck` exits 1. The rule needs no
      list of real values, and a composed value, such as an Authelia config line, still
      carries its stub token. Its one false positive is a renderer diff at a stub site,
      which the operator inspects locally before handing the tree over. Its scope is
      narrower than the copy: it vouches only for a file present on both sides whose C1
      render holds a token. A file only Ansible rendered, or one with no token, passes
      unchecked, and the role assert is the guard for those.
    - `digest DIR` prints one line per directory under `DIR/cluster/`: the directory name
      and a sha256 over every file's path relative to that directory and its bytes. A final
      `total` line holds one sha256 over every file under `DIR`, hashed the same way. It
      sorts every path bytewise, so no line depends on the absolute directory or the
      locale. A render of some tags gives the same lines for those tags as a render of all
      of them.
  It is shadow tooling rather than a second renderer, so C1's one-CLI rule still holds, and
  `scripts/lib/tests/test_entry_points_answer_help.py` covers its `--help`.
    - Only `compare` and `stubcheck` read an Ansible tree. `stubs` reads only the git tree,
      and `digest` only a C1 tree, so both outlive the Ansible path that R4 deletes.
    - Their callers are the operator's shadow run and the two determinism modules in PR 8,
      which call the stub writer and import `digest`'s function. R1 itself adds no workflow
      step. R2's `roundtrip` step in `.github/workflows/ci.yml` is the first workflow call
      to `shadow_diff.py`.
    - R4 deletes `shadow_diff.py` once `compare` and `stubcheck` have no subject. Before
      that, the stub writer and the digest function must move or stay, or the determinism
      modules break.

C1 is followed as written: two renders of one commit are byte-identical, and the renderer
never contacts a cluster, host, registry or key. The `DIR/flux/<tag>.yaml` half of C1 needs
`dependsOn` (D4) and `postBuild` (D3), so R1 does not write it. *Open questions* names the
C1 and C3 rows R1 extends.

### Why stub values, and not C1 placeholders, in R1

R1 proves that the renderer reproduces Ansible for identical inputs. A placeholder changes
the bytes on purpose: C1 escapes a literal `${` as `$${`. That transformation is R2's, and R2
uses R1's stub render as its round-trip oracle. Keeping the two apart means a diff in R1 is
always a renderer bug, never an escaping question.

### Stub output is never published

A stub render is a comparison input, and nothing may push it as the cluster artifact. Every
secret key renders as its stub, and `domain` is a secret key: `grep -c '^domain:'
ansible/vars/secrets.yml` prints 1. D2's leaf, bento-pdf, ships no IngressRoute template of
its own. It takes `ansible/templates/ingressroute-default.yaml.j2`, which calls the
`ingressroute` macro in `ansible/templates/ingressroute.yml.j2`. That macro builds the Host
rule as `hostname ~ '.local.' ~ domain`. A stub render therefore routes
`bento-pdf.local.stub-domain`. The stub is a valid DNS label, so the object passes
`--dry-run=server` and applies cleanly, yet the route matches no request.

The five pinned live-read facts are stub output too. Their values are fixed stand-ins, so a
render of `authelia`, `n8n` or `pi-peer-backup` that reads one is a stub render for that
tag, whatever mode produced it. R2's placeholder mode reads the same `PINNED_LIVE_FACTS`
values until R3 empties the constant. The publish therefore cannot rest on the order of the
slice index alone, which today puts R3 before D5. Two guards are needed, and both belong to
other slices (Open question 7):

- a tag rule: placeholder mode refuses a flux-owned tag that `PINNED_LIVE_FACTS` lists as a
  reader of a fact it still pins. This is the only guard that sees `k8s_built_images: []`,
  which renders `"unstaged"` and carries no stub token. R2 owns it.
- a value rule: D2's publish census refuses any rendered string that holds a stub token,
  which covers the string stand-ins as well as the secret stubs. D2 owns it.

R1 supplies what both rules read: the reader tags in `PINNED_LIVE_FACTS`, held to the
templates by a test, and the `stub-` marker on every string stand-in.

R1 makes the safe failure the default:

- `cluster` without `--secret-stubs` exits 2 before it writes a file. Under R1 alone, a bare
  `cluster` call in a publish job fails rather than publishing an empty or stubbed tree.
- R1 writes no `DIR/flux/` directory. D2's publish census requires one Kustomization file per
  flux-owned tag, so it fails a tree from R1's renderer. That holds after D2 only if D2's
  `flux/` writer skips stub mode, which Open question 7 asks D2 to state.

D2's publish therefore needs R2's placeholder mode, and a source for `domain` before D3
writes `homelab-cluster-vars`. Both are outside R1. *Open questions* names the edge and the
owners, and *Risks* records the hazard.

### The Ansible half of the shadow pair

The Ansible half is **operator-only**. `load_secrets.yml` decrypts every key before any role
runs, whatever the stub file says, and the manifests role escalates with `become`. The
operator runs it as `ubuntu` from the operator's own checkout, in a clean worktree at a
merged master SHA. It never runs over the agent's clone. Three guards make sure that a stub
file stays inside a k8s dry run, and that a shadow directory holds only stub values.

**The preamble refuses a misplaced or incomplete stub file.** Two asserts go into
`ansible/pre_tasks/load_secrets.yml`, after the local-connection assert and ahead of "Load
encrypted secrets", tagged `always`. Every playbook that reads a secret imports that file, so
every one inherits them. Both run on a stub run, which is either of two things: the sentinel
`homelab_secret_stubs` is true, or any key already resolves to its stub form before the SOPS
load. The second half means a stub file stripped of its sentinel still meets both asserts.
On a run with no stub file, nothing is defined before the load, so the trigger is false.

1. "Refuse a secret stub file outside a k8s dry run". On a stub run it requires
   `k8s_dry_run` and the play variable `load_secrets_stubs_allowed`. Only
   `ansible/deploy.yml` sets that variable.
    - The k8s play sets it to `true`.
    - The Docker play sets it to whether the host's `containers_list` holds no Docker entry.
      The shadow run on daniel-box therefore passes the Docker play, which deploys nothing
      there. A stubbed run with `-e target=daniel-pi` stops before any Pi role runs.
    - No other playbook sets it. A stub file passed to `initial_setup.yml`, `k3s-bringup.yml`
      or any other importer stops at its preamble, even with `-e k8s_dry_run=true`.
2. "Refuse a stub file that does not stub every key". On a stub run it reads the key names
   from the tree's `secrets.yml` with `lookup('file') | from_yaml | homelab_stub_keys`. That
   reads ciphertext, so nothing is decrypted. It asserts that `homelab_unstubbed_keys`
   returns no key, which means every key resolves to exactly `K | homelab_secret_stub`. It
   names each key that does not.
    - It runs before the SOPS load, so every value it sees is a stub from the extra-vars
      file or undefined. It never sees a real value, and its message names keys only.
    - Extra vars outrank the values `load_vars` loads, so a key it saw stubbed stays stubbed
      after the load.
    - It sets no fact on success. A fact set here would persist into the second play on the
      same host, and an extra var of the same name would outrank it. The manifests role
      repeats the check rather than trusting a flag.
    - A stub file written before `/add-secret` added a key fails here and names that key.
      `-e homelab_secret_stubs=true` passed without the file fails on every key.
    - The preamble runs in every play, so the trigger itself must stay false and cheap on a
      real run: one read of the ciphertext file and one comparison per key, with nothing
      defined to compare. A PR 5 red-proof holds it false for a context with no stub file.

**The manifests role keeps a shadow render.** The role gains one variable,
`manifests_shadow_dir`, defaulting to `''`.

- When it is set, "Select the render directory" points `manifests_dest_dir` at
  `<manifests_shadow_dir>/<service>` instead of the temporary directory. The deferred
  directory follows as a subdirectory, as it does under any dry run. "Discard the throwaway
  render directory" skips.
- An assert at the top of the role refuses the variable unless `k8s_dry_run` is true and
  every key still holds its stub. It recomputes the preamble's content check: it reads the
  key names from the ciphertext `secrets.yml` with `homelab_stub_keys` and requires
  `homelab_unstubbed_keys` to return none. It reads no flag and no sentinel, so no extra var
  can stand in for the check. A run with `manifests_shadow_dir` set and no stub file holds
  real values after the SOPS load, and the assert fails before any file is written.
    - It runs after the SOPS load, so the values it compares are real wherever a key is not
      stubbed. The filter returns key names only, and the assert's message prints those
      names and never a value.
    - It runs only when `manifests_shadow_dir` is set, so a real deploy pays nothing for it.
- Under the variable, the directory and the files it holds are owned by `sys_user`. The
  operator's `compare`, copy and cleanup then run without `sudo`.

**Sonarr's API reads skip under stubs.** In `ansible/roles/k8s/sonarr/tasks/verify.yml`, the
two `uri` reads and the two asserts that read their registers gain `when: not
(homelab_secret_stubs | default(false) | bool)`. The asserts need the guard too, because a
skipped read leaves a register with no `.json`. The guard is not `k8s_dry_run`: an ordinary
dry run keeps evaluating the reads with the real key, which is why they carry `check_mode:
false`.

**Running it and handing the result over.** The operator runs the pair at master SHA X with
the commands under *Verification*, and runs `compare` too. The operator posts `compare`'s
output on the PR or issue that tracks the run. The output holds file names and diff hunks
of a tree that the preamble and the role assert proved stub-only, and `compare` prints a
hunk only for a file that passes `stubcheck`'s rule. For PR 7's loop, the operator also
copies the Ansible tree into the agent's home with `sudo` and gives it to the agent user,
because `PrivateTmp=yes` and `ProtectHome=yes` hide `/var/tmp` and `/home` from an agent
session. The copy has two gates. The dry run exited 0, which means the role assert passed
for every render. `shadow_diff.py stubcheck` then exits 0 over the Ansible tree and the C1
render, as a second check on the bytes themselves.

A branch that changes only `scripts/renderer/` leaves Ansible's render of X unchanged. One
operator run at X therefore serves every such branch based on X, and the agent renders and
compares each branch itself. A branch that changes `ansible/filter_plugins/secret_stubs.py`
changes the stub file, and so both halves. It needs a new operator run at a master SHA that
holds it.

### What zero diff covers, and what it does not

Zero diff covers what the manifests role renders for the Ansible-owned tags: the applied
directory, the deferred directory and the claim files. It does not cover four other
channels, and R1 records each one rather than rendering it:

- objects that role tasks apply themselves: `--from-file` ConfigMaps, probe and gate Jobs,
  `kubectl patch` calls, `kubectl run` pods, `exec` writes and image build Jobs. The census
  row in PR 8 lists them by task file.
- files written to hosts by nine roles. These belong to the host plane.
- the `homelab` Namespace and its LimitRange, which `ansible/deploy.yml` builds inline in the
  k8s play.
- the values of the five pinned live-read facts, which R3 removes.

### File and call tree

```diff
+ scripts/renderer/homelab_render.py
+ scripts/renderer/shadow_diff.py
+ scripts/renderer/renderer_lib/context.py
+ scripts/renderer/renderer_lib/task_inputs.py  (+ PINNED_LIVE_FACTS and its reader tags)
+ scripts/renderer/renderer_lib/cluster.py
+ scripts/renderer/tests/                       (library tests, two determinism modules)
+ ansible/filter_plugins/secret_stubs.py        (stub form, key list, comparison, NEVER_STUB)
+ ansible/tests/k8s/                            (+ the secret-key task census module)
+ docs/homelab-render.md
  mkdocs.yml                                    (+ nav entry for docs/homelab-render.md)
  scripts/lib/ansible_jinja_env.py              (+ homelab_role_label, from_json, from_yaml)
  ansible/pre_tasks/load_secrets.yml            (stub refusal and stub content asserts)
  ansible/deploy.yml                            (load_secrets_stubs_allowed in both plays)
  ansible/roles/k8s/sonarr/tasks/verify.yml     (two reads, two asserts skip under stubs)
  ansible/roles/k8s/sonarr/CLAUDE.md            (the stub guard on verify.yml)
  ansible/roles/k8s/manifests/defaults/main.yml (+ manifests_shadow_dir)
  ansible/roles/k8s/manifests/tasks/main.yml    (directory selection, assert, owner, discard)
  ansible/roles/k8s/manifests/CLAUDE.md         (+ manifests_shadow_dir bullet)
  ansible/tests/_k8s_guards.py                  (+ write rule moved in; guard pattern argument)
  ansible/tests/deploy/test_k8s_dry_run.py      (imports the moved write rule)
  ansible/tests/repo/test_census_rows_roles.py  (+ out-of-render census row)
  scripts/dev/pytest_shard_weights.json         (the determinism modules; the help module)
```

```text
homelab_render.py cluster --out DIR --secret-stubs FILE
└─ renderer_lib.cluster.render_cluster(host="daniel-box", tags, out, stubs)
   ├─ render_targets.render_targets(host)                     at baseline
   ├─ renderer_lib.context.play_context(role, host, stubs)
   │   ├─ render_context.render_context(role_dir, host, overrides)    at baseline
   │   ├─ toposort.build_k8s_dep_map, toposort_containers             at baseline
   │   └─ secret_stubs.homelab_stub_keys, homelab_secret_stub
   ├─ renderer_lib.task_inputs.include_vars(role)
   ├─ renderer_lib.task_inputs.derived_facts(role, ctx)
   ├─ k8s_roles.resolved_manifest_files(role)                  at baseline
   └─ ansible_jinja_env.template_env + k8s_yaml.make_lookup    at baseline
```

## Steps, as PRs

No PR in R1 moves ownership of a live object, so the two-writer rule (C4) has nothing to
check: Ansible stays the only writer, and the renderer writes only to `--out`. Each PR
updates `docs/homelab-render.md` for what it adds.

1. **PR 1, in two parts.** PR 1a lands the stub module, and PR 1b the first render, which
   needs PR 1a's stub file.

   **PR 1a: the stub module and the stub file** (about 160 lines).
   `ansible/filter_plugins/secret_stubs.py`, complete: `homelab_stub_keys`,
   `homelab_secret_stub` with the home-assistant shape, `homelab_unstubbed_keys` and
   `NEVER_STUB`. `renderer_lib/task_inputs.py` holding only `PINNED_LIVE_FACTS`.
   `shadow_diff.py stubs`. `docs/homelab-render.md`, with its nav entry in `mkdocs.yml`
   under *Operations*, *Deploy and land*, after *The GitOps pipeline*. Every stub shape that
   R1 knows of lands here, before PR 5 makes a plugin edit a deferral of every service.
   Tests in `scripts/renderer/tests/`:
    - `homelab_stub_keys` returns one name per key in `secrets.yml`, without `sops` and
      `NEVER_STUB`. Every stub but `google_assistant_service_account`'s is a valid DNS
      label. That one parses as a JSON object with exactly `project_id`, `client_email` and
      `private_key`, and each value is a string that starts with
      `stub-google-assistant-service-account-`.
    - `stubs` writes the same file twice for one tree, and it holds every stub, the sentinel
      and the five pinned facts. Reloading the file with a YAML parser gives
      `google_assistant_service_account` as a `str` equal to its `homelab_secret_stub`.
    - home-assistant's `templates/config/secrets.yaml.j2` renders through ansible-core's
      `Templar` with the stub map as its variables. The test wraps the template text in
      `trust_as_template`, as `ansible/tests/deploy/test_image_builder_content_tag.py:_tag`
      does, because an untrusted string comes back unrendered. The output holds each
      field's stub. The red case is the old `{"stub": …}` form, which must raise
      `AnsibleUndefinedVariable`. That raise also proves the template was evaluated.
    - `homelab_unstubbed_keys` returns nothing for the full stub map, and returns exactly
      the key whose value was changed in a copy of it.
    - `PINNED_LIVE_FACTS`'s reader tags equal those a word grep of the k8s templates and
      `ansible/templates/` finds for each fact name. Its `must_find` member is
      `authelia/templates/config-secret.yaml.j2` for `authelia_password_hash`.
    - `shadow_diff.py --help` exits 0. Two entry points join
      `scripts/lib/tests/test_entry_points_answer_help.py` across PR 1a and 1b, the heaviest
      module in the weights table, so PR 1b refreshes its weight with `uv run python
      scripts/dev/pytest_shard.py --record-files` on that path.

   `ansible/filter_plugins/` is a deploy-plane prefix. No file that `_grep` reads names the
   new filters yet, so `_filter_tags` maps the plugin to no tag and the tick merges and
   applies nothing. The executor reads the tick's `narrow:` journal line to confirm it. If
   the tick defers instead, the landing is the one PR 5 describes.

   **PR 1b: render one stateless role in the C1 layout** (about 170 lines).
   `homelab_render.py` with its refusal without `--secret-stubs`; `context.py` with the
   play's `containers_list` and the `hostvars` rule; the `kustomization.yaml` writer; and the
   strict undefined. It renders `littlelink`, which has no task inputs, no claims and no
   deferred files. Tests in `scripts/renderer/tests/`:
    - The `containers_list` view matches the `toposort_containers` order for the real
      inventory.
    - `hostvars[k8s_primary_node].containers_list` equals that view.
      `hostvars['daniel-pi'].ansible_user` comes from `hosts.ini`, and
      `hostvars['daniel-pi'].ansible_host` resolves to daniel-pi's own `server_ip`.
    - `homelab_render.py --help` exits 0. `cluster` without `--secret-stubs` exits 2 and
      leaves `--out` empty.

   PR 1b touches only `scripts/`, so the deployer has nothing to apply.
2. **Apply the manifests role's file rules** (about 200 lines). The `resolved_manifest_files`
   lists, the shared `service.yaml` and `ingressroute.yaml` defaults, `k8s_claims`, the
   labelled wrapper and the deferred directory, with the `homelab_role_label` and `from_json`
   registrations the wrapper and home-assistant need. `include_vars` reads the
   `manifests_*` vars from each role's include. Every role without task-computed inputs
   renders after this PR. Tests:
    - one armed and one unarmed role; one deferred caller; one role with two claims.
    - A template census parses every template of the 56 roles and `ansible/templates/`. It
      asserts that every filter and test name resolves in the renderer's environment, and it
      must find `role-labelled.yaml.j2`. A fixture template with an unregistered filter is
      flagged.
3. **Derive the task-computed inputs** (about 190 lines). Content tags, script checksums,
   `kuma_declarations` and `janitorr_dry_run`, plus the `from_yaml` registration the
   content-tag expression needs. Tests: the renderer's content tag equals the tag
   ansible-core's `Templar` computes for each of the ten image-builder include sites, using
   the evaluation helper `test_image_builder_content_tag.py` already uses. A mutation test
   drops one `image_builder_context_files` entry from the renderer's input and must see the
   tags differ. After this PR all 56 tags render.
4. **Skip sonarr's API reads under a stub file** (about 30 lines). The four `when` guards in
   `sonarr/tasks/verify.yml`, and a line under *Notable* in `ansible/roles/k8s/sonarr/CLAUDE.md`.
   PR 5's secret-key census is its test. It names "Read the cluster copy's root folders" as a
   member that must be found guarded, and it flags the unguarded form. Landing this first
   keeps that census green when it arrives. The change is a k8s role change that is not an
   image-pin bump. The deployer therefore defers it and records a `k8s_unapplied` line for
   `sonarr`. `land.sh` follows it through to a deploy of `sonarr`, which discharges the line.
   A deploy without a stub file evaluates the reads exactly as before.
5. **Refuse a misplaced or incomplete stub file** (about 190 lines). The two preamble
   asserts in `ansible/pre_tasks/load_secrets.yml`; `load_secrets_stubs_allowed` in both
   plays of `ansible/deploy.yml`; and the secret-key task census, a module in
   `ansible/tests/k8s/`.
    - **The census rule.** It reads every task in `ansible/roles/k8s/*/tasks/*.yml` whose
      body names a secret key or a derived variable. A derived variable is a key in the k8s
      role defaults, `group_vars` or host_vars whose value references a secret key,
      transitively. Each such task must pass one of four tests. It can be skipped under a
      stubbed dry run, by a `k8s_dry_run`, `k8s_no_mutate` or `homelab_secret_stubs` guard
      on the task or at its include site. It can be a side-effect-free module (`set_fact`,
      `assert`, `debug`, an include). Otherwise it must sit on the module's per-task
      exemption list with a reason. The census reuses `_k8s_guards.py:_task_chunks` and
      `_guard_covered_files`, which gains a guard-pattern argument that defaults to
      `_GUARD_FACT`.
    - **Its exemptions.** Only authelia's three hashing tasks are exempt. Each one's `when`
      requires a hash fact that the stub file pins.
    - **Why its own module.** The exemptions are per task. A `Census` row's `allow` map is
      keyed by file, so it would exempt all of authelia's `tasks/main.yml`.
    - **Why the refusal sits in the preamble.** The refusal must run before every task that
      could act on a stub. Those tasks include image-builder's build, authelia's hashing
      pods, crowdsec's machine registration and the Grafana password sync. The preamble is
      the one point every playbook passes before any role. An assert in the manifests role
      would run after all of them.
    - **What landing does.** The PR changes `ansible/pre_tasks/` and `ansible/deploy.yml`,
      which are play prefixes, so the tick refuses to narrow it. It fast-forwards and runs
      no playbook. It records a `k8s_unapplied` line for every declared service and alerts
      once, and `land.sh` reports `needs-manual-apply`. Without a stub file both asserts are
      skipped and the play variable is read by nothing, so no fleet apply is owed.
    - **What it changes for later PRs.** The preamble now names `homelab_stub_keys` and
      `homelab_unstubbed_keys`. From this merge on, `_filter_tags` cannot narrow an edit to
      `ansible/filter_plugins/secret_stubs.py`, and every such edit lands exactly like this
      PR: a `k8s_unapplied` line for every declared service and `needs-manual-apply`. A
      stub edit moves no byte of a real render, so the lines clear the same way. R2's and
      R3's edits to the plugin inherit this landing (*Risks*).
    - **How the lines clear.** Each service's line drops on that service's next deploy.
      `manifests` and every role that `digest_provable.py` accepts drop through the render
      proof within about an hour, because no rendered byte moves. The PR's author lists the
      lines still open a day after the merge on the PR. The operator decides whether to let
      them discharge on later deploys or to run `gitops_state.py clear-owed k8s_unapplied
      <svc>`. Root `CLAUDE.md`, *When to wait*, applies: an executor stops at
      `needs-manual-apply` and never runs an unscoped `deploy.sh` to follow through.
6. **Keep a stubbed dry-run render** (about 120 lines). `manifests_shadow_dir`, its assert,
   the owner switch and the discard guard, with a `manifests_shadow_dir` bullet in
   `ansible/roles/k8s/manifests/CLAUDE.md`. The assert recomputes the stub check with PR 1a's
   filters, so it needs no plugin edit. The change touches the shared `manifests` role, so
   the deployer records one `k8s_unapplied` line for `manifests`. With the variable unset no
   rendered byte moves, so the render proof discharges that line within about an hour.
7. **Compare the two renders, then close the diffs** (about 190 lines, then one PR per
   class of diff). `shadow_diff.py compare`, `stubcheck` and `digest`. The operator runs
   the shadow pair at a master SHA X (*Verification*), posts the report and hands the agent a
   copy of the Ansible tree. Each class of difference then gets its own renderer PR, based on
   X. Each fix carries a regression test in `scripts/renderer/tests/`. Where the class
   reproduces outside a play, the test renders the affected template through both
   ansible-core's `Templar` and the renderer and asserts equal bytes. The agent compares each
   branch with the copy at X. A class closes when the operator's next run, at a master SHA
   that holds the fix, no longer lists it. Expected classes are trailing newlines from the
   `template` module, `lookup('template')` whitespace, and filter-argument defaults. Never fix
   a diff by changing a template: that changes what Ansible deploys. A class whose fix is a
   stub shape changes `secret_stubs.py`, which is both halves. It lands like PR 5 and closes
   only on an operator run at a master SHA that holds it; the copy at X cannot test it.
8. **Census of what the render does not cover, plus the determinism test** (about 190 lines).
    - **The census row.** A `Census` row in `ansible/tests/repo/test_census_rows_roles.py`.
      It reads every task file under `ansible/roles/k8s/*/tasks/` except `manifests`'. Its
      offence is a task chunk that writes by `_chunk_mutates`, or that runs `kubectl run`.
      `_chunk_mutates` and the patterns it reads move from `test_k8s_dry_run.py` into
      `_k8s_guards.py`, so both tests import one rule. The row's `allow` map holds each task
      file the 22 baseline roles write from, with the slice that owns its move as the reason.
      Its `count` is the number of writing chunks in a file, so a file that stops writing
      leaves the row's members. `must_find` names the files of `monitor-bridge`,
      `cronjob-gate` and `qbittorrent`, and so fails when one of them stops writing rather
      than when it stops being selected. A writing file outside the `allow` map fails the row
      until a reviewer adds it with its owning slice. A stale `allow` entry fails it too.
    - **The determinism modules.** Two modules in `scripts/renderer/tests/` share one
      helper. One renders the tags at even positions of the sorted `render_targets` list,
      the other those at odd positions, so the halves stay balanced as tags join. Each
      writes the stub file with `shadow_diff.py stubs` and renders its tags twice into
      `tmp_path`, each time in a subprocess with a different `PYTHONHASHSEED`. Together they
      cover all 56 tags. Each subprocess installs an
      audit hook before importing the renderer. The hook refuses any `open` outside the repo,
      the output directory and the interpreter's own prefixes, and any `socket.connect`. The
      hook lives in the subprocess because Python cannot remove an audit hook, and one
      installed in an xdist worker would outlive the test. The subprocess also inherits
      `leakguard`'s PATH shims.
    - **The digest.** When `GITHUB_STEP_SUMMARY` is set, each module appends a `render
      digest at $GITHUB_SHA` heading and one line per directory it rendered, computed by
      `shadow_diff.py digest`'s function. The shards are separate matrix jobs, so the two
      modules may write to two job summaries. A per-directory line does not depend on which
      other tags were rendered, so the union of the two summaries is the full set, and no
      step has to join them. Neither module writes the `total` line, which only a full
      render can compute. `.github/workflows/ci.yml` does not change. R1 therefore adds no
      step to the six-leg `pytest` job, no job for the gate to read, and no workflow edit,
      which the agent's token cannot push. On a `pull_request` or `merge_group` run
      `GITHUB_SHA` is a synthetic merge commit, so the lines to compare are the ones from
      the push run on master.
    - **Its budget.** The floor is the heaviest module in
      `scripts/dev/pytest_shard_weights.json`, `test_entry_points_answer_help.py` at 30.78s
      at the baseline. The `ci.yml` comment's 17.8s is stale. `test_validate_k8s_manifests.py`
      renders every role once in process at 15.46s, so one module rendering all 56 tags
      twice, with two ansible-core imports, would sit near the floor and become the pole of
      the job. That is why the test is two modules from the start: `--dist loadscope` keeps
      a module on one worker, so only separate modules spread the cost. Record both weights
      with `uv run python scripts/dev/pytest_shard.py --record-missing`. Each must record
      under 20s, about two thirds of the floor. If one does not, split into three by
      position modulo three. The per-directory digest lines make any split comparable.

## Verification

**The shadow pair** is **operator-only**. It runs on daniel-box as `ubuntu`, from one clean
worktree of the operator's checkout at a merged master SHA X, so both halves read the same
commit. `deploy.sh --dry-run` renders the working tree, which must be clean. Ansible refuses
non-blocking stdio, so the dry run redirects all three streams.

```bash
# Operator-only: as ubuntu, in a clean worktree of the operator's checkout at master SHA X.
mkdir -p /var/tmp/homelab-shadow/X
uv run python scripts/renderer/shadow_diff.py stubs \
  --out /var/tmp/homelab-shadow/X/stubs.yml
uv run python scripts/deploy_tools/render_targets.py daniel-box
# Pass the comma list the line above printed as <tags>.
./scripts/deploy.sh --dry-run --skip-staleness-check --tags "<tags>" \
  -e @/var/tmp/homelab-shadow/X/stubs.yml \
  -e manifests_shadow_dir=/var/tmp/homelab-shadow/X/ansible \
  > /var/tmp/homelab-shadow/X/play.log 2>&1 < /dev/null
uv run python scripts/renderer/homelab_render.py cluster \
  --out /var/tmp/homelab-shadow/X/render \
  --secret-stubs /var/tmp/homelab-shadow/X/stubs.yml
uv run python scripts/renderer/shadow_diff.py compare \
  /var/tmp/homelab-shadow/X/ansible /var/tmp/homelab-shadow/X/render
```

The last command must exit 0 and print `<t> tags, <n> files, 0 differences`, where `<t>` is
the Ansible-owned tag count at X: 56 until D2 hands a tag to Flux. It refuses to report
success over an empty set. It also refuses when it finds no file for one of three named
members: `traefik/deployment.yaml`, `pihole-instance-2` and a `claim-*.yaml` file. A named
member whose tag is flux-owned at X is dropped from that check, and `compare` prints which.

To give PR 7's loop its reference tree, after the dry run exited 0, the operator runs
`stubcheck` and copies only if it exits 0. A file that fails it holds no stub where the C1
render holds one. The operator inspects that file locally and never copies or posts it.

```bash
# Operator-only: check the Ansible tree's stub tokens, then copy it into the agent's home.
uv run python scripts/renderer/shadow_diff.py stubcheck \
  /var/tmp/homelab-shadow/X/ansible /var/tmp/homelab-shadow/X/render
sudo install -d -o claude -g claude -m 0700 /var/lib/claude/shadow
sudo cp -r /var/tmp/homelab-shadow/X/ansible /var/lib/claude/shadow/X
sudo chown -R claude:claude /var/lib/claude/shadow/X
```

The agent then runs, on a branch based on X that changes only `scripts/renderer/`:

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \
  --secret-stubs /tmp/r1-stubs.yml
uv run python scripts/renderer/shadow_diff.py compare /var/lib/claude/shadow/X /tmp/r1-render
```

**Determinism and host independence.** These need no secrets, and anyone may run them:

```bash
uv run pytest scripts/renderer/tests
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1 \
  --secret-stubs /tmp/r1-stubs.yml
uv run python scripts/renderer/shadow_diff.py digest /tmp/r1
```

Each per-directory line must equal the matching line in the job summaries of the push run
on master for the same commit. The two determinism modules may write their lines to two
shards' summaries, and together they hold every directory. The `total` line is for a
whole-tree comparison, such as two full renders of one commit.

**Red-proofs, one per new check:**

- `shadow_diff.py compare`: a test builds two trees in `tmp_path`. A one-byte change, a
  missing file and an extra file are each flagged and exit 1. Identical trees exit 0. An
  Ansible-side directory for a tag outside the Ansible-owned set is reported as extra, and
  a flux-owned directory on the C1 side is ignored. An empty set exits 1. A live proof edits
  one template in a scratch worktree, renders only the renderer half, and must see that file
  named.
- `shadow_diff.py stubcheck`: an Ansible-side file in which one stub token is replaced by
  another string fails, names the file and the missing token, and exits 1. The same case
  inside a `kind: Secret` document's base64 `data` value fails too. Identical trees pass.
  `compare` over the failing pair names the file and prints no hunk for it.
- `shadow_diff.py digest`: one tree copied under two absolute directories gives the same
  lines, and a one-byte change moves exactly one directory's line and the `total` line. A
  render of half the tags gives the same lines for those tags as a full render.
- The template filter census (PR 2): a fixture template using an unregistered filter is
  flagged, and the real tree passes.
- The determinism test: a test hands its comparison two trees that differ in one file and
  must see it fail. A fixture render step that opens `/etc/hostname` is refused by the audit
  hook, and one that opens a repo file passes.
- The preamble refusal: a test renders the assert with ansible-core's `Templar`. These pass:
  stubs with `k8s_dry_run` and the play variable, and no stubs at all. These fail: stubs
  without `k8s_dry_run`, and stubs with `k8s_dry_run` but no play variable, which is the
  `initial_setup.yml` case. The Docker play's expression is false for daniel-pi's list and
  true for daniel-box's.
- The preamble content assert: a stub file missing one key fails and names that key. A
  complete file passes. The sentinel without the file fails.
- The stub-run trigger: it is false for a context with no stub file. It is true for a stub
  file stripped of its sentinel, which the refusal then stops outside a k8s dry run.
- The role's shadow-dir assert, rendered with `Templar` over a context that stands for the
  state after the SOPS load. `k8s_dry_run` with every key stubbed passes. These fail:
  `k8s_dry_run` with real-shaped values and no stub file; the same context with
  `homelab_secret_stubs_verified: true` and the sentinel added, the case a flag-reading
  assert would pass; and a stub map in which one key holds another value, whose message
  names that key and not its value.
- The home-assistant stub: the `Templar` render in PR 1a, whose red case is the old
  one-key form.
- The reader tags of `PINNED_LIVE_FACTS`: a fixture template under a tag the constant does
  not list, naming `pi_peer_backup_known_hosts_line`, is flagged.
- The secret-key task census: a fixture `uri` task that names a secret key with no guard is
  flagged. The same task with `when: not (homelab_secret_stubs | default(false) | bool)` is
  clean. A file imported under `when: not k8s_no_mutate` is clean. A task naming a derived
  variable is flagged. The census must find "Hash the Authelia password" (exempt) and "Read
  the cluster copy's root folders" (guarded), its two named members.
- The out-of-render census row: its red fixture holds an unlisted `kubectl apply` and its
  green one a `kubectl get`. `must_find` holds `monitor-bridge`'s `kubectl create configmap`
  file.
- The content-tag parity: the mutation test in PR 3.

## Rollback

- **PRs 1a, 1b, 2, 3, 7 and 8** are additive. A revert removes code that nothing live
  reads, with two qualifications. Ansible loads PR 1a's filter plugin in every play. A
  plugin that fails to import would therefore reach every play, which is why it stays
  standard-library only and a PR 1a test imports it. And once PR 5 has merged, the
  preamble calls the plugin's filters, so PR 1a cannot be reverted alone; revert PR 5 first.
- **PR 4** adds guards that only a stub file triggers. A revert restores the reads under a
  stub file, which breaks only the shadow run.
- **PR 5** is inert without a stub file. Its revert is again a deploy-plane change, so the
  tick defers it the same way and records the same ledger lines.
- **PR 6** is inert with `manifests_shadow_dir` unset, so a revert moves no rendered byte.
- **A partial failure** leaves stub-only directories and nothing in the cluster, since the
  dry run applies with `--dry-run=server`. A preamble refusal stops the play before any role
  renders. A play that fails partway renders only the services before the failure, and
  `compare` reports the rest as missing rather than as clean. The operator deletes
  `/var/tmp/homelab-shadow/X` and the agent's copy under `/var/lib/claude/shadow/`.

## Risks

- **A stub file reaching a real run** would apply placeholder credentials. The preamble
  refuses it in every playbook that loads secrets: `deploy.yml`'s Docker play aimed at a host
  with Docker entries, `initial_setup.yml`, `k3s-bringup.yml`, `k3s-storage-smoke.yml`,
  `preflight.yml`, `prune_backups.yml` and `seed_volume_backup.yml`. It keys on stubbed
  values as well as on the sentinel, so a stub file stripped of its sentinel is refused too.
  PR 5's red-proofs hold each case.
- **A stub render published as the cluster artifact** (failure class 2). Every stub but the
  home-assistant object is a valid DNS label, so a stubbed IngressRoute passes server-side
  validation and nothing in the apply fails, while its Host rule names `stub-domain` and
  matches nothing. R1's mitigation is the default: `cluster` without `--secret-stubs` exits
  2 and writes nothing, and R1 writes no `flux/` file. The guards after R1 belong to D2 and
  R2: a publish in placeholder mode, a census that refuses stub tokens, and a refusal of a
  flux-owned tag that reads a pinned fact (Open question 7).
- **A pinned fact published through placeholder mode** (failure class 2). R2's placeholder
  mode reads `PINNED_LIVE_FACTS` until R3 empties it, so a flux-owned `authelia`, `n8n` or
  `pi-peer-backup` would publish a stand-in. `k8s_built_images: []` renders `"unstaged"`,
  which no value census can see. The protection must not rest on the slice index's D5 → R3
  edge alone. R1 records the reader tags and holds them to the templates. R2's tag rule and
  D2's token census are the guards (Open question 7).
- **Failure class 2 (the check observes a proxy).** A sentinel says a stub file was passed.
  It does not say every value is a stub. The preamble checks every key's value against its
  stub form. The manifests role repeats that check on the values it is about to render, so
  neither the sentinel nor a flag passed as an extra var can stand in for it. Zero diff
  also proves equivalence for stub inputs only. A template branch on a secret's value takes
  the same path for any non-empty stub, while an empty real value takes the other. R2 removes
  those branches.
- **A stubbed task acting live.** A task that sends a secret value to a live system under a
  dry run would send a stub. Sonarr's two reads are the measured case: a 401 stops the play.
  Authelia's hashing tasks would start pods without the pinned hash facts. PR 4 guards
  Sonarr, and PR 5's census refuses a new unguarded task, derived variables included.
- **Failure class 3 (empty read as clean).** A compare over no files passes vacuously.
  `compare` refuses an empty set and names three members it must find.
- **Failure class 4 (selector drift).** Both halves take their service list from
  `render_targets`, so a service it drops is dropped from both. The out-of-render census
  reuses the dry-run census's write rule rather than a second regex, which matched 4 roles
  the rule does not count and missed 6 it does.
- **Failure class 7 (execution context).**
    - The Ansible half needs the operator's age key and sudo. An agent session cannot read
      `/var/tmp` or `/home` as the operator's shell wrote them. That is why the pair is
      operator-only, the tree is owned by `sys_user`, and the agent gets an explicit copy.
    - The renderer runs on the CI runner and on daniel-box. The audit hook and the
      cross-host per-directory digests catch a hidden host read.
- **Pinned live reads hide renderer gaps** for those five inputs. R3 removes them, and its
  exit criterion repeats the shadow pair without them.
- **An ansible-core bump can move Ansible's bytes** after R1 passes. The operator reruns the
  shadow pair before D2, before each D5 batch, and on any Renovate PR that bumps
  `ansible-core` in `uv.lock`. After D2 hands a tag to Flux, C4 has the Ansible play skip it.
  `compare` then derives a smaller set at that commit, so a rerun checks only the tags
  Ansible still owns.
- **The PR 5 landing leaves ledger lines.** Every declared service gets a `k8s_unapplied`
  line that does not page. Most discharge on their own. An executor stops at
  `needs-manual-apply` rather than running an unscoped `deploy.sh`, which is the run #4333
  removed.
- **A plugin edit after PR 5 defers every service.** PR 5 names the stub filters in the
  preamble, so `broad.py:_sort_hits` refuses to narrow any later `secret_stubs.py` edit. Each
  such edit lands like PR 5: a `k8s_unapplied` line for every declared service, no receipt,
  and `needs-manual-apply`. It also changes the stub file, and so both halves, and needs a
  fresh operator shadow run at a SHA that holds it. Mitigations: every stub shape R1 knows
  of, the home-assistant object included, lands in PR 1a, before PR 5. R2's stub changes and
  R3's PHC-shaped stubs inherit this landing, and each of those slices names it.
- **Server-side validation of a stub.** If `--dry-run=server` rejects a stub value, that key
  gets a shape-valid stub in `secret_stubs.py`, and both halves use it. After PR 5 that fix
  lands as the deferral above and needs a fresh shadow run.
- **A real value in a posted or copied tree.** The posted `compare` output and the agent's
  copy must hold only stubs. The role assert refuses a shadow directory unless every key
  holds its stub, `compare` withholds the hunk of any file that fails `stubcheck`'s rule,
  and the copy runs only after `stubcheck` exits 0. `stubcheck` covers only paired files
  that hold a token, so for every other file the role assert is the one guard.

## Tests and docs that change in the same PR

- PR 1a adds `docs/homelab-render.md`: what the renderer reads, the C1 layout, and how to
  run it. Each later PR extends it. The same PR adds its nav entry to `mkdocs.yml`, under
  *Operations*, *Deploy and land*, because
  `scripts/tests/test_mkdocs_config.py:test_every_doc_is_either_in_the_nav_or_explicitly_excluded`
  fails on a page with none.
- PR 1b refreshes the weight of `scripts/lib/tests/test_entry_points_answer_help.py` in
  `scripts/dev/pytest_shard_weights.json` with `pytest_shard.py --record-files`, since R1's
  two entry points join that module.
- PR 4 adds a line to `ansible/roles/k8s/sonarr/CLAUDE.md`: `verify.yml` skips its API reads
  under a stub file.
- PR 5 states each preamble assert's reason in a comment at the assert in
  `load_secrets.yml`. It runs `uv run pytest ansible/tests/deploy` for the tests that read
  the preamble, such as `test_playbooks_gather_through_the_preamble.py`. The asserts go
  ahead of the SOPS load, and the fact gather stays last.
- PR 6 adds a `manifests_shadow_dir` bullet to `ansible/roles/k8s/manifests/CLAUDE.md`, under
  *The caller's contract*. It adds a paragraph to `docs/k8s-manifest-cycle-record.md`, *What
  a green `--dry-run` does not cover*, on how the shadow run differs from a plain dry run.
- PR 7 adds the shadow-pair procedure to `docs/homelab-render.md`, marked operator-only.
- PR 8 moves `_chunk_mutates` and its patterns from `test_k8s_dry_run.py` into
  `_k8s_guards.py`. `test_k8s_dry_run.py` imports them, and no assertion changes. It records
  the two determinism modules' weights in `scripts/dev/pytest_shard_weights.json`.
- Every other test is listed with its PR above. `scripts/validate/` keeps its own render
  until R4.

## Complexity delta

- **Added:** about 700 lines of renderer, comparison tooling and filter plugin, and about
  600 lines of tests. In Ansible: one role variable, three asserts (two in the preamble, one
  in the manifests role) and three filters, one play variable in each `deploy.yml` play,
  and four `when` guards in sonarr. One census row, one census module and two CI-weighted
  test modules. `shadow_diff.py` has four subcommands.
- **Removed:** nothing yet. R1 creates a second render path on purpose. R4 removes the
  Ansible one, and with it the offline render loop in `scripts/validate/k8s_manifests.py`.

## Open questions

1. **Which bytes differ first?** Trailing newlines, `lookup('template')` whitespace and
   filter-argument defaults are the likely classes. *Resolution:* PR 7's first shadow run.
1. **Does pinning `k8s_built_images` with `-e` disturb image-builder under a dry run?** An
   extra var outranks every `set_fact`, so the role's own write is ignored. *Resolution:* the
   first shadow run's play log. If a task fails, exclude n8n's two annotation lines from the
   comparison by name until R3 removes them.
1. **Should the shadow pair run on a schedule until R4?** The hourly render-record producer
   uses real secrets for its digests, so it cannot carry the stub run. *Resolution:* decide
   after D2. The default is the operator's reruns listed under Risks.
1. **Where does the Namespace and LimitRange go?** It belongs to no tag. *Resolution:* D2 or
   D4 names its Kustomization. R1 reports it as a contract gap.
1. **R1 extends four C1 rows and one C3 row.** Each needs either a contracts.md change or an
   R2 change.
    - *C1 Modes.* R1 adds `--secret-stubs FILE` to `cluster`. R2's *Mode selection* keeps
      that flag and adds none: with it the render is in stub mode, and without it the render
      is in placeholder mode. *Resolution:* contracts.md C1 *Modes* records `cluster --out
      DIR [--role TAG ...] [--secret-stubs FILE]`, and says that stub mode is for
      comparison only.
    - *C1 Secret references.* Without `--secret-stubs`, R1's `cluster` exits 2 rather than
      rendering `${<key>}`. *Resolution:* R2 delivers the placeholder default, and
      contracts.md does not change. D2's publish depends on that default, which is why
      Open question 7 asks for a D2 → R2 edge.
    - *C1 Cluster output.* R1 writes `DIR/cluster/<tag>-deferred/` for the three deferred
      callers. C3 names a `<tag>-deferred` Kustomization, but C1 lists only
      `DIR/cluster/<tag>/`. *Resolution:* contracts.md C1 *Cluster output* gains the
      deferred directory, with C3's path pointing at it.
    - *C1 Entry point.* `shadow_diff.py` is a second entry point beside the one CLI.
      *Resolution:* contracts.md C1 states that comparison and stub tooling beside the
      renderer renders nothing and is not a second renderer.
1. **When can an agent run the Ansible half?** [The agent-user page](../../claude-agent-user.md)
   slice 7, a pre-merge dry run without secrets, is optional, and parts of it are
   unverified. *Resolution:* R1 does not wait on it. If slice 7 lands, revisit the
   operator-only marking and the copy hand-off.
1. **What does D2 publish, and where does `domain` come from before D3?** R1 alone cannot
   feed D2's publish job (*Stub output is never published*). R1 changes no interface for
   this. Other documents do (contracts.md, D2, R2 and R3), and each change is a cross-slice
   note:
    - *The dependency edge.* The contracts.md slice index row for D2, and D2's header, gain
      R2. R2's Open question 8 asks for the same edge. *Owner:* the contracts.md maintainer
      and D2.
    - *D2's publish.* Its publish command says it renders in placeholder mode, which under
      R2 is the call without `--secret-stubs`. Its `flux/` writer skips stub mode. Its
      publish census also refuses any rendered string that contains a stub token: `stub-`
      followed by a `secrets.yml` key name or a `PINNED_LIVE_FACTS` name with underscores
      turned to hyphens, as `ansible/filter_plugins/secret_stubs.py`'s
      `homelab_secret_stub` builds it. A test of "equals the stub form" would miss both
      `bento-pdf.local.stub-domain` and the three home-assistant field values, which hold
      the token inside a longer string. That census has no false positive at the baseline:
      `grep -rl 'stub-' ansible/roles/k8s ansible/templates ansible/inventory` prints
      nothing, and those trees are where rendered values come from. It cannot see
      `k8s_built_images: []`, which renders `"unstaged"`, nor a stand-in without the
      marker, such as R3's PHC-shaped hashes. *Owner:* D2.
    - *R2's placeholder mode.* Until R3 empties `PINNED_LIVE_FACTS`, placeholder mode reads
      its stand-ins. It refuses, with exit 1, a flux-owned tag that the constant lists as a
      reader of a fact it still pins, and it names the fact. A tag that Ansible still owns
      renders as before, so R2's round trip keeps all tags. This is the only guard that sees
      `k8s_built_images`, and it makes the protection independent of the slice index's
      D5 → R3 edge. *Owner:* R2.
    - *Plugin edits after R1's PR 5.* Each edit to `secret_stubs.py`, such as R2's stub
      changes or R3's PHC-shaped stubs, defers every declared service and needs a fresh
      operator shadow run (*Risks*). *Owner:* R2 and R3, each in its own landing text.
    - *The source for `domain`.* C3 names a source for `domain` that exists before D3.
      Writing `domain` in plaintext into a tracked Flux object or the artifact is a decision
      about a SOPS-encrypted key, so it is **operator-only**. CI holds no age key (C3), so
      until that source exists, a check before `prod` moves cannot compare the Host rule
      with the real domain. It can assert that `${domain}` is present and that a
      substitution source for it is declared. The route load through Traefik after the
      promotion is what sees the real host. *Owner:* the contracts.md maintainer, D2 and
      the operator.

## Review log

### Round 1

Findings raised, all fixed. Each fix was checked against the tree at `70591c307` before it
was written. Where the evidence corrected a suggested fix, the entry says so.

- **R1-H1 (high): Sonarr's verify tasks send the stub API key to live Sonarr, get a 401 and
  abort the stubbed dry run.** Fixed.
    - Confirmed in `sonarr/tasks/verify.yml` and `sonarr/tasks/main.yml`.
    - A YAML walk of every k8s task, extended to derived variables, found one derived
      variable and no task naming it. It confirmed that sonarr's two reads and authelia's
      three hashing tasks are the only unguarded acting cases. *Baseline* records the walk.
    - PR 4 guards the two reads and the two asserts on `homelab_secret_stubs`, not on
      `k8s_dry_run`. PR 5's census names "Read the cluster copy's root folders" as a member
      and has a red-proof for the unguarded form. The census also accepts the
      `homelab_secret_stubs` guard, which `_GUARD_FACT` alone would not.
- **R1-H2 (high): PR 4's landing premise is wrong.** Fixed.
    - Confirmed through `_BROAD_PLAY_PREFIXES`, `broad_path_tags`, `_deploy_plane` and
      `_deferred`. `_BROAD_CENSUS_PREFIXES` holds only `inventory/` and `templates/`.
    - PR 5 now states the real consequence: a fast-forward, a `k8s_unapplied` line per
      declared service, no receipt, and `needs-manual-apply`. It says how the lines clear
      and who decides on the rest, and it forbids an unscoped `deploy.sh`.
    - The hold and window text is gone. The play-level placement is justified by what it
      must precede, not by cost.
    - The manifests change moved to its own PR 6, whose single `manifests` line discharges by
      render proof.
- **R1-H3 (high): the Ansible half decrypts SOPS and needs sudo, yet is not marked
  operator-only, and its hand-off does not work.** Fixed.
    - The Ansible half and `compare` over its output are operator-only and run from the
      operator's checkout at a merged master SHA.
    - The operator posts `compare`'s output, and copies the tree into the agent's home after
      a run that exited 0. `PrivateTmp=yes` covers `/var/tmp` as well as `/tmp`, so a shared
      `/var/tmp` path would not work.
    - PR 7's loop runs renderer-only branches against one operator run at X.
    - Failure class 7 is in *Risks*. Slice 7 of the agent-user page is an open question, not
      a dependency, because it is optional and unverified.
- **R1-H4 (high): the shadow-dir assert checks a sentinel, not the stub values.** Fixed.
    - The preamble reads the key names from the ciphertext file and asserts each value
      equals its stub form, before the SOPS load, so it never sees a real value.
    - The stub form lives in one filter plugin that the renderer also imports, so no second
      copy of the rule exists to drift.
    - The role assert reads the preamble's `homelab_secret_stubs_verified`, not the
      sentinel. The red-proofs cover a file missing one key, a complete file, and the bare
      sentinel.
- **R1-M1 (medium): the renderer's Jinja environment lacks filters.** Fixed.
    - A parse of all 294 templates confirmed `homelab_role_label` and `from_json`.
    - `from_yaml` appears in no template, only in image-builder's content-tag expression, so
      PR 3 registers it where that expression is evaluated.
    - PR 2 adds the template filter census with a red fixture. The renderer also uses a
      strict undefined, so a missing input cannot pass as `STUB` in both trees.
- **R1-M2 (medium): `render_context`'s hostvars cannot supply what three templates read.**
  Fixed. `context.py` builds `hostvars` as the finding asks, and PR 1 tests one member of
  each case.
- **R1-M3 (medium): the cross-host digest hashes absolute paths and sorts by locale.** Fixed.
  `shadow_diff.py digest` hashes relative paths in bytewise order. CI and the operator run
  the same function, and a red-proof copies one tree under two directories.
- **R1-M4 (medium): leakguard does not fail any host access.** Fixed.
    - The exit criterion now names what is checked.
    - Each render runs in a subprocess with an audit hook on `open` and `socket.connect`. A
      subprocess is used because an audit hook cannot be removed and would outlive the test
      in an xdist worker.
    - The red fixture opens `/etc/hostname`.
- **R1-M5 (medium): the kubectl census pattern misses roles and overlaps an existing census.**
  Fixed, with a corrected count.
    - Measured by the dry-run census's own rule, `_chunk_mutates`, plus `kubectl run`, the
      set is 22 roles, not the finding's 25.
    - The broader regex also counts four roles that the rule finds no write in. The rule
      confirms the finding's five additions and adds `volume-revert`.
    - PR 8's census is a `Census` row reusing the moved rule, with `cronjob-gate` and
      `qbittorrent` among its `must_find` members.
- **R1-M6 (medium): the secret-key census's guard rule flags guarded tasks and misses
  derived variables.** Fixed. The census accepts `k8s_no_mutate`, `k8s_dry_run` and
  `homelab_secret_stubs`, on the task or at its include site through `_guard_covered_files`.
  It widens the key set to derived variables, of which the tree holds one. Red and green
  fixtures cover the import guard.
- **R1-M7 (medium): the CI step and determinism module land on the sharded pytest job.**
  Fixed differently from both suggestions.
    - `ci.yml` does not change. The determinism module writes its digest to the job summary.
    - A new job would need a three-file gate edit. A workflow edit needs the operator's push.
    - The module's weight is budgeted under the 17.8s floor, with a split rule if it goes
      over.
- **R1-M8 (medium): the rerun guidance conflicts with C4.** Fixed.
    - `compare` derives its set at the commit under test, from `render_targets` minus
      `owner: flux`. It drops a flux-owned named member and ignores flux-owned directories on
      the C1 side.
    - Two C1 trees compare over the full set, which keeps R4's render-to-render use working.
- **R1-M9 (medium): C1 interface extensions are not raised in Open questions.** Fixed. Open
  question 5 names C1 *Modes*, *Secret references*, *Cluster output* and *Entry point*, and
  C3, each with its resolution. R1's `cluster` exits 2 without `--secret-stubs`.
- **R1-M10 (medium): the sentinel assert sits only in deploy.yml.** Fixed, with a tighter
  predicate.
    - The refusal moved into `load_secrets.yml`, ahead of the SOPS load.
    - The finding's predicate, stubs without `k8s_dry_run`, would still pass
      `initial_setup.yml -e k8s_dry_run=true` and a Docker play aimed at daniel-pi. The
      refusal therefore also requires `load_secrets_stubs_allowed`, a play variable only
      `deploy.yml` sets.
    - The refusal triggers on any key already holding its stub value, not on the sentinel
      alone, so a stub file stripped of its sentinel is refused as well.
    - *Risks* names every importing playbook.

Also changed this round, outside the findings:

- `shadow_diff.py stubs` and `PINNED_LIVE_FACTS` moved into PR 1, because `cluster` refuses
  to run without a stub file. Each PR now runs end to end from the CLI. The template filter
  census and the two filter registrations moved to PR 2, where the wrapper first needs them.
- PR 4 of the first draft split into three PRs: sonarr's guard (PR 4), the preamble (PR 5)
  and the manifests role (PR 6). Each lands through a different path class, and each has
  its own ledger consequence.
- A grep for gathered facts in the templates and role defaults found none. *Baseline*
  records it, so no sixth live-read class exists.

### Integration round

Two cross-slice findings named R1 among their targets. Both are confirmed against the tree at
`70591c307`, and neither is refuted. R1 fixed its own part of each. The parts that belong to
other documents went out as cross-slice notes.

- **X-01 (high): D2's publish job cannot render bento-pdf: no stub file, and no source for
  `domain` before D3.** Fixed in R1's part.
    - Confirmed. `grep -c '^domain:' ansible/vars/secrets.yml` prints 1, so `domain` is a
      secret key. bento-pdf ships only `deployment.yaml.j2` and takes its route from
      `ansible/templates/ingressroute-default.yaml.j2`. The `ingressroute` macro in
      `ansible/templates/ingressroute.yml.j2` builds the Host rule from `'.local.' ~
      domain`. R1's `cluster` exits 2 without `--secret-stubs`.
    - The *Unblocks* line no longer says that R1 unblocks D2. D2 needs R1 and R2's
      placeholder mode together.
    - A new Design section, *Stub output is never published*, states the rule and the
      measured case. It names R1's two defaults that keep a stub tree out of a publish: the
      exit 2 now also writes nothing, and R1 writes no `flux/` file. PR 1's test checks that
      `--out` stays empty.
    - *Risks* records a stub render published as the artifact as a failure class 2 hazard.
    - New Open question 7 assigns the rest: the D2 → R2 edge in contracts.md and D2's
      header; D2's placeholder-mode publish, a `flux/` writer that skips stub mode, and a
      census that refuses stub values; and C3's pre-D3 source for `domain`, which is
      **operator-only**.
    - The finding's red proof, "a Host rule that lacks the real domain fails before `prod`
      moves", is not possible as written while CI holds no age key (C3). Open question 7
      states what a check before promotion can assert, and the D2 note carries it.
- **X-17 (medium): R4 PR 9 deletes `shadow_diff.py` and handles only its `stubs` CI call,
  not R2's `roundtrip` check.** Fixed in R1's part, with one correction to R4's premise.
    - R4's premise is wrong. R1 adds no workflow step: PR 8 states that
      `.github/workflows/ci.yml` does not change. Within R1, `stubs` and `digest` are
      called by the operator's shadow run and by the determinism module in
      `scripts/renderer/tests/`. R2 adds the `roundtrip` step to `ci.yml`.
    - The finding misses a second consumer. If R4 deletes `shadow_diff.py` "and its tests",
      the determinism module loses its stub writer and digest function. That module is the
      test behind C1's byte-identical rule.
    - The `shadow_diff.py` design bullet now says that only `compare` reads an Ansible
      tree, that `stubs` and `digest` outlive the Ansible path, who calls each, and that the
      two must move or stay before R4 deletes the file.

Also changed this round, outside the findings:

- Open question 5's *C1 Modes* bullet still described R2's dropped `--secrets
  {placeholder,stub}` flag. It now records R2's *Mode selection*: `--secret-stubs FILE`
  selects stub mode, and the call without it is placeholder mode.

### Round 10

Six findings raised: one high and five medium. All six are fixed, and none is refuted. Each
was checked against the tree at `70591c307` before the fix was written. Where the evidence
changed the suggested fix, the entry says so.

- **R1-R10-H1 (high): the JSON stub for `google_assistant_service_account` breaks
  home-assistant's render in both halves, so exit criterion 1 cannot be reached.** Fixed.
    - Confirmed. `home-assistant/templates/config/secrets.yaml.j2` reads `ga.project_id`,
      `ga.client_email` and `ga.private_key` after its `from_json`. The template text was
      wrapped in `trust_as_template` and rendered through ansible-core 2.21.5's `Templar`.
      The one-key form raised `AnsibleUndefinedVariable: object of type 'dict' has no
      attribute 'project_id'`, and an object holding the three keys rendered.
    - Without `trust_as_template`, both forms came back as unrendered text. A test that
      omits the wrapper therefore cannot go red, and PR 1a's test names the wrapper.
    - The finding's scope grep holds. A secret key followed by `.` or `[` matches nothing
      else but the `email.json:` key in uptime-kuma's `static-monitors.yaml.j2`.
    - The stub is a sorted-key JSON object with exactly the three fields, each valued
      `stub-google-assistant-service-account-<field>`. `homelab_secret_stub` returns that
      string, and `stubs` writes it as a quoted YAML string, so `-e @FILE` loads a string
      and not a mapping. *Baseline* records the measurement.
    - PR 1a's tests check the object's shape and the YAML reload as a `str`. They also
      render the template with the stub map, with the old form as the red case.
- **R1-R10-M1 (medium): from PR 5 on, every edit to `secret_stubs.py` lands as a deferral of
  every service.** Fixed with both halves of the suggestion.
    - Confirmed in `narrow_lib/broad.py`. `_grep` scans `PLAY_PREFIXES`, which holds
      `ansible/pre_tasks/`. `narrow_lib/filters.py:callers` drops only `.md` files and plugin
      `.py` files. `_sort_hits` raises `CannotNarrow` on a play-prefix hit.
    - The design bullet now says the plugin maps to no tag only until PR 5.
    - PR 1 split into PR 1a and PR 1b, and PR 1a lands the plugin complete. That covers the
      home-assistant shape and the comparison filter that PR 5 and PR 6 call, all before
      PR 5.
    - PR 5's landing text, PR 7 and *Risks* state the deferral for later plugin edits, and
      the fresh operator shadow run a stub-shape change needs. *Risks* and Open question 7
      name R2's and R3's plugin edits as inheriting it.
- **R1-R10-M2 (medium): PR 1 adds `docs/homelab-render.md` without a mkdocs nav entry.**
  Fixed. Confirmed in `mkdocs.yml`'s comment and in
  `scripts/tests/test_mkdocs_config.py:test_every_doc_is_either_in_the_nav_or_explicitly_excluded`.
  PR 1a adds the entry under *Operations*, *Deploy and land*. The file tree and the same-PR
  docs list name `mkdocs.yml`.
- **R1-R10-M3 (medium): an extra var `homelab_secret_stubs_verified` bypasses the shadow-dir
  assert.** Fixed through the finding's second option.
    - Confirmed. The role assert read a flag, and an extra var outranks a `set_fact`.
    - The first option, refusing a run that defines the flag before the preamble sets it,
      would refuse a real shadow run. Both plays of `ansible/deploy.yml` target the same
      host, so a fact set by the first play's preamble is still defined in the second.
    - The role assert now recomputes the stub check with `homelab_unstubbed_keys`, and
      `homelab_secret_stubs_verified` no longer exists. A red-proof passes that flag and the
      sentinel without a stub file and must fail.
    - On the second layer, the suggested predicate, "every Secret value is a stub form", is
      not checkable. A composed value, such as an Authelia config line, holds literals
      around its stub. `stubcheck` checks a predicate that is: every stub token in the C1
      file appears in the Ansible file, with Secret `data` decoded on both sides. It gates
      the copy, and `compare` withholds the hunk of any file that fails it.
- **R1-R10-M4 (medium): `PINNED_LIVE_FACTS` stub values reach R2's placeholder mode.** Fixed
  in R1's part.
    - Confirmed. `authelia`, `n8n` and `pi-peer-backup` read the five facts. n8n passes
      `k8s_built_images` to `checksum_annotation`, whose `default('unstaged')` turns `[]`
      into `"unstaged"`, a value no census can match to a stub.
    - `PINNED_LIVE_FACTS` records its reader tags, and a PR 1a test holds them to a grep of
      the templates. String stand-ins carry the `stub-` marker.
    - *Stub output is never published* and *Risks* cover the pinned facts.
    - Open question 7 assigns the tag rule to R2, the only guard that sees
      `k8s_built_images`, and the token census to D2.
    - The same note's census wording changed from "equals a key's stub form" to "contains
      a stub token". Equality missed `bento-pdf.local.stub-domain` and the home-assistant
      field values.
- **R1-R10-M5 (medium): the split rule breaks exit criterion 2's full-tree digest, and the
  17.8s budget is stale.** Fixed.
    - Confirmed in `scripts/dev/pytest_shard_weights.json`, which records 30.78s,
      17.77s and 15.46s for the three modules the finding names.
    - `digest` prints one line per directory under `cluster/`, then a `total` line that
      keeps the whole-tree value R4 reads. Each module writes its directories' lines, so
      any split stays comparable without a step that joins two shards' summaries.
    - The test is two modules from the start, because `--dist loadscope` keeps one module
      on one worker. The budget is stated against the measured 30.78s floor, with each
      module under 20s.
    - PR 1b refreshes the help module's weight, since R1's two entry points join it.

Also changed this round, outside the findings:

- PR 1 became PR 1a and PR 1b to stay near the 200-line guide. Item 1 of *Steps* holds
  both, so the later PR numbers do not move. A grep of `docs/target-architecture/` found no
  other slice that cites an R1 PR number.
- `PINNED_LIVE_FACTS` has a named home, `renderer_lib/task_inputs.py`, which R2's
  placeholder mode can import.
- *Baseline* records the stale `ci.yml` floor comment and the mkdocs nav rule.
