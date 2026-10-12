# The homelab-render shadow renderer

`homelab-render` renders the k8s manifests from the git tree alone, as a shadow of the
Ansible manifests role. Target slice R1 builds it and proves it byte-identical to Ansible for
the same stub inputs. Ansible stays the only writer to the cluster: the renderer writes only
to the directory it is given.

The plan is [R1, shadow render with zero diff](target-architecture/render/r1-shadow-render.md).
C1 in [the contracts](target-architecture/contracts.md) defines the interface. R1 lands in
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

Two more filters serve the checks that a later R1 PR adds to the playbooks.
`homelab_stub_keys` lists the key names that take a stub, from the parsed `secrets.yml`. It
reads keys only, so the ciphertext file is enough. `homelab_unstubbed_keys` returns the keys
whose value is not exactly its stub. It returns names only, never a value.

## Render the cluster tree

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

- `--secret-stubs` is missing. Without it the render is placeholder mode, which target slice
  R2 adds. A bare `cluster` call in a publish job therefore fails rather than publishing.
- The stub file does not stub every key of `ansible/vars/secrets.yml`. The message names the
  keys, never a value. Write a fresh stub file for the commit.
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
would not compute.

The render is not yet proven byte-identical to Ansible's. R1's PR 7 compares the two and
closes each class of difference.

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
