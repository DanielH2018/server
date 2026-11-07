# The homelab-render renderer

`homelab-render` renders the k8s manifests from the git tree alone. Placeholder mode renders
each secret reference as Flux's `${<key>}`, and stub mode renders each as a stub. Host and
package modes render the host packages (*Render a host package*). The renderer writes only to
the directory it is given. `promote` and Flux carry a published bundle to the cluster.

The plans are [R1, shadow render with zero diff](target-architecture/render/r1-shadow-render.md)
and [R2, secret placeholders](target-architecture/render/r2-secret-placeholders.md). C1 in
[the contracts](target-architecture/contracts.md) defines the interface. Each slice lands in
steps, and this page describes each piece once its PR has merged.

## What is in place

- `ansible/filter_plugins/secret_stubs.py` holds the stub form. It is the one definition
  that R1's renderer and its playbook checks are built to share, so neither restates it.
- `scripts/renderer/shadow_diff.py stubs` writes the stub file a stub-mode render takes.
- The renderer pins no live fact, because no value a template reads comes from live state
  (*The renderer pins nothing*).
- `scripts/renderer/homelab_render.py cluster` renders into the C1 layout. It applies the file
  rules (*The file rules*), and renders every k8s render target of daniel-box.
- `scripts/renderer/renderer_lib/context.py` builds the variables a role's templates see,
  and `renderer_lib/cluster.py` renders each tag and writes its directories.
- `scripts/renderer/renderer_lib/task_inputs.py:include_vars` reads each role's render
  metadata from its `defaults/main.yml` (*Where render metadata lives*).
- `scripts/renderer/renderer_lib/derived.py:content_tag` computes each built image's tag
  (*Derived values*), and `scripts/renderer/renderer_lib/targets.py:render_targets` lists the
  tags a host renders.
- `scripts/renderer/renderer_lib/filters.py` holds the Ansible-dialect filters, so the renderer
  imports no ansible-core. `scripts/renderer/tests/test_renderer_filters.py` compares each with
  ansible-core's while it is installed.
- `scripts/renderer/shadow_diff.py digest` hashes a render (*Hash a render tree*).
  `scripts/renderer/renderer_lib/shadow.py` holds its library.
- Four determinism modules in `scripts/renderer/tests/` render every tag twice under an audit
  hook (*The render is deterministic and reads only the repo*).
- `scripts/renderer/renderer_lib/placeholders.py` holds placeholder mode: the mode call, the
  three passes and the per-directory key sets (*Placeholder mode*).
- `scripts/renderer/shadow_diff.py roundtrip` builds the placeholder render and the stub render
  with the pinned flux CLI and compares them, and `fluxprobe` checks that CLI on fixtures
  first. CI's `flux_roundtrip` job runs both (*The round trip*).
- `scripts/renderer/renderer_lib/secret_checks.py` holds `PASSTHROUGH_FILTERS`, the filters
  that only interpolate a secret key, which is empty (*Filters that only interpolate a
  secret*).
- The standalone-value guard: `placeholders.py`'s standalone key sets,
  `scripts/renderer/standalone_map.py` and the map it commits, the resolver in
  `scripts/lib/plain_scalar.py`, and the check in `scripts/secrets_mgmt/cluster_vars.py`
  (*The standalone-value guard*).
- `scripts/renderer/renderer_lib/host.py` holds host and package modes, which
  `homelab_render.py host` and `package` run (*Render a host package*).

## The stub form

A stub render never reads a secret. Every key of `ansible/vars/secrets.yml` renders as its
stub instead, which the filter `homelab_secret_stub` builds:

- A key `K` becomes `stub-` plus `K` with underscores turned to hyphens, such as
  `stub-email`. That shape is a valid DNS label, so a stubbed host name still passes
  `kubectl apply --dry-run=server`.
- `become_password` never takes a stub. It is the one name in `NEVER_STUB`, because
  `ansible/pre_tasks/load_secrets.yml` hands it to privilege escalation and no template
  reads it.
- `domain` takes no stub and no placeholder, because it is not in `ansible/vars/secrets.yml`.
  The operator ruled on 2026-10-14 that it is a variable, not a secret, and deleted it from
  the store on 2026-10-15. Every render reads its value from
  `ansible/inventory/group_vars/all.yml`.

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

- With `--secret-stubs FILE`, the render is in stub mode.

To render the tags in stub mode, write a stub file for the commit and pass it to `cluster`:

```bash
uv run python scripts/renderer/shadow_diff.py stubs --out /tmp/r1-stubs.yml
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r1-render \
    --secret-stubs /tmp/r1-stubs.yml
```

`--role TAG` renders only the tags it names, and it repeats. Without it, `cluster` renders
every k8s render target of daniel-box, the list
`scripts/renderer/renderer_lib/targets.py:render_targets` returns. A service renders when its
role has a top-level manifest template, a shared default its entry's `port` or `hostname` earns,
or a vendored manifest.

Each tag becomes `DIR/cluster/<tag>/`. The directory holds the rendered manifests and a
`kustomization.yaml` whose `resources` list them in lexical order, the order
`kubectl apply -f <dir>/` reads a directory in. A tag that defers files also gets
`DIR/cluster/<tag>-deferred/`, with its own `kustomization.yaml`.

A role can vendor upstream files: manifests byte for byte under `files/upstream/`, and
Kustomize patches under `files/patches/`. The renderer copies both into `DIR/cluster/<tag>/`
under the same subdirectory names, after every pass that edits rendered text. The
`kustomization.yaml` lists the upstream files under `resources` and the patches under
`patches`. A placeholder render refuses a tag that vendors files and whose own render holds a
placeholder, because Flux would then substitute into the upstream bytes. No `$` in those bytes
is escaped. `ansible/tests/_k8s_render.py:rendered_docs` reads `render_tag`'s output, which
holds no vendored file, so the fleet guards never read them. H1's *Vendored upstream manifests*
(`docs/target-architecture/host/h1-cluster-config-to-flux.md`) says why that is accepted.

A placeholder render with `--role` also writes the publish bundle, `DIR/flux/` and
`DIR/digests.json`, for the tags it names that carry `owner: flux`, plus each shared kind's
`DIR/flux/<kind>.yaml` and `DIR/cluster/<kind>/`. `docs/flux-deploy-plane.md`'s *The bundle*
describes them. A stub render, a render without `--role` and a render that names no
flux-owned tag write none of them. A published tag whose role lists `render_sops_manifests`
also gets a byte copy of each of its SOPS manifests from `ansible/vars/cluster-manifests/`
in `DIR/cluster/<tag>/`, and `decryption` on its Kustomization.

Every placeholder render also writes the image contexts (D5.4a1, C1 *Cluster output*).
`DIR/images/<name>/` holds one file per key of the build context that `k8s/image-builder`
renders for that image, byte for byte as its `data` parses. `DIR/images/tags.json` maps each
name to its content tag, `sha-<12 hex>`, which image-builder's own expression computes over
that `data`. The renderer renders each role among the render's tags that includes
`k8s/image-builder`, because a consumer reads only the images its own role builds. A render
without `--role` therefore writes all ten. A `--role` render writes the named builders'
contexts alone, and no `DIR/images/` when it names none (#5374). `--all-images` renders all
ten whatever `--role` names, and the publish job passes it, because its `images` job builds
every context. Every placeholder render refuses an `--out` whose `images/` already holds a
file. A consumer's ref reads its tag
from the same computation, never from `render_guard.py:BUILT_IMAGE_TAG_STUBS`. No
Kustomization's path reaches `DIR/images/`, and the bundle's footprints leave it out. A stub
render writes no image context, and `--shared-only` writes one only beside `--all-images`. `scripts/renderer/renderer_lib/images.py`
holds the writer and the checks. No job builds the contexts yet. D5.4a2 adds the `images`
job that builds and pushes them, and D5.4b points the flux-owned consumers at GHCR.

Every render, in either mode, stamps `checksum/restart` on the pod template of each restart
target of a flux-owned tag that names a ConfigMap or Secret. Every `manifests_self_rollouts`
entry is a restart target. Both modes hash the
placeholder-form documents and the digests in `ansible/vars/cluster-vars.source`, so they
stamp the same value. `docs/flux-deploy-plane.md`'s *Restart triggers* has the rule.

Every render, in either mode, also labels each PersistentVolumeClaim and Namespace of a
flux-owned tag `kustomize.toolkit.fluxcd.io/prune: disabled`, so Flux leaves the
object in place when it leaves the render (C3 *Prune exemptions*). The label is
inserted as text and moves no other byte. `scripts/renderer/renderer_lib/prune_label.py` holds
the rule.

`cluster --out DIR --shared-only` writes the shared kinds, a root listing them and
`digests.json`, and renders no tag. The publish job calls it when no tag is flux-owned, with
`--all-images`, so the `images` job still finds every context and `tags.json` (#5593). It
takes neither `--role` nor `--secret-stubs`, and either beside it is a usage error. The
`cluster-vars` kind copies the cluster file, `ansible/vars/cluster-vars.sops.env`, byte for
byte.

`cluster` exits 2 and writes nothing in seven cases:

- In stub mode, the stub file does not stub every key of `ansible/vars/secrets.yml`. The
  message names the keys, never a value. Write a fresh stub file for the commit.
- A `--role` names a tag that is not a k8s render target of daniel-box.
- A directory it would write under `DIR/cluster/` already holds files from an earlier
  render, or `DIR/flux/`, `DIR/digests.json` or `DIR/images/` does.
- A render that writes the bundle cannot read the cluster file. The bundle always carries
  the `cluster-vars` kind, so the render refuses rather than publish without it.
- A render that writes the bundle cannot read a SOPS manifest that a published tag's role
  lists.
- A tag to render is named `homelab`, the root Kustomization's name, after a member of
  `SHARED_KINDS`, or ends in `-deferred` (C3 *Reserved names*). An all-tag render refuses it
  too.
- The `dependsOn` edges a bundle render generates form a cycle
  (`docs/flux-deploy-plane.md`, *Ordering: `dependsOn`*).

It exits 1 when a template fails to render, and it writes nothing then either. It also exits
1 when a SOPS manifest lacks `kustomize.toolkit.fluxcd.io/substitute: disabled`, and when a
flux-owned tag's restart target names no rendered workload or reads a secret key that
`cluster-vars.source` does not record. A placeholder render exits 1 in three more cases: an
image context holds a secret's sentinel, a rendered ref names a built image with a tag other
than `DIR/images/tags.json`'s, or a Kustomization path reaches `DIR/images/`. It exits 64
on a usage error. `scripts/lib/exit_codes.py` holds the four codes as its `RENDER_*` family.

### The render context

A role's templates render with these variables, resolved for daniel-box:

- role defaults, `group_vars/all.yml` and daniel-box's host_vars, in Ansible's precedence,
  with the stub file's variables over all of them;
- `containers_list`: the k8s entries only, in the dependency order of `toposort_containers`.
  `scripts/renderer/renderer_lib/context.py` builds it with two filters from
  `ansible/filter_plugins/toposort.py`, and templates read that order;
- `container_item`, the tag's own entry from that list;
- `hostvars` for every host in `ansible/inventory/hosts.ini`. daniel-box's entry is the
  rendered context, so `hostvars[k8s_primary_node].containers_list` is that same list.
  Another host's entry holds its `hosts.ini` inline variables under its own host_vars,
  resolved against that host's own variables. daniel-pi's `ansible_host`, the Jinja
  `{{ server_ip }}`, therefore resolves to daniel-pi's address, not daniel-box's.

The render is strict. A name that no layer supplies stops the render with an error naming it.
The render guards under `scripts/validate/` print `STUB` instead, and lay stub values such as
`k8s_built_image_tags` under every layer. The renderer does neither: it derives
`k8s_built_image_tags` (*Derived values*). An undefined attribute
still takes its `| default(...)`.

The same rule holds one level down. A variable whose value names an undefined variable, such
as `x: "{{ no_such_name }}"`, is left undefined rather than resolved to `STUB` or `None`, so a
template that reads `x` stops the render. One that nothing reads costs nothing. That matters because three `group_vars/all.yml` values name
variables that only setup roles or Ansible itself define. Another host's `hostvars` entry is
stricter: a value there that does not resolve stops the render at once.

### The file rules

The renderer writes a tag's files by these rules:

- **The file lists.** `scripts/lib/k8s_roles.py:resolved_manifest_files` derives them from
  the role's templates and its `containers_list` entry. A top-level template whose basename
  says `secret` is a secret file. The rest are files, with a shared `service.yaml` for an entry
  with a `port` and a shared `ingressroute.yaml` for one with a `hostname`, less any `-job.yaml`,
  the role's `manifests_deferred_files` and its `manifests_exclude_files`. A role whose defaults
  set `manifests_files` or `manifests_secret_files` gets that list instead, resolved against the
  render context. navidrome's list, for one, names `ingressroute.yaml` only while it has
  replicas.
- **The shared defaults.** A listed `service.yaml` or `ingressroute.yaml` that the role ships
  no template for renders from `service-default.yaml.j2` or `ingressroute-default.yaml.j2`
  in `ansible/templates/`, so a role whose Service or route fits the shared shape writes no
  template (#2872, #3043). Such a route leaves no string in the role, so
  `ansible/filter_plugins/toposort.py` derives the traefik edge from the entry's `hostname`.
  A listed name with no template stops the render.
- **The claims.** Each `k8s_claims` entry renders `claim-default.yaml.j2` to
  `claim-<name>.yaml`, with the entry as `manifests_claim`.
- **The labelling wrapper.** A role that arms `manifests_prune`, the default, renders its
  files, its secret files and its claims through `role-labelled.yaml.j2`. The wrapper stamps
  `homelab/role: <tag>` on every document. authelia and nut opt out in their defaults.
- **The deferred files.** `manifests_deferred_files` render from the role's own templates,
  unlabelled, into `DIR/cluster/<tag>-deferred/`. pihole, headlamp and netpol-baseline defer
  files.

### Where render metadata lives

A role sets the keys of `scripts/lib/k8s_roles.py:RENDER_KEYS` in its
`defaults/main.yml`, and `image_builder_builds` lists the images it builds. Each key in
defaults carries `# noqa var-naming[no-role-prefix]`. `include_vars`, `image_builder_sites` and
`scripts/lib/k8s_roles.py:resolved_manifest_files` read `defaults/main.yml` only, and each raises
on a role without that file.

The render reads no role's `tasks/`. The only task files under `ansible/roles/k8s/` are the
revert runbook's, and `scripts/renderer/tests/test_renderer_runbook_allowlist.py` holds that set.

The renderer resolves only the five keys it renders with: the
two file lists, the deferred pair and `manifests_prune`. The others stay strings until a
reader such as `restart.py` resolves them. Role defaults still enter the template context,
so a template must not name one of these keys for any other purpose.

The renderer holds two defaults as constants:
`scripts/lib/k8s_roles.py:MANIFESTS_PRUNE_DEFAULT` and `ROLLOUT_TIMEOUT_DEFAULT`.

**The test harness reads this render.** `ansible/tests/_k8s_render.py:rendered_docs` and
`rendered_texts` read `render_tag`'s files and deferred files in stub mode, so a fleet guard
reads the labelled bytes. They stop before the restart stamp and placeholder mode.
`render_role_template` renders one template unlabelled with `role_context`'s variables, for a
guard that sets a value the inventory does not hold. A top-level template out of the file
lists, such as pihole's macro file, is in neither walk. A guard that reads one names it in its
own fallback table. `scripts/validate/k8s_manifests.py` validates the same stub-mode files, and
the files each tag renders from its `templates/verify/` and `templates/precheck/` too.

## The Jinja dialect the renderer accepts

[ADR-0028](adr/0028-the-renderer-supports-a-closed-jinja-subset.md) specifies the Jinja subset
the cluster render accepts: every filter, test and lookup, and the variable semantics.
`scripts/renderer/renderer_lib/dialect.py` holds the three literal sets. `renderer_env` holds
only those filters and tests and refuses any other lookup, so a template off the list fails the
render with an error naming the construct. To add a construct, add it to the literal, the ADR's
table and a case in `scripts/renderer/tests/_dialect_cases.py`, in one PR.

`scripts/renderer/tests/test_renderer_dialect.py` compares each construct with its literal, and
holds every template, variable value and render-key value of the k8s roles to the list. Host and package modes
render through `strict_env`, which the subset does not bind.

A template census in `scripts/renderer/tests/` parses every template of the k8s roles and
`ansible/templates/`. It fails when a filter or test name does not resolve in the environment
the renderer renders with.

### Derived values

One value a template or a role default reads is stated by no file. `k8s_built_image_tags`, which
`scripts/renderer/renderer_lib/task_inputs.py:derived_facts` returns, maps each built image to
its content tag:

- `scripts/renderer/renderer_lib/derived.py:content_tag` computes the tag: `sha-` and the first
  12 hex digits of `sha256` over `context-configmap.yaml.j2`'s rendered `data`, each key mapped
  to its text, sorted case-insensitively and encoded by `json.dumps`.
  `scripts/deploy_tools/ci_images.py:content_tag` holds a second copy, because the job that holds
  `packages: write` imports no renderer code.
- Each `image_builder_builds` entry's variables are image-builder's defaults under the calling
  role's context, with the entry's vars over both and `playbook_dir` resolving to `ansible/`.
- An entry whose `when:` is false adds no tag, as valheim's does without mods. Every role reads
  the map as `{}` when it builds nothing.

A role default that reads the map, such as `nut_k8s_image`, resolves in a second pass of the
context with the map as an override, the precedence `set_fact` has over every file layer.

Tests hold each content tag equal to the value ansible-core's `Templar` computes from
image-builder's expression with the same variables, which `ansible/tests/_image_builder_tag.py`
holds as a literal.

`lookup('template')` keeps the render's trailing newlines. The content tag hashes those bytes,
so a lookup that stripped them would move every content tag.
`scripts/lib/k8s_yaml.py:make_lookup`, which the render guards under `scripts/validate/` read
through, follows the same rules, so a guard checks the bytes the renderer writes.

## Placeholder mode

Placeholder mode is the mode the publish job runs (C1 *Only placeholder mode publishes*).
`cluster` without `--secret-stubs` renders every secret reference as `${<key>}`, where `<key>`
is the key name in `ansible/vars/secrets.yml`. Flux substitutes the real values at apply time.

`scripts/renderer/renderer_lib/placeholders.py` renders in three passes, because the escape
would also escape a placeholder emitted before it:

1. The context maps each secret key `K` to the sentinel `HOMELAB_SECRET__K__END`. Letters and
   underscores survive `default`, `indent`, `to_json` and `~` intact, and a sentinel is a
   valid YAML plain scalar, so a render the renderer parses itself still parses.
2. In a directory that substitutes, every `$` followed by `$`, by `{` or by a sentinel becomes
   `$$`. Flux reads `$$` as one literal `$`, so every run of dollars survives substitution as
   written. A literal `${HOME}` renders as `$${HOME}`, and a bare `$$` as `$$$`.
3. In a directory that substitutes, every sentinel becomes `${K}`.

The renderer pins no live fact (*The renderer pins nothing*), so every value outside the
secret keys is the same on both sides of a round trip.

### Which directories substitute

The unit is the Kustomization directory under `cluster/`, so `<tag>` and `<tag>-deferred`
are decided apart. A directory's key set is the keys whose sentinel its render holds.

- **A directory substitutes when its key set is not empty.** Passes 2 and 3 run on it, and
  its Flux Kustomization carries `postBuild`.
- **A directory with an empty key set** is written exactly as rendered, every `$` included.
  Flux applies it with no `postBuild`, so it never reads that `$` as syntax, and an escape
  there would reach the cluster as a literal `$${`.

- **A document that opts out keeps its text, even in a directory that substitutes.**
  Flux's Kustomize controller skips substitution on a resource whose label or annotation
  `kustomize.toolkit.fluxcd.io/substitute` is `disabled`, so passes 2 and 3 skip it, and
  rules 1 to 4 read none of its references. The Flux CRDs in the `flux` tag carry it, and
  the Kustomization CRD's description holds a literal `${`. Escaped, that `${` came back
  from the pinned CLI as `$${`. A sentinel left in such a document fails rule 2.

The key set is the one source for every reader that asks whether a directory substitutes,
and each reads it in process from `PlaceholderRender.key_sets`: the escape, the round trip,
the five secret rules, and later D2's `postBuild` writer. pihole substitutes, because its
Secret holds keys, and `pihole-deferred` does not. At `65b52ff2f`, 46 of the 61 directories
substitute.

### Every tag renders in placeholder mode

Placeholder mode renders every tag it is asked for, as stub mode does. R2's PR 9 deleted the
set of tags it skipped, `PLACEHOLDER_DEFERRED`, so a tag that cannot render with sentinels
fails the render instead of being left out. The set's last member was `home-assistant`, whose
`from_json` parse of `google_assistant_service_account` raised on a sentinel until R2's PR 5
moved that key into a Secret of its own.

A `<tag>-deferred` directory is a different thing: it holds the files a role lists as
`manifests_deferred_files`.

### The five secret rules

`scripts/renderer/renderer_lib/secret_checks.py` runs five rules after every placeholder
render, before anything is written. The rules are enforced with no exemption list: any
violation makes `cluster` exit 1. Each line names the
rule, the tag, the file, the document as `Kind/name` or the refused form, and the key.

| Rule | Reads | Refuses |
|---|---|---|
| `known-key` | the output of a directory that substitutes | a `${NAME}` whose name is not a key of `ansible/vars/secrets.yml` |
| `no-bare-dollar-brace` | the output, read as Flux reads it, each `$$` paired first | a `${` that is not a placeholder of pass 3, and a sentinel fragment in any directory |
| `confined-to-secret` | the kind of each rendered document | a placeholder outside `kind: Secret`, except `email` (`PLACEHOLDER_ANYWHERE`) |
| `no-sops-manifest-key` | the output | a placeholder of a key in `SOPS_MANIFEST_KEYS`, the multi-line or all-digit standalone keys Flux cannot substitute as written |
| `no-secret-expression` | the syntax tree of every `.j2` under the k8s roles' `templates/` and `ansible/templates/`, but `*.sh.j2` | any form of a secret key that could change its value |

Rule 5 allows a key only bare, under `default`, `to_json` or `indent` (not for a
`SOPS_MANIFEST_KEYS` key), inside a `~` concatenation, as the subject of `is defined`, as an
argument to a member of `PASSTHROUGH_FILTERS`, or bound to a name or placed in a list, tuple
or dict. It refuses every other filter, every attribute, subscript or method call, every
comparison and every truthiness test, `{% if K | default('') %}` included. It reads a key
reached through `lookup('vars', 'K')` as `K`. A non-literal `lookup('vars', X)` counts as a
possible secret reference, held to bare, `default` and `to_json`, unless
`NON_SECRET_VARS_LOOKUPS` names its site. That set holds one site, Prometheus's
`lookup('vars', job.replicas_var) | int`, and the entry fails when any `containers_list`
`replicas_var` names a secret key, or when the site has gone.

An output inside a `{% filter %}` block, or a `{% set %}` block with a filter, passes through
that block's filters too, and rule 5 holds it to the same list. An `{% include %}` or a
`lookup('template', ...)` counts as a use of every key its target names, and through its
target's own embeds. Rule 5 resolves the target as the renderer does, then refuses the
embed on a refused form, or on `indent` when the target names a `SOPS_MANIFEST_KEYS` key.
Two embeds in image-builder name their target through a variable, so they cannot be
resolved. They pass only `indent`, which rule 5 accepts on an unresolved embed.

Rule 5 also follows a key through a name bound to it, so a transform of the name fails as a
transform of the key. It reads two kinds of binding.

- **Template-local.** A `{% set %}`, a set block, a `{% with %}`, a loop target (unpacked
  targets included) or a macro parameter that receives an expression naming a key is an
  alias of that key. A `{% set x = 'a.' ~ K %}` followed by `{{ x | trim }}` fails like
  `{{ K | trim }}`. A bare use of the alias passes. A binding that receives a list, tuple or
  dict is a container alias, and a binding of a single value is a value alias. A container
  alias also passes iteration, a part selected by a constant name and, when a `{% set %}`
  bound it to a literal container, a length test in an `{% if %}`. A value alias passes none
  of the three.
- **Role defaults and inventory.** The Jinja strings in `ansible/roles/k8s/*/defaults/main.yml`,
  `ansible/inventory/group_vars/` and `ansible/inventory/host_vars/` are parsed the same way.
  A value that names a key is an alias for it, the text around an interpolation counts as a
  `~` concatenation, and a transform in the value is a violation. Every template that uses
  the variable then treats it as the key, and an alias of an alias counts too. The tree has
  none since `domain` was published. Its one alias was `headlamp_k8s_oidc_issuer_url`,
  `https://auth.{{ domain }}`.

The residual: rule 5 has no types, so a part of a container alias is untyped. A loop over a
part that holds a string would iterate the string character by character, and the check
would accept it. No template iterates such a
part, so a reviewer reads any new `{% for %}` over a part of a container alias as a
transform.

Until R2's PR 9, the ratchet `KNOWN_VIOLATIONS` held every violation the first fleet render
found and the tree still had. An entry that no longer matched failed the check, so the set
shrank only through fixes, and PR 9 deleted it once it was empty. Admitting a violation now
means changing the rule itself. R2's PRs 6 and 7 removed rule 4 on pi-peer-backup's and authelia's Secrets and rule 5's
`indent`s on their keys, by moving each key into a Secret of its own. R2's PR 4 removed qbittorrent's two `trim`s and
mosquitto's `string`, and `cluster_vars.py write` refuses a stored hash that holds a colon.
R2's PR 5 removed Home Assistant's two entries, the `from_json` parse and
the Secret that embedded its output through `indent`. R2's PR 3 removed the 24 truthiness
tests, each now `{% if K is defined %}`. Domain PR 2b removed the `hash('sha1')` on
Prometheus's config block, which output `domain`
([#4952](https://github.com/DanielH2018/server/issues/4952)), by publishing `domain`.

Each rule must find its named members, so a rule that stops matching cannot pass: rule 3
`${email}` in traefik's `traefik-static` ConfigMap, rule 1 a placeholder in authelia's config
Secret, rule 2 the escaped `$${` in qbittorrent's `qbittorrent-netns-reset` ConfigMap, rule 4 every
`SOPS_MANIFEST_KEYS` key in `ansible/vars/secrets.yml`, and rule 5 at least 46 templates that
name a key and two accepted uses. A `--role` render asserts a member of rules 1 to 4 only
for the tags it rendered. Rule 5 reads the whole tree on every run.

`scripts/renderer/tests/test_renderer_secret_checks.py` holds each rule's `_is_clean` and
`_is_flagged` pair, and `test_renderer_secret_checks_tree.py` the fleet render and the named
members.

### Manifests placeholder mode leaves out

A multi-line key cannot pass through a placeholder, because Flux deletes every newline from a
substituted value. An all-digit key that stands alone in its field cannot either, because Flux
drops the template's quotes and types the value as a number. Each such key moves into a small
Secret of its own, which D3 ships as a SOPS-encrypted manifest. The role lists that Secret's
manifest in the defaults variable
`render_sops_manifests`, and placeholder mode emits no document for it. It drops the file
before pass 2 and rebuilds the directory's `kustomization.yaml` without it, so its key counts
in no key set and in no standalone set. Stub mode still renders it.

| Role | Entry | Secret | Key |
|---|---|---|---|
| `home-assistant` | `google-sa-secret.yaml` | `home-assistant-google-sa` | `google_assistant_service_account` |
| `pi-peer-backup` | `ssh-key-secret.yaml` | `pi-peer-backup-ssh-key` | `pi_peer_backup_ssh_key` |
| `qbittorrent` | `mullvad-account-secret.yaml` | `qbittorrent-mullvad-account` | `mullvad_account` |
| `authelia` | `oidc-jwk-secret.yaml` | `authelia-oidc-jwk` | `authelia_oidc_rsa_key_content` |

A wrong entry would hide a whole Secret from the publish and from the round trip, so the
`sops-manifest-entries` check in `renderer_lib/sops_entries.py` holds the lists on every
placeholder render. `cluster` exits 1 and writes nothing when an entry:

- names a file the role does not render;
- renders anything but exactly one document, of kind `Secret`;
- names a secret key outside `SOPS_MANIFEST_KEYS`, or no key in it.

The check reads the keys from the entry's sentinel render, so a key reached through an alias
counts. A `--role` render checks only the tags it rendered.

A third clause is an equality: the templates of every role's `render_sops_manifests` entries
are exactly the templates that name a `SOPS_MANIFEST_KEYS` key. A template that names one and
is in no role's list fails, and so does an entry whose template names none. Rule 5's walk
supplies the names, so an alias counts. The clause reads template sources and every role's
list, so it scans the whole tree on every run, `--role` renders included.
`secret_checks.check` runs it beside the five rules, so a failure prints `secret check:
sops-manifest-entries: <template> ...` and `cluster` exits 1. The four entries in the table
above are the four templates that name such a key.

### Filters that only interpolate a secret

`PASSTHROUGH_FILTERS` in `scripts/renderer/renderer_lib/secret_checks.py` names the repo
filters that place an argument into their output unchanged. Its reader is rule 5, which
accepts a secret key as an argument to a member and refuses it as an argument to any other
filter. The set is closed and empty. Its two members, `homepage_href` and
`authelia_service_rules`, were each passed `domain` alone, so they left the set when `domain`
was published. As members they would accept a later secret passed to either filter.

A member must pass a membership test in
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
2. **Every key outside the standalone-value guard `0123`**, on both sides, and every other key
   its R1 stub. A key is inside the guard when the guard's map names it under any tag. The
   stub side is a stub-mode render with those keys set to
   `0123`, so Jinja writes the template's quotes around it there. Flux drops them on the
   placeholder side (*What the pinned flux CLI does*). A difference therefore marks a
   standalone use of a key that the guard does not cover anywhere. The pass cannot see a
   missed site of a key that is mapped under another tag, such as a new standalone
   `${email}`, because that key keeps its stub fleet-wide. The coverage test catches that
   one (*The standalone-value guard*). `--guard-map FILE` reads another map than the
   committed one, so `{}` sets every key to `0123` again.

One thing is left out of the comparison, by count, and nothing else: the files a role's
`render_sops_manifests` names. Placeholder mode emits no document for them, and each stub
render drops the same files. Each pass counts the documents it dropped and fails unless the
count equals the number of entries in the tags it built. Until R2's PR 9 the round trip also
left out the documents a `KNOWN_VIOLATIONS` entry of rules 1 to 4 named. The last was
authelia's `Secret/authelia-config`, compared since R2's PR 7 moved its multi-line key out.

Every `checksum/...` pod annotation is compared. Until domain PR 2b the round trip also left
out observability's five `checksum/config` annotations, because Prometheus's hashed config
held `${domain}`. No hashed config holds a placeholder since `domain` was published, so a
placeholder that reaches one is a difference.

Each pass prints a summary line, such as `roundtrip: 58 tags, 4 deferred directories, 47
substituting, 450 objects, 0 differences, 4 documents in render_sops_manifests, with each
key's R1 stub`. `roundtrip` exits 0 only when both passes find no difference and the
exclusion count holds.

It needs the pinned flux CLI. daniel-box has none, so it runs in CI's `flux_roundtrip` job.
Where `flux` is not on `PATH`, it exits 2 and reports no pass. It also exits 2 when a render
fails or no rendered directory substitutes, because nothing would pass through Flux's
substitution. A setup failure therefore never exits 1, the code CI's red half requires. `--red-proof` changes one key's
substitute value by one byte in the first pass, so a working round trip must exit 1. The CI
job runs it that way too, and fails unless it does:

```bash
uv run python scripts/renderer/shadow_diff.py roundtrip --out /tmp/r2-roundtrip \
  --role configarr --role janitorr --role home-assistant
```

The CI job round-trips every tag in both passes, with no `--role`. That covers `home-assistant`, `pi-peer-backup`, `qbittorrent` and `authelia`, which
carry the four `render_sops_manifests` entries.
Its red half runs on `configarr`, `janitorr` and `home-assistant` only, because one changed byte
is all it has to show. The job also round-trips `traefik` with `email` dropped from the guard's
map under every tag, and fails unless that run exits 1 and names `Deployment/homelab/traefik`.
That proves the second pass still sees a standalone use the map does not cover:

```bash
jq 'map_values(map(select(. != "email")))' \
  scripts/renderer/job_files/standalone_secret_keys.json > /tmp/guard-map-without-email.json
uv run python scripts/renderer/shadow_diff.py roundtrip --out /tmp/r2-roundtrip-map \
  --role traefik --guard-map /tmp/guard-map-without-email.json
```

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
- **No template shape keeps a standalone value a string.** CI run
  [38432848015](https://github.com/DanielH2018/server/actions/runs/38432848015) built
  `0123`, `true`, `yes`, `1e3` and `null` through a double-quoted, a single-quoted and a
  `|-` block field, and through a bare `${v}` whose substitute value carries its own double
  quotes. Every one came back typed, as the integer 83, `bool`, `bool`, the integer 1000 and
  null, except
  `yes` with its own quotes, which stayed a string. Only text around the placeholder, as in
  `"x-${v}"`, kept all five strings, and that text changes the value. The probe behind it
  was not merged.
- A bare `n` as a key of `postBuild.substitute` defines no variable `n`. Go's YAML 1.1 reader
  takes the unquoted key `n` for `false`, so the probe names its variable `probe_n`.
- **The probe holds the guard's resolver to the CLI.** It builds each value of
  `RESOLVER_CORPUS` as a standalone `stringData` field. A value counts as kept only when it
  comes back as the identical string. Measured with v2.9.6 on 2026-10-13, `2001-12-14` came
  back as the string `2001-12-14T00:00:00Z`, and `.inf` failed the build. The probe fails when
  the resolver passes a value the CLI does not keep. It also fails when the resolver refuses
  a kept value outside `FLUX_KEEPS_BUT_REFUSED`, and when a member of that set stops being
  kept. The set holds `1:20`: the CLI keeps it, and PyYAML reads it as the integer 80.
  `--red-proof` reads the set as empty, so `1:20` fails too.

## The standalone-value guard

A *standalone use* is a placeholder that is the whole scalar of a field, such as
`email: ${email}` or `account: "${mullvad_account}"`. Flux substitutes without the template's
quotes, so it types a standalone value by its text (*What the pinned flux CLI does*). A value
such as `0123`, `yes`, `1e3`, `null` or `2001-12-14` stops being the same string. In a
Secret's `stringData` the apply then fails, and elsewhere the object changes meaning. No
template shape fixes it, so the guard checks the values where the cluster file is written.

- **The renderer names each key's standalone uses.** After pass 3, `placeholders.py` parses
  every document of a directory that substitutes. A key joins the directory's standalone key
  set when its `${K}` is the entire parsed value of a scalar, in any quote or block style.
  `x-${K}`, or a placeholder inside a block that holds more text, is not standalone.
- **The map is the round trip's list of guarded keys.** `standalone_map.py` writes
  `scripts/renderer/job_files/standalone_secret_keys.json`, which maps each deploy tag to the
  sorted keys standing alone in its directories, every one of them. `shadow_diff.py roundtrip`
  reads it in its second pass, where a key the map names keeps its R1 stub (*The round trip*).
  To regenerate it, run `uv run python scripts/renderer/standalone_map.py` and commit the file.
- **`cluster_vars.py write` checks the values.** It reads the standalone key sets from a
  placeholder render, not from the map, and refuses a referenced key whose value the resolver
  refuses. It prints key names and never a value.
- **The resolver** is `scripts/lib/plain_scalar.py:refused`. It refuses a value when either of
  two readers takes its bare text for anything but the same string. The first is PyYAML's
  `safe_load`, a YAML 1.1 reader, which types timestamps and sexagesimal numbers, and which
  changes or rejects any text that is no valid plain scalar. The second is a port of the
  plain-scalar resolver of go-yaml v2.4.4, the version the pinned CLI carries. It types
  `1e3`, `08`, `0o17` and `y`, which PyYAML reads as strings. The flux probe holds both to
  the pinned CLI.
- **The guard is enforced with no exemption list.** Every key with a standalone use is in
  the map. R2's PR 9 deleted `GUARD_DEFERRED`, the set of keys the guard skipped. Its last
  member was `mullvad_account`, whose real value is all digits, until R2's PR 7b moved that
  key into the dedicated SOPS-encrypted Secret `qbittorrent-mullvad-account`, which
  placeholder mode leaves out.
- **The coverage test**, `scripts/renderer/tests/test_renderer_standalone_map.py`, renders the
  fleet in placeholder mode and holds the committed map equal to it. It also fails on a
  mapped key absent from `ansible/vars/secrets.yml`. Its named members are `email` and
  `wireguard_interface_private_key`, mapped under `traefik` and `qbittorrent`.

When `cluster_vars.py write` refuses a key, it writes nothing, so the cluster file and the live
objects keep their old values. R2's Open question 13 records the operator's ruling of
2026-10-13: rotate the value to one that stays a string (**operator-only**), or move the key
into a dedicated SOPS-encrypted Secret, as PR 7b did for `mullvad_account`. Then write the
cluster file again.

## The renderer pins nothing

No value a k8s template reads comes from live state, so the renderer gives no fact a fixed
stand-in. Target slice R3 removed the three live reads: pi-peer-backup's host key, n8n's
registry digest and authelia's password digests. The stub file holds exactly R1's secret-key
stub map plus the sentinel `homelab_secret_stubs`, and a placeholder render overrides only
the secret keys.

Two tests hold this:

- `scripts/renderer/tests/test_renderer_stub_file.py` requires the stub file's key set to be
  the secret keys plus the sentinel. A stub writer that adds one other key fails it.
- The live-read census, `scripts/renderer/tests/test_renderer_live_read_census.py`, walks
  every task file a render target's `tasks/main.yml` reaches. It fails when a name a task
  sets reaches a template the render reads, directly or through a role default, an
  inventory value, an `include_role` var or a `set_fact`, unless the renderer derives that
  name from the tree. It also fails on a lookup whose reads it cannot name: any plugin but
  `file`, `template`, `pipe` and `vars`, a `pipe` other than `'base64 -w0 ' ~ <path>`, and
  a `vars` lookup outside a loop over `py_table('CHECKS')` or `scrape_jobs`. A read through
  `vars.X` or `hostvars[H].X` counts as a read of `X`, and a `vars` or `hostvars` read whose
  name it cannot list fails it too. The engine is `_live_read_census.py` beside it.

A new value that a task reads from the cluster and a template needs fails the census. Move
the value into the tree instead: a SOPS key, a committed file, or a value
`task_inputs.derived_facts` computes from the tree.

## Stub output is never published

A stub render is a comparison input. Nothing may push it as the cluster artifact, because
every secret key in it holds a stub, not a value the workload can use.
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

The file holds every stub and the sentinel `homelab_secret_stubs: true`. Pass it only to the
renderer, never to a run that applies anything.

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
  is refused even in an allowed dry run. Only `ansible/deploy.yml` sets that variable. Its
  Docker play sets it to true only where the host's `containers_list` holds no Docker entry,
  so a stubbed run aimed at a host with a Docker entry stops before any role. A stub file passed
  to any other playbook stops at its preamble, dry run or not.
- "Refuse a stub file that does not stub every key" reads the key names from the ciphertext
  `secrets.yml` and names each key that does not hold exactly its stub. A stub file written
  before a secret was added fails here and names the new key. To fix it, write a fresh stub
  file from the same tree.

## Hash a render tree

The render-diff job's `compare` holds two complete C1 trees to each other (*The render-diff
gate*), so nothing compares a render with a per-service tree.

`shadow_diff.py digest DIR` prints one line per directory under `DIR/cluster/`, as
`<directory> sha256:<hex>`, then a `total` line over every file under `DIR`. Each hash covers
every file's path relative to its directory and the file's bytes, with paths sorted by byte, so
no line depends on the absolute directory or the locale. A render of some tags gives the same
lines for those tags as a render of all of them. `digest_line` and `directory_digests` in
`scripts/renderer/renderer_lib/shadow.py` build the same lines for R1's determinism tests.

It exits 64 on a usage error. `scripts/lib/exit_codes.py` holds the codes as its `SHADOW_*`
family.

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
repo, the output directory, the stub file and the interpreter's prefixes, and on any
`socket.connect`. `/dev/null` is the one device it allows. It also ends the process on any
program start, because a child process reads the host without an `open` in the process the hook
watches. The hook lives in a subprocess because Python cannot remove an audit hook, and one
installed in an xdist worker would outlive the test.

The same subprocess sets `sys.modules['ansible'] = None`, so `import ansible` raises wherever the
renderer reaches for it, and the four modules together render every tag that way.
`scripts/renderer/tests/test_renderer_ansible_free.py`
holds the validator and a host package to the same condition, and proves the condition can fail:
a shim that makes `renderer_lib/filters.py` import `ansible` ends the render.

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

## The render-diff gate

The required `render-diff` job proves that a PR moved no rendered byte, or only the files it
named. R4 needs that proof because its refactors must leave the render alone, and R4.9
removes the job. It is not `scripts/validate/render_diff_gates.py`, whose arms refuse an
unsafe edge or removal.

`.github/workflows/render-diff.yml` calls `scripts/validate/render_diff_job.py` twice:

- `render` renders the PR's merge base and its head, each from its own checkout, tag list and
  lockfile, as the publish job does.
- `compare` compares the two complete trees and reads the PR body's `Render-diff:` line.

**Both renders take one commit value.** The `soak` Job embeds the commit it judges, so two
renders of two commits always differ under `cluster/soak/`. The job passes the PR head's SHA
to both renders as `--revision` (C1 *Modes*), so a remaining difference comes from the tree.

**The line has two forms.** `Render-diff: none` allows no difference. A list such as
`Render-diff: flux/x.yaml, cluster/x/y.yaml` must equal the differing files, except
`digests.json`, which moves with any file it digests. A line inside a code fence or an HTML
comment does not count.

**The line is mandatory on the hazard paths.** A PR with no line fails when it touches
`ansible/roles/k8s/*/tasks/`, `scripts/renderer/`, `scripts/lib/k8s_roles.py`,
`scripts/lib/ansible_jinja_env.py` or `ansible/filter_plugins/`, or deletes a file under
`ansible/roles/k8s/`. On any other PR with no line, the job prints the difference and passes,
because most PRs change render output on purpose.

**A renderer dependency bump needs the line too.** The job reads the closure of
`scripts/validate/render_diff_job.py:RENDERER_DEPENDENCIES` from `uv.lock`, and compares each
entry between the merge base and the head, with its `pyproject.toml` requirement. A moved entry
makes the line mandatory, so Renovate cannot automerge a bump that changes the render. A bump
of any other package needs no line. A census test,
`scripts/renderer/tests/test_renderer_dependency_pins.py`, holds the literal to the third-party
modules the renderer imports.

**Both trees must clear a floor.** `flux/pihole.yaml`, `flux/traefik.yaml` and
`cluster/pihole-deferred/` must exist, and `flux/` must hold at least `FLUX_FLOOR` files. A PR
that retires a tag lowers `FLUX_FLOOR` in its own diff and lists the tag's `flux/<tag>.yaml`.
Each listed retired tag lowers the head's floor by one.

**The merge base's copy of the job runs.** A change to `render_diff_job.py` takes effect once it
has merged, and its PR carries the line, because the module is a hazard path.

To reproduce a verdict by hand, follow *Exit 3* under *Verification* in
`docs/target-architecture/render/r4-delete-k8s-ansible-path.md`.

## What the render does not cover

A cluster render covers each role's applied directory, its deferred directory and its claim
files, and the shared kinds such as `homelab-namespace`. Files on a host are outside it, and
they belong to the host plane (*Render a host package*).

## Render a host package

Host mode renders one host's package, and package mode renders one shared package. The plan
is [H3, Pi pilot](target-architecture/host/h3-pi-pilot.md), whose *Shared names for the host
slices* fixes the source tree.

```bash
uv run python scripts/renderer/homelab_render.py host --host daniel-pi --out /tmp/h3
uv run python scripts/renderer/homelab_render.py package --name homelab-updater --out /tmp/h3
```

- `host` reads `hosts/<host>/package.yml` and writes `DIR/hosts/<host>/`.
- `package` reads `hosts/packages/<pkg>/package.yml` and writes `DIR/packages/<pkg>/`.
- Each writes `root/`, the file tree the package installs, and `nfpm.yaml`, the spec the
  builder runs `nfpm package` on.
- A missing spec, a key the renderer does not know, a destination that is not a plain
  absolute path, two files with one destination, or an existing package directory under
  `--out` exits 2 and writes nothing. A template that fails exits 1 and writes nothing.

A package spec lists its `components`, its `depends`, and optionally a `description` and files
and defaults of its own. A component is a directory under `hosts/components/` with a
`component.yml`, which holds a `defaults:` map and a `files:` list. Each file entry names a
`src` relative to the component, a `dst` and an octal `mode`, 0644 by default. The mode is a
quoted string such as `"0755"`: YAML loads an unquoted `0755` as the integer 493, so the
renderer refuses an integer. A `src` that ends `.j2` renders through the strict environment
cluster mode uses. Any other `src` is copied byte for byte.

A file entry may name `lib: NAME` instead of a `src`. It copies `hosts/lib/NAME`, a script
several packages ship, byte for byte. `NAME` is a plain name, and the file must resolve inside
`hosts/lib/` through any symlink. It is the one source outside the component that an entry may
name.

A package spec may also list `maintainer_scripts`: any of `preinst`, `postinst` and `prerm`.
Each renders from `hosts/lib/maintainer-scripts/<name>.j2` with the package's context plus
`package_units`, the unit files the package installs directly under
`/usr/lib/systemd/system/`. The render writes it to `DIR/<package>/scripts/<name>`, mode 0755,
outside `root/`, and `nfpm.yaml`'s `scripts:` names it. Only a host package may list
`preinst`, because it refuses a hostname mismatch. The maintainer-script lint in
`hosts/lib/tests/test_maintainer_scripts.py` holds each rendered script to the line shapes H3's
*Package rules* allow.

A package spec or a component may also carry `units:`, `enable:` and `legacy:`, which feed
the manifest.

- `units:` maps a unit to `{policy: manual}` or `{policy: windowed}`. Reconcile does not
  restart a unit with a policy when its file changes. The unit must be one the host package
  or `homelab-updater` installs, or one the host package drops in on.
- `legacy:` lists the Ansible-era entries reconcile retires: `{path: ...}`, `{cron: NAME,
  user: USER}`, `{cron_file: NAME}` or `{unit: NAME}`. Each value renders with the
  package's context, so `user: "{{ sys_user }}"` becomes the user's name. A shared package
  refuses a legacy entry, because reconcile reads only the host package's manifest.
- `enable:` maps a unit to the target that wants it, such as
  `{homelab-updater.timer: timers.target}`. The render ships the link `systemctl enable`
  writes: a symlink at `/usr/lib/systemd/system/<target>.wants/<unit>` pointing at
  `../<unit>`, as an `nfpm` `type: symlink` entry. systemd adds a `Wants=` only for a unit
  symlinked from a `.wants/` directory, so the render refuses a file entry there. The unit
  may be one `homelab-updater` installs, which is how a host arms the updater's timer. A
  shared package refuses an enable link, for the same reason it refuses a legacy entry.

Host mode writes `/usr/lib/homelab/manifest.json`, mode 0644, into every host package. It is
the manifest `hosts/lib/homelab-reconcile` reads, in the schema that script's docstring
documents. `units` lists every unit the host package and `homelab-updater` install, and every
unit the host package drops a `50-homelab.conf` into, each with its policy or `{}`.
`enable_links` lists each unit the host package's `enable:` maps link from a
`/usr/lib/systemd/system/<target>.wants/` directory. `legacy` holds the rendered entries, and
`reconcilers` is empty until H4. A spec file entry may not install the manifest's path.
The render refuses any manifest that `homelab-reconcile:parse_manifest` would refuse: an enable
link for a unit no package installs or drops in on, or a drop-in on a unit that is not a
service, timer, socket or path.

A host package's context is the host's inventory layers, as
`scripts/lib/render_context.py:render_context` builds them, with `inventory_hostname` set to
the host. A shared package's context has no host_vars layer. The component's `defaults:` map
goes in through the `component_defaults` keyword, beneath `group_vars/all.yml`. A host_vars
value of the same name therefore wins, as it does over a role default. No `ansible_facts`
value is in the context, so a template that reads one fails. A fact a template needs becomes
an inventory variable.

Every host package's `nfpm.yaml` carries `Depends: python3 (>= 3.12)`, because reconcile runs
under `/usr/bin/python3`. Its `version` is `${HOMELAB_VERSION}`, which `nfpm` expands from the
builder's environment, so two commits with the same tree render the same spec. Every package
is `arch: all`. The spec sets `version_schema: none`, so `nfpm` keeps the version as
given. Its default, `semver`, builds `0.5513+gc9db6da8cdee` as `0.5513.0+gc9db6da8cdee`, which
differs from the form H3's *Package rules* states.

### Host mode renders no secret

Only cluster mode has a substitution step after it, so only cluster mode renders placeholders
and escapes `$`.

- Host mode withholds every top-level key name of `ansible/vars/secrets.yml` from every layer,
  through `render_context`'s `withhold` keyword, and resolves under the strict undefined class.
  An inventory value that reads a secret is dropped with it.
- A template that reads a secret, or a value aliasing one, fails the render. The error names
  the secret key.
- A `${` in a host template is shell, and renders byte for byte. Host mode imports nothing
  from `placeholders.py`.

`scripts/renderer/tests/test_renderer_host_mode.py` proves both halves. The secret pair goes
red when the withholding is removed, because each fixture gives the secret's name a value in a
layer.
