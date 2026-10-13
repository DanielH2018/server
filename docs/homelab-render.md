# The homelab-render shadow renderer

`homelab-render` renders the k8s manifests from the git tree alone, as a shadow of the
Ansible manifests role. Target slice R1 builds it and proves it byte-identical to Ansible for
the same stub inputs. Target slice R2 adds placeholder mode, which renders each secret
reference as Flux's `${<key>}`. Ansible stays the only writer to the cluster: the renderer
writes only to the directory it is given.

The plans are [R1, shadow render with zero diff](target-architecture/render/r1-shadow-render.md)
and [R2, secret placeholders](target-architecture/render/r2-secret-placeholders.md). C1 in
[the contracts](target-architecture/contracts.md) defines the interface. Each slice lands in
steps, and this page describes each piece once its PR has merged.

## What is in place

- `ansible/filter_plugins/secret_stubs.py` holds the stub form. It is the one definition
  that R1's renderer and its playbook checks are built to share, so neither restates it.
- `scripts/renderer/shadow_diff.py stubs` writes the stub file both halves of the shadow
  render take.
- `scripts/renderer/renderer_lib/task_inputs.py` holds `PINNED_LIVE_FACTS`, the stand-ins for
  facts a role task reads from live state.
- `scripts/renderer/homelab_render.py cluster` renders in stub mode into the C1 layout. It
  applies the manifests role's file rules (*The file rules*), and renders every k8s render
  target of daniel-box.
- `scripts/renderer/renderer_lib/context.py` builds the variables a role's templates see in
  the deploy play, and `renderer_lib/cluster.py` renders each tag and writes its directories.
- `scripts/renderer/renderer_lib/task_inputs.py:include_vars` reads the `manifests_*` vars
  each role passes to its `k8s/manifests` include.
- `scripts/renderer/renderer_lib/task_inputs.py:derived_facts` computes the values role tasks
  compute from the tree (*Task-computed values*).
- `ansible/roles/k8s/manifests/defaults/main.yml:manifests_shadow_dir` keeps the Ansible
  render of a stubbed dry run (*Keep the Ansible render of a stubbed dry run*).
- `scripts/renderer/shadow_diff.py compare`, `stubcheck` and `digest` compare the two halves
  and hash a render (*Compare the two halves*). `scripts/renderer/renderer_lib/shadow.py`
  holds their library.
- Four determinism modules in `scripts/renderer/tests/` render every tag twice under an audit
  hook (*The render is deterministic and reads only the repo*).
- A census row names every role task file that writes a cluster object itself, which the
  render does not cover (*What the render does not cover*).
- `scripts/renderer/renderer_lib/placeholders.py` holds placeholder mode: the mode call, the
  three passes, the per-directory key sets and `PLACEHOLDER_DEFERRED` (*Placeholder mode*).
- `scripts/renderer/shadow_diff.py roundtrip` builds the placeholder render and the stub render
  with the pinned flux CLI and compares them, and `fluxprobe` checks that CLI on fixtures
  first. CI's `flux_roundtrip` job runs both (*The round trip*).
- `scripts/renderer/renderer_lib/secret_checks.py` holds `PASSTHROUGH_FILTERS`, the filters
  that only interpolate a secret key (*Filters that only interpolate a secret*).

## The stub form

A shadow render never reads a secret. Every key of `ansible/vars/secrets.yml` renders as its
stub instead, which the filter `homelab_secret_stub` builds:

- A key `K` becomes `stub-` plus `K` with underscores turned to hyphens, such as
  `stub-domain`. That shape is a valid DNS label, so a stubbed host name still passes
  `kubectl apply --dry-run=server`.
- `google_assistant_service_account` is the one key a template parses. home-assistant's
  `templates/config/secrets.yaml.j2` runs it through `from_json` and reads `project_id`,
  `client_email` and `private_key`. Its stub is a JSON object holding exactly those three
  fields, each valued `stub-google-assistant-service-account-<field>`, serialised with
  sorted keys.
- `become_password` never takes a stub. It is the one name in `NEVER_STUB`, because
  `ansible/pre_tasks/load_secrets.yml` hands it to privilege escalation and no template
  reads it.

Two more filters serve the stub-file refusals in the playbook preamble (*A stub file stops
outside a k8s dry run*). `homelab_stub_keys` lists the key names that take a stub, from the parsed `secrets.yml`. It
reads keys only, so the ciphertext file is enough. `homelab_unstubbed_keys` returns the keys
whose value is not exactly its stub. It returns names only, never a value.

## Render the cluster tree

`cluster` has two modes, and `--secret-stubs` alone selects between them (C1 *Modes*):

- Without `--secret-stubs`, the render is in placeholder mode (*Placeholder mode*):

  ```bash
  uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r2-render
  ```

- With `--secret-stubs FILE`, the render is in stub mode, R1's shadow mode. Every R1 command
  on this page passes it, so each still renders stubs.

To render the tags in stub mode, write a stub file for the commit and pass it to `cluster`:

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \
    --secret-stubs /tmp/r1-stubs.yml
```

`--role TAG` renders only the tags it names, and it repeats. Without it, `cluster` renders
every k8s render target of daniel-box, the list
`scripts/deploy_tools/render_targets.py` prints.

Each tag becomes `DIR/cluster/<tag>/`. The directory holds the rendered manifests and a
`kustomization.yaml` whose `resources` list them in lexical order, the order
`kubectl apply -f <dir>/` reads a directory in. A tag that defers files also gets
`DIR/cluster/<tag>-deferred/`, with its own `kustomization.yaml`. The renderer writes nothing
else, and `DIR/flux/` belongs to a later slice.

`cluster` exits 2 and writes nothing in four cases:

- In stub mode, the stub file does not stub every key of `ansible/vars/secrets.yml`. The
  message names the keys, never a value. Write a fresh stub file for the commit.
- In placeholder mode, every requested tag is in `PLACEHOLDER_DEFERRED`, so nothing would
  render.
- A `--role` names a tag that is not a k8s render target of daniel-box.
- A directory it would write under `DIR/cluster/` already holds files from an earlier
  render.

It exits 1 when a template fails to render, and it writes nothing then either. It exits 64
on a usage error. `scripts/lib/exit_codes.py` holds the four codes as its `RENDER_*` family.

### The play-time context

A role's templates render with the variables they see in the k8s play on daniel-box:

- role defaults, `group_vars/all.yml` and daniel-box's host_vars, in Ansible's precedence,
  with the stub file's variables over all of them as `-e @FILE` puts them;
- `containers_list` as the play holds it: the k8s entries only, in the dependency order of
  `toposort_containers`. `ansible/deploy.yml` builds it with the same two filters from
  `ansible/filter_plugins/toposort.py`, and templates read that order;
- `container_item`, the tag's own entry from that list, as the play's loop sets it;
- `hostvars` for every host in `ansible/inventory/hosts.ini`. daniel-box's entry is the
  rendered context, so `hostvars[k8s_primary_node].containers_list` is the play's view.
  Another host's entry holds its `hosts.ini` inline variables under its own host_vars,
  resolved against that host's own variables. daniel-pi's `ansible_host`, the Jinja
  `{{ server_ip }}`, therefore resolves to daniel-pi's address, not daniel-box's.

The render is strict. A name that no layer supplies stops the render with an error naming it.
The render guards under `scripts/validate/` print `STUB` instead, and lay stub values such as
`k8s_built_image_tags` under every layer. The renderer does neither: it derives
`k8s_built_image_tags` and the other task-computed values (*Task-computed values*). An undefined attribute
still takes its `| default(...)`, as it does under Ansible.

The same rule holds one level down. A variable whose value names an undefined variable, such
as `x: "{{ no_such_name }}"`, is left undefined rather than resolved to `STUB` or `None`, so a
template that reads `x` stops the render. One that nothing reads costs nothing, as under
Ansible's lazy evaluation. That matters because three `group_vars/all.yml` values name
variables that only setup roles or Ansible itself define. Another host's `hostvars` entry is
stricter: a value there that does not resolve stops the render at once.

### The file rules

The renderer writes the files the Ansible manifests role writes for a tag, by the rules of
its render tasks in `ansible/roles/k8s/manifests/tasks/main.yml`:

- **The file lists.** `scripts/lib/k8s_roles.py:resolved_manifest_files` derives them from
  the role's templates and its `containers_list` entry. A role whose include passes
  `manifests_files` or `manifests_secret_files` gets that list instead, resolved against the
  play-time context. navidrome's list, for one, names `ingressroute.yaml` only while it has
  replicas.
- **The shared defaults.** A listed `service.yaml` or `ingressroute.yaml` that the role ships
  no template for renders from `service-default.yaml.j2` or `ingressroute-default.yaml.j2`
  in `ansible/templates/`. A listed name with no template stops the render.
- **The claims.** Each `k8s_claims` entry renders `claim-default.yaml.j2` to
  `claim-<name>.yaml`, with the entry as `manifests_claim`.
- **The labelling wrapper.** A role that arms `manifests_prune`, the default, renders its
  files, its secret files and its claims through `role-labelled.yaml.j2`. The wrapper stamps
  `homelab/role: <tag>` on every document. authelia and nut opt out at their includes.
- **The deferred files.** `manifests_deferred_files` render from the role's own templates,
  unlabelled, into `DIR/cluster/<tag>-deferred/`. Ansible stages them in a subdirectory
  named by `manifests_deferred_dir_name`. pihole, headlamp and netpol-baseline defer files.

`include_vars` reads the include whose `tasks_from` is absent or `main`, and refuses a role
with none or with two. It keeps the `manifests_*` keys, and the renderer resolves only the
five it reads: the two file lists, the deferred pair and `manifests_prune`.

A template census in `scripts/renderer/tests/` parses every template of the k8s roles and
`ansible/templates/`. It fails when a filter or test name does not resolve in the environment
the renderer renders with.

### Task-computed values

Some values a template or a role default reads are set by a role task, not by a file.
`scripts/renderer/renderer_lib/task_inputs.py:derived_facts` computes them. It reads each
task's expression out of its tasks file and evaluates it in the environment the
templates render in, so the renderer never restates one:

- **Content tags.** Each `include_role` of `k8s/image-builder` gets its tag from the
  expression of the image-builder task `Compute the content tag`. The tag hashes
  `context-configmap.yaml.j2`'s rendered `data`. The build's variables are image-builder's
  defaults under the calling role's context, with the `vars:` of the include over both and
  `playbook_dir` resolving to `ansible/`. An include whose `when:` is false adds no tag, as
  valheim's does without mods. The tags form `k8s_built_image_tags`, which every role reads
  as `{}` when it builds nothing, as `ansible/deploy.yml` resets it.
- **Script checksums.** monitor-bridge, autofix-bridge and game-stats hash their staged
  modules with `lookup('file', ..., rstrip=False) | hash('sha1')`. game-stats' two checksums
  also read `game_stats_lib_checksum`, which `tasks/stage.yml` sets first.
- **`kuma_declarations`.** uptime-kuma's task renders `static-monitors.yaml.j2` and parses
  its `stringData`. The template reads it through `default({})`, so a missing value would
  still render, and a test holds the rendered index above 50 declarations, as the role's own
  assert does.
- **`janitorr_dry_run`**, which janitorr's task copies from `janitorr_k8s_dry_run`.

`SET_FACTS` in the same module lists the roles and facts besides the content tags. A role
default that reads one of these values, such as `nut_k8s_image`, resolves in a second pass of
the context with the values as overrides, the precedence `set_fact` has over every file
layer.

Tests in `scripts/renderer/tests/` hold each derived value equal to the value
ansible-core's `Templar` computes from the same expression and variables. `kuma_declarations`
is the exception, because its render needs the repo's filter plugins.

`lookup('template')` keeps the render's trailing newlines, as ansible-core's lookup does. The
content tag hashes those bytes, so a lookup that stripped them gave every build a tag Ansible
would not compute. `scripts/lib/k8s_yaml.py:make_lookup`, which the render guards under
`scripts/validate/` read through, follows the same rules, so a guard checks the bytes a deploy
writes.

The render is not yet proven byte-identical to Ansible's. `shadow_diff.py compare` reports
each difference (*Compare the two halves*), and each class of difference then gets its own
renderer fix.

## Placeholder mode

Target slice R2 adds placeholder mode, the mode a later publish job runs (C1 *Only
placeholder mode publishes*). `cluster` without `--secret-stubs` renders every secret
reference as `${<key>}`, where `<key>` is the key name in `ansible/vars/secrets.yml`. Flux
substitutes the real values at apply time. Nothing publishes a render yet: target slice D2
adds the publish job, and it waits on R2's PR 2 as well.

`scripts/renderer/renderer_lib/placeholders.py` renders in three passes, because the escape
would also escape a placeholder emitted before it:

1. The context maps each secret key `K` to the sentinel `HOMELAB_SECRET__K__END`. Letters and
   underscores survive `default`, `indent`, `to_json` and `~` intact, and a sentinel is a
   valid YAML plain scalar, so a render the renderer parses itself still parses.
2. In a directory that substitutes, every `$` followed by `$`, by `{` or by a sentinel becomes
   `$$`. Flux reads `$$` as one literal `$`, so every run of dollars survives substitution as
   written. A literal `${HOME}` renders as `$${HOME}`, and a bare `$$` as `$$$`.
3. In a directory that substitutes, every sentinel becomes `${K}`.

The five pinned live-read facts keep the values the stub file gives them (*Pinned live-read
facts*), so a round trip sees one value on both sides.

### Which directories substitute

The unit is the Kustomization directory under `cluster/`, so `<tag>` and `<tag>-deferred`
are decided apart. A directory's key set is the keys whose sentinel its render holds.

- **A directory substitutes when its key set is not empty.** Passes 2 and 3 run on it, and
  its Flux Kustomization carries `postBuild`.
- **A directory with an empty key set** is written exactly as rendered, every `$` included.
  Flux applies it with no `postBuild`, so it never reads that `$` as syntax, and an escape
  there would reach the cluster as a literal `$${`.

The key set is the one source for every reader that asks whether a directory substitutes,
and each reads it in process from `PlaceholderRender.key_sets`: the escape, the round trip,
and later the rules of R2's PR 2 and D2's `postBuild` writer. pihole substitutes, because its
Secret holds keys, and `pihole-deferred` does not. At `65b52ff2f`, 46 of the 61 directories
substitute.

### Tags placeholder mode skips

`PLACEHOLDER_DEFERRED` in `placeholders.py` names the tags placeholder mode cannot render
yet, each with its reason. It holds `home-assistant`, whose `templates/config/secrets.yaml.j2`
runs `from_json` on `google_assistant_service_account`, which raises on a sentinel. R2's PR 5
empties the set. Placeholder mode skips a member and names it in the summary line, as
`skipped, in PLACEHOLDER_DEFERRED: home-assistant`. A `--role` list that names only members
renders nothing, so `cluster` refuses it with exit 2. Stub mode renders every tag.

This set is a different thing from a `<tag>-deferred` directory, which holds the files a role
passes as `manifests_deferred_files`.

### Filters that only interpolate a secret

`PASSTHROUGH_FILTERS` in `scripts/renderer/renderer_lib/secret_checks.py` names the repo
filters that place an argument into their output unchanged. R2's PR 2 adds rule 5, which
accepts a secret key as an argument to a member and refuses it as an argument to any other
filter. The set is closed and holds two members, each passed `domain`:

- `homepage_href`, from `ansible/filter_plugins/homepage_tiles.py:homepage_href`;
- `authelia_service_rules`, from
  `ansible/filter_plugins/authelia_access.py:authelia_service_rules`.

Each member passes a membership test in
`scripts/renderer/tests/test_renderer_secret_checks.py`. The test calls the filter in every
call shape the templates write, once with the sentinel `r2-sentinel` for the key and once with
`a.b-c.example`. The first output, serialised as JSON, must hold the sentinel. Replacing the
sentinel there with `a.b-c.example` must give the second output exactly. A fixture filter that
upper-cases its argument fails the first check, and one that rewrites `.` as `-` fails the
second. A filter that fails the test leaves the set.

## The round trip

`shadow_diff.py roundtrip` proves that Flux's substitution of the placeholder render gives the
stub render back, object for object. `scripts/renderer/renderer_lib/roundtrip.py` holds its
logic. It renders the requested tags in placeholder mode and in stub mode under `--out`. Then
it builds every directory of each render with `flux build kustomization <dir> --dry-run`,
through a throwaway Flux Kustomization:

- **Placeholder side, a directory that substitutes.** The Kustomization carries every key's
  value inline in `postBuild.substitute`, and the build adds `--strict-substitute`.
- **Placeholder side, a directory that does not substitute.** No `postBuild`, so the
  comparison proves the renderer escaped nothing there.
- **Stub side.** No `postBuild`, so nothing is substituted.

`flux build` runs the controller's own substitution per resource and serialises both sides
with the same code. `roundtrip` therefore compares the two outputs byte for byte, and names
the first object that differs. It prints no file content.

It runs two passes:

1. **Each key's R1 stub**, on both sides.
2. **Every key `0123`**, on both sides. The stub side is a stub-mode render with every key set
   to `0123`, so Jinja writes the template's quotes around it there. Flux drops them on the
   placeholder side (*What the pinned flux CLI does*), so each site where substitution would
   change a value's YAML type shows as a difference.

Each pass prints a summary line, such as `roundtrip: 2 tags, 0 deferred directories, 2
substituting, 4 objects, 0 differences, with each key's R1 stub`. `roundtrip` exits 0 only
when both passes find no difference.

It needs the pinned flux CLI. daniel-box has none, so it runs in CI's `flux_roundtrip` job.
Where `flux` is not on `PATH`, it exits 2 and reports no pass. It also exits 2 when a render
fails or no rendered directory substitutes, because nothing would pass through Flux's
substitution. A setup failure therefore never exits 1, the code CI's red half requires. `--red-proof` changes one key's
substitute value by one byte in the first pass, so a working round trip must exit 1. The CI
job runs it that way too, and fails unless it does:

```bash
uv run python scripts/renderer/shadow_diff.py roundtrip --out /tmp/r2-roundtrip \
  --role configarr --role janitorr
```

The CI job round-trips `configarr` and `janitorr`, whose Secrets embed whole app configs with
several placeholders in a block scalar. R2's PR 2 widens it to every tag outside
`PLACEHOLDER_DEFERRED`.

### What the pinned flux CLI does

`shadow_diff.py fluxprobe` builds four small fixture directories with the flux CLI and fails
unless each expected output appears. CI runs it before the round trip, against the same
pinned binary, so a pin bump that changes one of these behaviours fails there first. With
`--red-proof` it also expects one output no fixture yields, and CI requires that run to exit
1. `scripts/renderer/renderer_lib/flux_probe.py` holds the fixtures.

Measured on CI run 38426501907 with flux v2.9.6:

- `$${HOME}` comes back as `${HOME}`, and `$$$`, which pass 2 writes for a bare `$$`, as `$$`.
- `$$$$$${x}`, which pass 2 writes for a literal `$$${x}`, comes back as `$$${x}`.
- `$$${K}`, which passes 2 and 3 write for a `$` directly before a secret reference, comes
  back as `$` plus the value.
- `${HOME}` in a directory built with no `postBuild` comes back unchanged.
- A variable absent from `postBuild.substitute` fails the build under `--strict-substitute`.
- **A `stringData` value written `"${probe_n}"` comes back as the integer `83` for
  `probe_n=0123`, and as the YAML `bool` value `true` for `probe_n=true`.** Flux substitutes
  into a resource it has already serialised, without the template's quotes. An all-digit or
  `true` value therefore changes type wherever it stands alone in a field. The probe asserts
  both results. A bump that keeps the quotes fails it, and the round trip's second pass can
  then go.
- A bare `n` as a key of `postBuild.substitute` defines no variable `n`. Go's YAML 1.1 reader
  takes the unquoted key `n` for `false`, so the probe names its variable `probe_n`.

## Pinned live-read facts

Five facts come from live state in a real run, so the renderer cannot compute them from the
tree. Target slice R3 removes those reads. Until then, each one takes a fixed stand-in in
both halves:

- `authelia_password_hash`, `authelia_claude_password_hash` and
  `authelia_agent_password_hash`, read by the `authelia` templates.
- `pi_peer_backup_known_hosts_line`, read by the `pi-peer-backup` templates.
- `k8s_built_images`, read by the `n8n` templates. It pins to `[]`, which n8n renders as
  `checksum/image: "unstaged"`.

A string stand-in carries the secret stubs' marker, such as `stub-authelia-password-hash`.
Each entry also names its reader tags. A test in `scripts/renderer/tests/` holds those tags
equal to a word grep of the templates, so a new reader fails the suite.

## Stub output is never published

A stub render is a comparison input. Nothing may push it as the cluster artifact, because
`domain` is a secret key too: a stubbed route matches `*.stub-domain` and answers no request.
The plan's section *Stub output is never published* names the guards later slices add.
A publish job states its mode by passing no `--secret-stubs` (C1 *Only placeholder mode
publishes*).

## Write the stub file

The file is a function of the git tree alone, so an agent, CI and the operator write the
same bytes for one commit. It reads only the key names of `ansible/vars/secrets.yml`, which
SOPS leaves in plaintext, and decrypts nothing.

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
```

The file holds every stub, the sentinel `homelab_secret_stubs: true` and the five pinned
facts. Pass it only to a k8s dry run or to the renderer, never to a run that applies
anything.

A stub file goes stale when a secret key is added or removed, so write a fresh one for each
commit you render.

## A stub file stops outside a k8s dry run

`ansible/pre_tasks/load_secrets.yml` refuses a misplaced or incomplete stub file. Every
playbook that reads a secret imports that file, and its two asserts run ahead of the SOPS
load and before any role. A run counts as a stub run when the sentinel
`homelab_secret_stubs` is true, or when any secret key already holds its stub. The second
half catches a stub file whose sentinel was stripped. A run without a stub file defines no
key before the load, so neither assert fires.

- "Refuse a secret stub file outside a k8s dry run" requires `k8s_dry_run`, the play
  variable `load_secrets_stubs_allowed` and the sentinel. A stub file stripped of its sentinel
  is refused even in an allowed dry run, because sonarr's API reads skip on the sentinel
  alone. Only `ansible/deploy.yml` sets that variable. Its
  k8s play sets it to true. Its Docker play sets it to true only where the host's
  `containers_list` holds no Docker entry, so a stubbed run aimed at daniel-pi stops before
  any Pi role. A stub file passed to any other playbook stops at its preamble, dry run or
  not.
- "Refuse a stub file that does not stub every key" reads the key names from the ciphertext
  `secrets.yml` and names each key that does not hold exactly its stub. A stub file written
  before a secret was added fails here and names the new key. To fix it, write a fresh stub
  file from the same tree.

A census in `ansible/tests/k8s/test_secret_key_task_census.py` covers the tasks that would
act on a stub under an allowed run. It reads every k8s role task whose body names a secret
key, or a variable derived from one. A derived variable is a role default or inventory var
built from a secret key, or a `set_fact` key or `register:` name whose task in any k8s role
names one. Each such task must be skipped under a stubbed dry run, use a
side-effect-free module, or carry a per-task exemption with a reason. An `assert` or a `fail`
can refuse a stub value, so the census credits only the `homelab_secret_stubs` guard on it.
sonarr's API reads, and its two asserts on the reads' registers, pass on that guard.

The same module names uptime-kuma's push-token format check in `STUB_REFUSING`, which must
carry the `homelab_secret_stubs` guard itself. The list predates the census's assert rule and
backs it up.

From this change on, the preamble names the stub filters, so the deployer cannot narrow an
edit to `ansible/filter_plugins/secret_stubs.py` to a service. Each such edit defers every
declared service, and `land.sh` reports `needs-manual-apply`.

## Keep the Ansible render of a stubbed dry run

The manifests role variable `manifests_shadow_dir` keeps what a k8s dry run renders. Passed
with `-e` beside the stub file, it renders each service into `<dir>/<service>` and keeps it,
owned by `sys_user`. Unset, which is the default, the dry run renders into a temporary
directory and discards it, as before. This tree is the Ansible half of the shadow pair, which
R1's comparison reads against `homelab_render.py cluster`'s output.

The role's first task refuses the variable unless the run is a k8s dry run and every secret
key still holds its stub. It runs after the SOPS load, so a key the stub file did not cover
holds its real value there and is named. It recomputes the check with
`homelab_unstubbed_keys`, so no sentinel or flag passed as an extra var can stand in for it.

The run is **operator-only**, because the play decrypts SOPS and escalates with `become`. The
plan's *Verification* section has the commands.

## Compare the two halves

`shadow_diff.py compare` reads two render trees and reports how they differ. Either side is
an Ansible shadow tree, with one directory per service as `manifests_shadow_dir` keeps it, or
a C1 tree, with `cluster/` at the top as `homelab_render.py cluster` writes it. It keys both
by the C1 path. An Ansible service's deferred subdirectory, such as
`pihole/pihole-instance-2/`, maps to `cluster/pihole-deferred/`. `kustomization.yaml` is
ignored, because Ansible writes none.

- **The expected tags.** When either side is an Ansible tree, the set is the render targets
  of daniel-box less the entries that carry `owner: flux` (C4 in
  [the contracts](target-architecture/contracts.md)). A C1 side's flux-owned directories are
  ignored, and an Ansible directory for a tag outside the set is reported as extra. Two C1
  trees compare over every render target.
- **The verdict.** It exits 0 and prints `<t> tags, <n> files, 0 differences` only when the
  trees match. `<t>` is the expected tag count and `<n>` the number of files compared. It
  exits 1 on any missing, extra or differing file, and on an expected tag that neither side
  rendered.
- **The refusals.** It exits 2 when the expected set is empty, when neither tree holds a file
  for an expected tag, or when either side lacks one of three named members:
  `traefik/deployment.yaml`, pihole's deferred directory `pihole-instance-2`, and a
  `claim-*.yaml` file. A named member whose tag is flux-owned is dropped from that check, and
  `compare` prints which. A file where neither layout puts one is refused as well.
- **The report.** It groups the differences by class: present on one side only, an expected
  tag on neither side, trailing newlines only, whitespace only, content, and a file lacking a
  stub token. The report is meant for posting, so it prints no stub or secret value. A file
  that fails `stubcheck`'s rule is named and its content withheld. A file holding a `kind:
  Secret` document is named without a hunk, because its `data` is base64 that redaction
  cannot read. A file that differs on a line where the C1 render holds a stub token is named
  without a hunk too, because the Ansible line beside it sits at a stub site. Every other hunk
  carries each stub token as `<stub>`.

`shadow_diff.py stubcheck ANSIBLE C1` checks the Ansible tree's stub values against the C1
render. For each file both trees hold, every stub token in the C1 file must appear in the
Ansible file at least as often, with the `data` of each Secret decoded on both sides. The
count matters: a file that keeps a token at one site and holds another value at a second
site fails. A stub token is `stub-` followed by lower-case letters, digits and hyphens, and
tokens compare whole. It
names each failing file with the tokens it lacks, never the file's content, and exits 1. A
file only one side holds, or one whose C1 render holds no token, passes unchecked: the
manifests role's shadow-dir assert is its guard.

`shadow_diff.py digest DIR` prints one line per directory under `DIR/cluster/`, as
`<directory> sha256:<hex>`, then a `total` line over every file under `DIR`. Each hash covers
every file's path relative to its directory and the file's bytes, with paths sorted by byte, so
no line depends on the absolute directory or the locale. A render of some tags gives the same
lines for those tags as a render of all of them. `digest_line` and `directory_digests` in
`scripts/renderer/renderer_lib/shadow.py` build the same lines for R1's determinism tests.

All three exit 64 on a usage error. `scripts/lib/exit_codes.py` holds the codes as its
`SHADOW_*` family.

### Run the shadow pair

The shadow pair is **operator-only**. Its Ansible half decrypts SOPS and escalates with
`become`, so it runs on daniel-box as `ubuntu`, from a clean worktree of the operator's
checkout at a merged master SHA X. Both halves then read the same commit. `deploy.sh
--dry-run` renders the working tree, which must be clean. Ansible refuses non-blocking stdio,
so the dry run redirects all three streams.

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

The operator posts `compare`'s output on the issue or PR that tracks the run.

To hand the agent a reference tree, the operator first runs `stubcheck`. The copy goes ahead
only when the dry run exited 0 and `stubcheck` exits 0. A file that fails `stubcheck` holds
no stub where the C1 render holds one. The operator inspects that file locally and never
copies or posts it.

```bash
# Operator-only: check the Ansible tree's stub tokens, then copy it into the agent's home.
uv run python scripts/renderer/shadow_diff.py stubcheck \
  /var/tmp/homelab-shadow/X/ansible /var/tmp/homelab-shadow/X/render
sudo install -d -o claude -g claude -m 0700 /var/lib/claude/shadow
sudo cp -r /var/tmp/homelab-shadow/X/ansible /var/lib/claude/shadow/X
sudo chown -R claude:claude /var/lib/claude/shadow/X
```

The agent then renders and compares a branch based on X that changes only
`scripts/renderer/`. Ansible's render of X is the same for every such branch, so one operator
run serves them all:

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \
  --secret-stubs /tmp/r1-stubs.yml
uv run python scripts/renderer/shadow_diff.py compare /var/lib/claude/shadow/X /tmp/r1-render
```

## The render is deterministic and reads only the repo

Four test modules prove R1's exit criterion 2: `test_renderer_determinism_0.py` to
`test_renderer_determinism_3.py` in `scripts/renderer/tests/`. Module `k` takes the tags at
positions `k`, `k + 4`, ... of the sorted `render_targets` list, so the parts stay balanced as
tags join. It writes the stub file and renders its tags twice, each time in a subprocess with
a different `PYTHONHASHSEED`, and requires the two trees to be identical. Module 0 also fails
when a module for one of the four positions is missing.

The split is four because of CI time. A module runs about 3.4 times slower on the CI runner
than serially on daniel-box. The two-module split of 2026-10-13 took 28.0s per module in CI
against 8.2s locally. Three parts were projected at 20.3s, over R1's 20s budget, and four at
about 16.6s. `PARTS` in `scripts/renderer/tests/_render_audit.py` is the modulus, and changing
it means adding or removing modules to match.

Each subprocess runs `scripts/renderer/tests/_render_audit_child.py`, which installs an audit
hook before it imports the renderer. The hook ends the process on any file open outside the
repo, the output directory, the stub file, ansible-core's local temp directory and the
interpreter's prefixes, and on any `socket.connect`. `/dev/null` is the one device it allows.
The test points `ANSIBLE_LOCAL_TEMP` into its own directory, because ansible-core creates that
directory on import and its default is under the user's home. The hook lives in a subprocess
because Python cannot remove an audit hook, and one installed in an xdist worker would outlive
the test.

What the hook does not see: ansible-core's import runs `ldconfig -p` and `ip link` as child
processes, to find the C library and to read a MAC address for `uuid.getnode`. A child
process reads the host without an `open` in the process the hook watches. No template reads
either result, and a
rendered byte that depended on one would differ between the CI runner's digest lines and
daniel-box's.

In CI, each module writes one `digest_line` per directory it rendered to two places. The job
log carries each as a line `render digest at <sha> (position <k> of 4): <line>`, which
`gh run view --log` prints without a sign-in. The job summary carries them under a `render
digest at <sha>` heading, but only a signed-in viewer can read it, and `gh` has no API for it.

The log line takes a path of its own, because CI runs each shard under xdist. A test runs in a
worker, and a worker's stdout never reaches the job log. So the test records its lines on its
report with `record_property`, xdist carries the report back to the controller, and the
`pytest_terminal_summary` hook in `scripts/renderer/tests/conftest.py` prints them there.
`test_renderer_digest_log.py` runs a pytest session of its own under `-n 2` to prove that path.

A directory's line depends on its own files alone, so the four modules' lines together hold
every directory, whichever shards the modules ran on. None writes the `total` line. On a pull
request the SHA is a synthetic merge commit, so compare the lines from the push run on master.
To check them on daniel-box, render that commit in full and diff its `digest` against the
log. Replace `<run>` with the push run's id, which `gh run list --branch master --event push
--commit <sha>` prints.

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1 \
  --secret-stubs /tmp/r1-stubs.yml
uv run python scripts/renderer/shadow_diff.py digest /tmp/r1 | grep -v '^total ' \
  | LC_ALL=C sort > /tmp/r1-box.txt
gh run view <run> --log | grep -o 'render digest at .*' | sed 's/^[^:]*: //' \
  | LC_ALL=C sort > /tmp/r1-ci.txt
diff /tmp/r1-ci.txt /tmp/r1-box.txt
```

The `diff` must print nothing, and both files must hold one line per directory.

## What the render does not cover

Zero diff covers what the manifests role renders: each role's applied directory, its deferred
directory and its claim files. Four channels stay outside it:

- objects that role tasks apply, create, patch, run or write through `exec` themselves, such
  as `--from-file` ConfigMaps, probe and gate Jobs and `kubectl run` pods;
- files that nine roles write to hosts, which belong to the host plane;
- the `homelab` Namespace and its LimitRange, which `ansible/deploy.yml` builds inline;
- the values of the five pinned live-read facts, which target slice R3 removes.

The first channel is a census. The `render-does-not-cover-role-writes` row of
`ansible/tests/repo/test_census_rows_roles.py` reads every task file under
`ansible/roles/k8s/*/tasks/` except the manifests role's. A task writes when
`ansible/tests/_k8s_guards.py:_chunk_mutates` matches it, the rule the dry-run guard census in
`ansible/tests/deploy/test_k8s_dry_run.py` also reads. The row's `allow` map, in
`ansible/tests/repo/_out_of_render_row.py`, names each writing file with the slice that owns
its move. A writing file outside the map fails the row until a reviewer adds it with its owner.
A file in the map that no longer writes fails the row too, so the map stays the list of what
the render still leaves out.
