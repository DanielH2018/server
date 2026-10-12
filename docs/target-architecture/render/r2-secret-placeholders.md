# R2 — Secret placeholders and CI secret checks

Status: PLANNED

Depends on: [R1](r1-shadow-render.md)

Unblocks: D2 and D3. D2's publish job renders bento-pdf, whose IngressRoute holds `domain`,
so it needs this slice's PRs 1 and 2; Open question 8 asks for that sub-edge in the
contract. D3 produces `homelab-cluster-vars` and the SOPS-encrypted Secret manifests this
slice hands over.

## Goal and exit criterion

After R2, `homelab-render cluster` renders every secret reference as the C1 placeholder
`${<key>}`. The renderer decides per Kustomization directory whether that directory
substitutes: it does when its render holds at least one placeholder. In a directory that
substitutes, the renderer escapes every other `$` that Flux would read as syntax. A directory
that does not substitute gets no `postBuild` and no escape. CI refuses a render that names an
unknown key, leaves a `${` unescaped in a directory that substitutes, puts a credential
outside a `kind: Secret` document, or substitutes a multi-line key. CI also refuses a template
that transforms a secret value beyond the identity, and a flux-owned tag that reads one of
R1's pinned live-read facts. The templates that cannot survive raw substitution are
restructured, and Ansible deploys the restructured templates with the same live behaviour.
Ansible stays the only writer to the cluster throughout.

**Exit criterion.** Four checks, all green at one master commit:

1. **The round trip is exact.** For all 56 tags and the three deferred directories,
   `roundtrip` builds each directory twice with `flux build kustomization --dry-run`.
    - A directory that substitutes is built on the placeholder side with each key's R1 stub
      value inline in `postBuild.substitute`, under `--strict-substitute`.
    - A directory that does not substitute is built with no `postBuild` on both sides.
    - The stub side is the R1 stub render, always built with no `postBuild`.

   The two outputs are equal byte for byte. Both pass through the same serialiser, so the
   comparison is of the objects Flux would apply. The only documents excluded are the three
   listed in `render_sops_manifests`, which D3 supplies as SOPS-encrypted manifests. The
   `render_sops_manifests` check under *Multi-line keys become dedicated Secrets* holds that
   list to exactly the three templates that name a multi-line key, and `roundtrip` fails
   unless it excluded exactly one document per entry.
2. **The five rules hold with no ratchet entries.** `renderer_lib/secret_checks.py` reports
   zero violations. Its `KNOWN_VIOLATIONS` set and the renderer's `PLACEHOLDER_DEFERRED` set
   are both empty. Rule 3's `PLACEHOLDER_ANYWHERE` is part of the rule, not an exemption
   from it, and so is the `(key, kind)` set that H1 adds later (Open question 10). Rule 5's
   `PASSTHROUGH_FILTERS` is part of its rule too, and each member passes its membership
   test.
3. **Each rule can go red.** Every rule has an `_is_clean` and `_is_flagged` test pair.
4. **The restructured roles behave as before.** `uptime-kuma`, `qbittorrent`, `mosquitto`,
   `home-assistant`, `pi-peer-backup` and `authelia` are deployed. Each passes the check
   listed for it under Verification.

## Baseline at `70591c307`

- **184 secret keys, names in plaintext.** Every top-level key in `ansible/vars/secrets.yml`
  holds one `ENC[...]` scalar. 183 are `type:str` and `mullvad_account` is `type:int`.
- **Two keys are identifiers, not credentials, and appear everywhere.** `domain` and `email`
  are secret keys. Outside the 40 template files that declare `kind: Secret`, `domain`
  appears 130 times and `email` 6 times, mostly in IngressRoutes, Middlewares and ConfigMaps.
  A rule that confines every placeholder to `kind: Secret` cannot hold for these two.
- **Other credentials outside Secret templates.** In the template sources, the
  remaining non-comment matches outside `kind: Secret` files are in app configs under
  `templates/config/` and in host scripts. The app configs (`configarr`, `janitorr`,
  `homepage`, `zigbee2mqtt`, `crowdsec`) are embedded by a top-level `config-secret` or
  `secret` template, so they render inside a Secret. The kind that matters is the kind of the
  rendered document, so PR 2 measures confinement on R1's output.
- **Literal `${` in template sources.** 95 occurrences in 13 files. Nine of the files are host
  scripts (`*.sh.j2`) that roles install on daniel-box, outside the cluster artifact. The
  cluster-side ones are `traefik/templates/deployment.yaml.j2` (1),
  `ansible/templates/crowdsec-agent.yml.j2` (2, in the pods of `traefik` and `authelia`) and
  `prowlarr/templates/netpol-probe-job.yaml.j2` (1, a task-applied Job). Embedded files add
  more: `qbittorrent/files/netns-reset.sh` holds 6 and is embedded into a ConfigMap by
  `lookup('file')`.
- **No render input holds `$$` or a `$` directly before `{{`.** Neither the templates nor
  the files under `ansible/roles/k8s/` and `ansible/templates/` contain either sequence.
  Every `lookup('file'|'template')` target in a cluster template sits under
  `ansible/roles/k8s/`.
- **configarr and janitorr.** Each role's `config-secret.yaml.j2` embeds an app config that
  holds API keys, through `lookup('template') | indent(4, true)`. Each role's health script
  (`configarr-health.sh.j2`, `janitorr-health.sh.j2`) mixes shell `${...}` with a push token.
  The scripts install on daniel-box, so they belong to the host plane.
- **Every context a secret key appears in, by a walk of the syntax tree.** The walk parses
  the 284 templates under `ansible/roles/k8s/*/templates/` and `ansible/templates/` that are
  not `*.sh.j2`, and classifies the parent node of every key it loads. It finds 323 uses:
  268 bare in output, and the 55 listed below. An earlier text census matched a key followed
  by `|`, `.` or `[`. It missed the 22 filter arguments, because a key in argument position
  is followed by `)` or `,`. Role defaults and inventory hold one alias, under rule 5's
  alias clause.
    - **Two transforms that parse or cut a value.**
      `home-assistant/templates/config/secrets.yaml.j2` runs
      `google_assistant_service_account | from_json` and emits `project_id`, `client_email`
      and `private_key`, which `home-assistant/files/configuration.yaml` reads with
      `!secret`. `mosquitto/templates/config-secret.yaml.j2` renders the password file as
      `{{ mqtt_username }}:{{ (mqtt_password_hash | string).split(':', 1) | last }}`. Its
      comment says this strips "any `user:` prefix baked into the stored hash".
    - **Two keys pass through `indent`.** `pi_peer_backup_ssh_key` in
      `pi-peer-backup/templates/secret.yaml.j2` (`indent(4, true)`), and
      `authelia_oidc_rsa_key_content` under `jwks` in
      `authelia/templates/config-secret.yaml.j2` (`indent(14)`). A multi-line value
      substituted raw breaks the YAML.
    - **Two secrets pass through `trim`.** `wireguard_interface_private_key` and
      `mullvad_account` in `qbittorrent/templates/secret.yaml.j2`. Flux substitutes the
      untrimmed value.
    - **18 branches test a secret's truthiness.** 17 `{% if <token> | default('') %}` in
      `uptime-kuma/templates/static-monitors.yaml.j2` and one on `healthchecks_ping_key` in
      `pi-peer-backup/templates/secret.yaml.j2`. A placeholder is always truthy, so an empty
      real value takes the other branch under Ansible.
    - **Expressions that keep the value intact.** `uptime-kuma/templates/secret.yaml.j2`
      applies `default('')` in value position to `kuma_status_page_sync_push_token` and
      `prometheus_kuma_api_key`. Seven `~` concatenations join `domain` to text: two under
      `to_json` in HA's `secrets.yaml.j2`, four in the list literals of two loops, and one
      in the bridge-row `tags` binding in `static-monitors.yaml.j2`.
    - **22 uses pass a key as an argument to a repo filter.** 21 are
      `containers_list | homepage_href('<name>', domain)` in
      `homepage/templates/config/services.yaml.j2`. One is the loop
      `{% for rule in containers_list | authelia_service_rules(domain, rfc1918_networks) %}`
      in `authelia/templates/config-secret.yaml.j2`. Both filters keep the value intact.
      `ansible/filter_plugins/homepage_tiles.py:homepage_href` returns
      `f"https://{host}.{domain}{path}"`.
      `ansible/filter_plugins/authelia_access.py:authelia_service_rules` builds
      `f"{hostname}.local.{domain}"` for each entry and sorts the names within each rule.
    - **Five bindings hold a key.**
        - The loop in the shared `ansible/templates/ingressroute.yml.j2` macro unpacks
          `host`, `rate_limit`, `suffix` and `tls_option` from a list of tuples whose first
          member is `hostname ~ '.local.' ~ domain` or `hostname ~ '.' ~ domain`. Every
          routed tag renders it.
        - `traefik/templates/livesync-gate-secret.yaml.j2` has a loop of the same shape,
          whose `host` member is `'livesync.' ~ domain` or `'livesync.local.' ~ domain`.
        - `static-monitors.yaml.j2` binds `tags` to a list of dicts, one of which holds
          `'https://docs.local.' ~ domain ~ '/' ~ check.runbook ~ '/'`. It then tests
          `{% if tags %}` and emits `{{ tags | to_json }}`.
        - The `authelia_service_rules` loop above binds `rule`.
        - HA's `secrets.yaml.j2` binds `ga` to `google_assistant_service_account |
          from_json`, the first transform listed above.

      Every later use of these names is bare, under `to_json`, an iteration, an attribute
      selection, `is defined`, or the `{% if tags %}` test on a list.
- **Push tokens resolve by name, through three non-literal `vars` lookups.**
  `scripts/lib/k8s_yaml.py:make_lookup` returns the context value for a `vars` lookup, so it
  returns the placeholder. Two templates read a bridge row's token this way and use it bare:
  `uptime-kuma/templates/static-monitors.yaml.j2` and
  `monitor-bridge/templates/env-secret.yaml.j2`, which renders
  `KUMA_PUSH_{{ check.name | upper }}: "{{ lookup('vars', check.token) }}"`. The third,
  `observability/templates/prometheus.yaml.j2`, applies `| int > 0` to
  `lookup('vars', job.replicas_var)`. `replicas_var` is a field of a `containers_list`
  entry's `metrics:` list (`ansible/filter_plugins/scrape_jobs.py`), and its one value is
  `terraria_k8s_replicas`, which is not a secret key.
- **Rule 5's subject, measured on the syntax tree.** All 294 `.j2` files under
  `ansible/roles/k8s/*/templates/` and `ansible/templates/` parse with Jinja's default
  delimiters. 68 of them name a secret key as a loaded variable or as the literal argument of
  a `vars` lookup. Nine of those are `*.sh.j2` host scripts, so 59 reach the cluster render.
  R1's text match finds 78, because it also counts a key name in a comment or in plain YAML,
  such as the `secretKeyRef` key name in `monitor-bridge/templates/deployment.yaml.j2`.
  No `Dockerfile*.j2` names a key. image-builder's `context-configmap.yaml.j2` embeds those
  templates into a cluster ConfigMap, and no cluster template embeds a `*.sh.j2`.
- **The deferred templates hold no `$` and no secret key.** Six templates feed the three
  deferred directories. Five are the files the roles pass as `manifests_deferred_files`:
  `pihole`'s `deployment-2.yaml.j2`, `headlamp`'s `rbac-observability.yaml.j2` and
  `netpol-baseline`'s three `networkpolicy-{observability,prometheus,loki}.yaml.j2`. The
  sixth is `pihole-deployment.yaml.j2`, the macro file that `deployment-2.yaml.j2` calls. So
  a per-tag and a per-directory substitution predicate render the same bytes at this commit.
- **Three tags read a pinned live-read fact.** R1's `PINNED_LIVE_FACTS` holds five facts.
  Their names appear in `authelia/templates/config-secret.yaml.j2` and
  `authelia/defaults/main.yml`, in n8n's `deployment.yaml.j2` and
  `deployment-runners.yaml.j2`, and in `pi-peer-backup/templates/secret.yaml.j2`, and in no
  other template or role default.
- **Secret is not a pruned kind.** `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist`
  lists no Secret. A Secret dropped from a role stays live until someone deletes it.
- **Strict substitution is the controller default.** D3's baseline reads kustomize-controller
  v1.9.6 source: `StrictPostBuildSubstitutions` is on, so an undefined `${var}` with no
  default fails the build.
- **The render-record dry run decrypts and runs as root.** `deploy.sh --dry-run` loads every
  secret through `ansible/pre_tasks/load_secrets.yml` (`community.sops.load_vars`).
  `ansible/roles/k8s/manifests/tasks/render_record.yml` and `release_digest.yml` read the
  root-only `manifests_secret_digest_key` and write their records with `become: true`.
  [The agent user](../../claude-agent-user.md) holds no age key and no sudo grant, so an
  agent session cannot run that dry run. The records themselves are root-owned and mode
  `0644`, so any account can read them.
- **No flux CLI on daniel-box.** `which flux` prints `flux not found`.
- **Six tests parse Authelia's rendered `configuration.yml`** with `yaml_fast.safe_load`:
  `test_authelia_config_schema.py`, `test_authelia_redis_sessions.py`,
  `test_smtp_wiring.py`, `test_authelia_access_tiers.py`, `test_grafana_authelia_oidc.py`
  and `test_authelia_argon2_parameters_agree.py`, all in `ansible/tests/services/`.
- **The Grafana UI test decrypts.** In `scripts/diagnostics/tests/test_ui_smoke_grafana.py`,
  the `domain` and `grafana` fixtures call `scripts/diagnostics/probe_lib/core.py:sops_extract`,
  which runs `sops -d --extract` and raises `SystemExit` on failure. An agent session cannot
  run it.
- **Flux's substitution grammar, read from source on 2026-10-11.** These are upstream facts
  at each repository's default branch. PR 1's CI red-proofs confirm them for the pinned CLI.
    - fluxcd/pkg `envsubst/parse/parse.go`, function `parseAny`: in text, only `$` is an
      escape character. In `envsubst/parse/scan.go`, function `scanEscaped`, a `$` followed
      by `$` is an escape, and `skip` drops the first one. A bare `$$` therefore becomes `$`,
      and `$${x}` becomes the literal `${x}`.
    - Same file, function `scanLbrack`: a reference opens only on `${`. A bare `$name` is
      never substituted. A backslash escapes only inside `${...}`.
    - fluxcd/pkg `kustomize/kustomize_varsub.go`, functions `SubstituteVariables` and
      `LoadVariables`: the controller substitutes each resource's whole YAML at once. It
      deletes every newline from every substitution value, inline or from a Secret.
    - fluxcd/flux2 `cmd/flux/envsubst.go`, function `runEnvsubstCmd`: the `flux envsubst`
      CLI reads standard input line by line with a default `bufio.Scanner`. It never checks
      the scanner's error, so a line of 64 KiB or more ends the output silently with exit 0.
    - fluxcd/flux2 `internal/build/build.go`: `flux build kustomization --dry-run` calls the
      controller's `SubstituteVariables` with dry run set. Dry run skips only the
      `substituteFrom` fetch, so an inline `postBuild.substitute` and `--strict-substitute`
      still apply.

The commands behind the tree counts above, from the repo root at `70591c307`.
`/tmp/keys.txt` holds key names only, written by the first command in
[R1's baseline](r1-shadow-render.md).

```bash
grep -rl '^kind: Secret' ansible/roles/k8s/*/templates ansible/templates | wc -l   # 40
grep -rlwFf /tmp/keys.txt ansible/roles/k8s/*/templates ansible/templates \
  | xargs grep -L '^kind: Secret' | xargs grep -owh 'domain\|email' | sort | uniq -c
                                                       # 130 domain, 6 email
grep -ro '\${' ansible/roles/k8s/*/templates ansible/templates | wc -l    # 95
grep -rl '\${' ansible/roles/k8s/*/templates ansible/templates | wc -l    # 13
grep -rlF '$$' ansible/roles/k8s/ ansible/templates/ | wc -l             # 0
grep -rlF '${{' ansible/roles/k8s/ ansible/templates/ | wc -l            # 0
grep -c '\${' ansible/roles/k8s/qbittorrent/files/netns-reset.sh         # 6
grep -rnE "\{%-? *if [a-z_0-9]+ *\| *default\(''\)" ansible/roles/k8s/*/templates | wc -l
                                                       # 18
hostname; which flux                                   # daniel-box, flux not found
grep -c '\$' ansible/roles/k8s/pihole/templates/{deployment-2,pihole-deployment}.yaml.j2 \
  ansible/roles/k8s/headlamp/templates/rbac-observability.yaml.j2 \
  ansible/roles/k8s/netpol-baseline/templates/networkpolicy-{observability,prometheus,loki}.yaml.j2
                                                       # 0 for each
grep -rlwE 'pi_peer_backup_known_hosts_line|authelia_[a-z_]*_password_hash|k8s_built_images' \
  ansible/roles/k8s/*/templates ansible/templates ansible/roles/k8s/*/defaults
                                                       # the five files named above
```

Rule 5's floor comes from a parse, not a text match. From the repo root:

```bash
uv run python - <<'EOF'
import glob, jinja2
from jinja2 import nodes
keys = set(open('/tmp/keys.txt').read().split())
env = jinja2.Environment(extensions=['jinja2.ext.do', 'jinja2.ext.loopcontrols'])
paths = glob.glob('ansible/roles/k8s/*/templates/**/*.j2', recursive=True) \
    + glob.glob('ansible/templates/**/*.j2', recursive=True)
def vars_lookups(tree):
    return [c.args[1] for c in tree.find_all(nodes.Call)
            if isinstance(c.node, nodes.Name) and c.node.name == 'lookup'
            and len(c.args) > 1 and getattr(c.args[0], 'value', None) == 'vars']
hits, dynamic = [], []
for p in paths:
    tree = env.parse(open(p).read())
    found = {n.name for n in tree.find_all(nodes.Name) if n.ctx == 'load'}
    found |= {a.value for a in vars_lookups(tree) if isinstance(a, nodes.Const)}
    if found & keys:
        hits.append(p)
    if any(not isinstance(a, nodes.Const) for a in vars_lookups(tree)):
        dynamic.append(p)
print(len(paths), len(hits), sum(p.endswith('.sh.j2') for p in hits), len(dynamic))
EOF
# 294 68 9 3: 294 parsed, 68 name a key, 9 of those are *.sh.j2, 3 non-literal vars lookups
```

The census of contexts and bindings comes from the same parse. It labels each key use by its
parent node, and lists every `{% set %}`, `{% with %}` and loop whose source holds a key:

```bash
uv run python - <<'EOF'
import collections, glob, jinja2
from jinja2 import nodes
keys = set(open('/tmp/keys.txt').read().split())
env = jinja2.Environment(extensions=['jinja2.ext.do', 'jinja2.ext.loopcontrols'])
paths = [p for p in glob.glob('ansible/roles/k8s/*/templates/**/*.j2', recursive=True)
         + glob.glob('ansible/templates/**/*.j2', recursive=True) if not p.endswith('.sh.j2')]
uses, binds = collections.Counter(), []
def walk(n):
    for field in n.fields:
        for k in (lambda v: v if isinstance(v, list) else [v])(getattr(n, field)):
            if isinstance(k, nodes.Name) and k.ctx == 'load' and k.name in keys:
                tag = type(n).__name__
                if isinstance(n, (nodes.Filter, nodes.Test)):
                    tag += ':' + n.name + (' subject' if field == 'node' else ' argument')
                uses[tag] += 1
            if isinstance(k, nodes.Node):
                walk(k)
for p in paths:
    tree = env.parse(open(p).read())
    walk(tree)
    for b in tree.find_all((nodes.Assign, nodes.AssignBlock, nodes.For, nodes.With)):
        src = b.iter if isinstance(b, nodes.For) else b
        if any(x.name in keys for x in src.find_all(nodes.Name) if x.ctx == 'load'):
            binds.append((p.split('/')[-1], type(b).__name__))
print(sum(uses.values()), sorted(uses.items(), key=lambda kv: -kv[1]))
print(binds)
EOF
# 323: Output 268, homepage_href argument 21, default subject 20, Concat 7, trim 2,
#      indent 2, from_json 1, authelia_service_rules argument 1, string 1
# five bindings: For in livesync-gate-secret, ingressroute.yml and authelia's
#      config-secret; Assign in static-monitors and HA's secrets.yaml
```

## Design

### Placeholders by sentinel, then escape

A placeholder cannot be emitted before escaping, because escaping would escape it too.
`renderer_lib/placeholders.py` therefore works in three passes:

1. The context maps each secret variable `K` to a sentinel, `HOMELAB_SECRET__K__END`. A sentinel
   of letters and underscores survives `default`, `indent`, `to_json` and `~` intact. It is
   also a valid start for a YAML plain scalar, so a render the renderer parses itself, such as
   `kuma_declarations` or the content tag's ConfigMap, still parses. No image build context
   reads a secret key, so no content tag hashes a sentinel.
2. In a directory that substitutes, every `$` that is followed by `$`, by `{` or by a
   sentinel becomes `$$`. Flux reads `$$` as one literal `$`, so every run of dollars
   survives substitution as written. A literal `${` becomes `$${`, and a bare `$$` becomes
   `$$$`, which Flux turns back into `$$`. The sentinel case keeps a literal `$` written
   directly before a secret reference from pairing with the placeholder's own `$`.
3. In a directory that substitutes, every sentinel becomes `${K}`.

Rule 5 refuses every transform that could alter a sentinel, such as `upper`, a slice or
`b64encode`, at its source. Rule 2 is the output-side backstop: a sentinel only partly
altered still leaves a fragment that rule 2 finds. A sentinel hidden entirely, as by
`b64encode`, leaves no text to find, so only rule 5 catches it. Open question 5 proposes the
C1 wording for the unit that pass 2 runs on.

### Which directories substitute

Between passes 1 and 2, `placeholders.py` computes each directory's placeholder key set: the
keys whose sentinel appears in that directory's render. The unit is the Kustomization
directory under `cluster/`, so `<tag>` and `<tag>-deferred` each have their own set.

- **A directory substitutes when its set is non-empty.** Passes 2 and 3 run on it, and its
  Flux Kustomization gets `postBuild`.
- **A directory with an empty set** is written exactly as rendered, every `$` included.
  Flux applies it without `postBuild`, so it never reads that `$` as syntax. An escape there
  would reach the cluster as a literal `$${`.

This key set is the one source for every reader that asks whether a directory substitutes,
and each reads it in process from the render result:

- passes 2 and 3, and the scope of rules 1 and 2 below;
- `roundtrip`, which gives the placeholder side `postBuild` only where the set is non-empty;
- D3's writer of `postBuild.substituteFrom`, and D3's `cluster_vars.py check`, whose dotenv
  key set is the union of these sets less `SOPS_MANIFEST_KEYS`;
- H1's refusal of a role that ships vendored files and also substitutes.

**The Flux file is a projection of the key set, not its source.** From D2 on, the renderer
writes a directory's Kustomization under `DIR/flux/`, and `spec.postBuild` is present there
exactly when the key set is non-empty. The rules and the round trip cannot read that file
instead, for two reasons:

- R2's PRs land before any `DIR/flux/` file exists. R1 writes none (R1 *Stub output is
  never published*), and D2, which writes the first, depends on R2.
- From D2 on, the renderer writes a Kustomization only for a tag passed with `--role`, and
  the publish job passes only flux-owned tags. The 56-tag CI render and the round trip
  therefore cover tags that have no Flux file.

C1 *Substitution and escaping* as it stands names a per-tag unit and the rendered
Kustomization as the source. Open question 5 proposes the replacement, and PR 1 waits on it.
contracts.md does not yet list that request under *Requested amendments not yet applied*, so
the contracts.md maintainer must record it first (Open question 5).

**Mode selection.** C1 *Modes* fixes the call, and R2 adds no flag. `cluster` without
`--secret-stubs` renders in placeholder mode. `--secret-stubs FILE` selects stub mode, which
is R1's shadow mode, unchanged. R1's documented commands all pass `--secret-stubs`, so they
keep rendering stubs. A job that publishes the artifact states its mode by passing no
`--secret-stubs`, which C1 *Only placeholder mode publishes* requires and D2's test on
`.github/workflows/promote.yml` asserts.

**Pinned live-read facts.** Until R3 deletes `PINNED_LIVE_FACTS`, placeholder mode reads the
five pinned live-read facts from that constant: the same values `shadow_diff.py stubs` writes
into the stub file. The round trip needs them on both sides. A pinned value must never reach
the cluster, for the reason R1 gives in *Stub output is never published*: a fixed password
digest or a stub host-key line renders a valid object that fails logins or backups. D2's
`no-stub` gate looks only for the `stub-<key>` form, so it cannot see these values. Only a
flux-owned tag is published, so the renderer refuses that one combination itself:

- A placeholder-mode render exits 1 when a tag that reads a pinned fact is marked
  `owner: flux` (C4). The message names the tag and the fact.
- The refusal reads the reader tags that `PINNED_LIVE_FACTS` records for each fact it still
  pins. R1 owns that set and holds it to the templates with its own test (R1 *Stub output is
  never published*). R2 derives the set nowhere else, so the refusal and R1's test cannot
  disagree about a tag. At `70591c307` the set is `authelia`, `n8n` and `pi-peer-backup`
  (Baseline).
- The refusal's named-member check reads no tag by name. It asserts that the refusal set
  equals R1's reader set, and that it is non-empty while `PINNED_LIVE_FACTS` holds a fact.
  R3's PRs 1, 2 and 4 each remove facts from the constant, and the check follows them with
  no edit.
- The refusal fires in PR CI on the commit that marks such a tag `owner: flux`, before any
  publish job runs. A tag that Ansible owns renders as before, so the fleet render and the
  round trip keep their pinned values.
- R3's PR 5 deletes `PINNED_LIVE_FACTS`. That PR deletes this refusal, its test pair and its
  sentence in `docs/homelab-render.md`, because the check's subject is then gone (Open
  question 11).

**Deferred tags.** `home-assistant` cannot render in placeholder mode until PR 5, because
`from_json` of a sentinel raises. The renderer's frozenset `PLACEHOLDER_DEFERRED` names it
with that reason. Placeholder mode skips a deferred tag, and the summary line names each one
it skipped. Stub mode renders every tag. PR 5 empties the set, and exit criterion 2 requires
it empty.

### The five rules

`renderer_lib/secret_checks.py` runs after every placeholder render. A violation makes
`cluster` exit 1 and print the tag, the file, the document's kind and the key.

1. **`known-key`**: in a directory that substitutes, every `${NAME}` that opens a Flux
   reference names a key in `ansible/vars/secrets.yml`.
2. **`no-bare-dollar-brace`**: in a directory that substitutes, the rule scans the output as
   Flux does, pairing each `$$` before it looks for `${`. Every `${` that opens a reference
   must be a placeholder from pass 3. In every directory, no sentinel fragment may remain. A
   `${` in a directory that does not substitute is literal text, because Flux never
   substitutes there.
3. **`confined-to-secret`**: a placeholder sits only in a document of `kind: Secret`. The two
   identifier keys, `domain` and `email`, are the exception, in a frozenset
   `PLACEHOLDER_ANYWHERE` whose comment gives the reason. A key that may leave a Secret for
   one kind only is a `(key, kind)` pair, never a member of `PLACEHOLDER_ANYWHERE`. H1 adds
   the first two pairs, for its BackupTargets (Open question 10).
4. **`no-multiline-key`**: no key in `SOPS_MANIFEST_KEYS` appears as a placeholder. That set
   holds `pi_peer_backup_ssh_key`, `authelia_oidc_rsa_key_content` and
   `google_assistant_service_account`. Flux deletes every newline from a substitution value
   (`LoadVariables`, under Baseline), so a multi-line value cannot pass through a
   placeholder at all.
5. **`no-secret-expression`**: this rule reads template sources, not output. It parses each
   template with the renderer's Jinja environment and walks the syntax tree. Its scope is
   every `.j2` under `ansible/roles/k8s/*/templates/` and `ansible/templates/` except
   `*.sh.j2`. A shell template is host-plane output, which C1 renders with no secret, and no
   cluster template embeds one (Baseline). `Dockerfile*.j2` stays in scope, because
   image-builder embeds it into a cluster ConfigMap.
   `scripts/lib/k8s_roles.py:is_manifest_template` exempts both shapes, but it answers a
   different question, which files parse as YAML, so rule 5 does not reuse it. A secret key
   may appear only in these forms, each of which leaves a single-line value intact:
    - as a bare value, `{{ K }}`;
    - under `default(...)` in value position;
    - under `indent(...)`, when `K` is not in `SOPS_MANIFEST_KEYS`;
    - inside a `~` concatenation, and under `to_json` applied to the key or to that
      concatenation;
    - as the subject of `is defined` or `is not defined`;
    - as an argument to a member of `PASSTHROUGH_FILTERS`, described below;
    - as a value bound to a name, or as an element of a list, tuple or dict literal. Each
      makes an alias, described below.

   The rule refuses every other filter (`from_json`, `trim`, `string` and the rest), every
   method call, attribute or subscript, every comparison, and any truthiness test in an
   `{% if %}`, whether bare, through `default('')` or through `length`. The allowed forms are
   the identity for the values D3's `write` accepts. That command refuses a newline, a double
   quote and a backslash, so `indent` changes nothing, and `to_json` adds only the
   surrounding quotes. A non-ASCII character differs only between two equivalent JSON
   escapes.

   **Filters that only interpolate.** The frozenset `PASSTHROUGH_FILTERS` names the repo
   filters that place an argument into their output unchanged. It is closed, so rule 5
   refuses a key passed to any other filter. It holds two members:
    - `homepage_href`, cited as `ansible/filter_plugins/homepage_tiles.py:homepage_href`;
    - `authelia_service_rules`, cited as
      `ansible/filter_plugins/authelia_access.py:authelia_service_rules`.

   A member's output holds the key, so rule 5 holds the output to the forms a `~`
   concatenation may take. Each member has a membership test. The test calls the filter with
   the arguments the templates pass, once with the sentinel `r2-sentinel` for the key and
   once with `a.b-c.example`. The first output, serialised as JSON, must hold the sentinel.
   Replacing the sentinel there with `a.b-c.example` must give the second output exactly. A
   filter that fails the test leaves the set.

   **Template-local aliases.** Binding a name to an expression that holds a key or an alias
   makes the name an alias, within the scope Jinja gives it. The bindings are `{% set %}`,
   `{% set %}…{% endset %}`, `{% with %}`, a `for` loop target, including every name of an
   unpacked target, and a macro parameter whose call passes such an expression. The binding
   is allowed. Each use of the alias is then checked as a use of the key. A binding whose
   own expression is a violation, such as HA's `ga = google_assistant_service_account |
   from_json`, is one violation, and its uses add none. An alias has one of two shapes:
    - A *value alias* is bound to one of the forms above. It is held to those forms.
    - A *container alias* is bound to a list, tuple or dict expression, or to a
      `PASSTHROUGH_FILTERS` member's output. Three more uses are allowed on it, because each
      selects a part and reads no character of the key: iterating it; selecting from it by a
      constant attribute name or string key; and an `{% if %}` on it when `{% set %}` bound
      it to a list, tuple or dict expression, a test that reads only its length.
    - A loop target over a list built from literals in the template takes the shape of its
      element, member by member when it unpacks a tuple. A loop target over any other
      container alias, and a part selected from one, is a container alias.

   Rule 5 has no types, so a part of a container alias that holds a string could be
   iterated character by character and pass (*Risks*). Without this clause,
   `{% set x = 'a.' ~ K %}{{ x | trim }}` would pass all three checks, for the reason the
   `vars`-lookup clause below gives.

   A key can also be reached by name, through `lookup('vars', X)`. With a string-literal `X`,
   the rule checks the lookup as a use of variable `X`. With any other argument, the lookup counts
   as a possible secret reference, and only the identity forms above may apply to it: bare,
   under value-position `default(...)`, or under `to_json`. The two push-token sites use it
   bare (Baseline). The third site, `prometheus.yaml.j2`'s `lookup('vars', job.replicas_var)
   | int > 0`, is the one member of the frozenset `NON_SECRET_VARS_LOOKUPS`. Its entry
   asserts that no `replicas_var` in any `containers_list` entry names a key of
   `secrets.yml`. An entry whose site has gone fails, like a stale `KNOWN_VIOLATIONS` entry.
   A site in `NON_SECRET_VARS_LOOKUPS` is not a secret reference for any clause of rule 5,
   the alias clauses included. A binding whose expression holds such a site and no key or
   alias is therefore no alias. The prometheus site sits inside the
   `{% set observability_prometheus_config %}` block, which the Deployment hashes with
   `hash('sha1')`. Without this sentence, that block would be an alias, and its hash would be
   a violation.
   Without this clause, a later `lookup('vars', check.token) | trim` would pass all three
   checks. Rule 5 would see no key name. Rule 2 would find the sentinel intact. The round
   trip would find both sides equal, because `trim` changes neither the sentinel nor the
   stub.

   A key can also be reached through a variable whose value names it. Rule 5 therefore also
   parses the Jinja strings in `ansible/roles/k8s/*/defaults/main.yml`,
   `ansible/inventory/group_vars/` and `ansible/inventory/host_vars/`.
    - A value there that names a secret key is an alias for it. Its own expression must be
      one of the forms above, and plain text around an interpolation counts as a `~`
      concatenation. A transform there is a violation.
    - The alias's name then counts as a secret reference in the template walk, so a template
      that transforms the alias fails as if it transformed the key. An alias of an alias
      counts the same way.
    - Without this clause, a default `x: "{{ K }}"` used as `{{ x | trim }}` would pass all
      three checks, for the reason the `vars`-lookup clause gives.
    - A parse of those 68 files at `70591c307` finds 184 Jinja strings, and one names a key:
      `ansible/roles/k8s/headlamp/defaults/main.yml:headlamp_k8s_oidc_issuer_url`, which is
      `https://auth.{{ domain }}`. Its one use, in `headlamp/templates/deployment.yaml.j2`,
      is bare.

`KNOWN_VIOLATIONS` starts with every violation PR 2 measures and shrinks to empty as PRs 3 to
8 land. An entry that no longer matches fails too, so the set shrinks only through a fix.
Each rule also asserts that it read something, so a rule that stops matching cannot pass
vacuously:

- Rule 3 must see `${domain}` in `homepage`'s IngressRoute.
- Rule 1 must see a placeholder in `authelia`'s config Secret.
- Rule 2 must see `$${` in `qbittorrent`'s `qbittorrent-netns-reset` ConfigMap, which embeds
  `qbittorrent/files/netns-reset.sh`. The `qbittorrent` directory substitutes, because its
  Secret holds the WireGuard key.
- Rule 4 asserts that its three keys exist in `secrets.yml`.
- Rule 5 must find a secret key in the syntax tree of at least 59 in-scope templates, the
  count at `70591c307` (Baseline). The floor counts what the rule computes, not a text
  match. Its other named members:
    - It accepts the value-position `default('')` on `kuma_status_page_sync_push_token` in
      `uptime-kuma/templates/secret.yaml.j2`.
    - It accepts the 21 `homepage_href` calls in
      `homepage/templates/config/services.yaml.j2`, and the `authelia_service_rules` loop in
      `authelia/templates/config-secret.yaml.j2` with every use of `rule` in its body.
    - It classifies the bridge-row `tags` binding in
      `uptime-kuma/templates/static-monitors.yaml.j2` as a container alias of `domain`, and
      accepts its `{% if tags %}` and `{{ tags | to_json }}`.
    - It classifies `host` in the `ansible/templates/ingressroute.yml.j2` macro's loop as a
      value alias of `domain`, and accepts its bare uses.
    - It classifies the bare `lookup('vars', check.token)` in
      `monitor-bridge/templates/env-secret.yaml.j2` as a possible secret reference.
    - It classifies `headlamp_k8s_oidc_issuer_url` as an alias of `domain`.
    - It classifies the `observability_prometheus_config` set block in
      `observability/templates/prometheus.yaml.j2` as no alias, and accepts its
      `| hash('sha1')` use, because the block's one candidate reference is the
      `NON_SECRET_VARS_LOOKUPS` site.

  No PR in this slice edits any of these uses. PR 3 changes only `static-monitors.yaml.j2`'s
  `{% if <token> | default('') %}` tests, and PR 7 only the `jwks` block of authelia's
  config. None of them is a ratchet entry.

**A `--role` render checks only what it rendered.** D2's publish job renders one tag, so a
named member in another tag must not fail it.

- Rules 1 to 4 read the rendered output. Each asserts a named member only when that member's
  tag is in the render.
- A `KNOWN_VIOLATIONS` entry for rules 1 to 4 counts as stale only when its tag was rendered.
- Rule 5 reads template sources, so it always scans the whole tree. Its 59-file floor, its
  named members and its stale-entry check hold on every run.

The unfiltered render, in CI and in `scripts/renderer/tests/`, asserts every named member.

### Multi-line keys become dedicated Secrets

Each of the three keys moves into its own small Secret, rendered from its own template whose
basename matches the `secret` file rule (`(^|-)secret[-.]`). Its consumer mounts it as a
file. Ansible renders these templates from SOPS as it does today.

The basename rule reaches only a role whose file lists are derived. `pi-peer-backup` and
`home-assistant` pass neither `manifests_files` nor `manifests_secret_files`, so their new
templates join the secret list by name. `authelia` passes both lists as Jinja expressions
in its `k8s/manifests` include, and the task "Resolve the manifest file lists" in
`ansible/roles/k8s/manifests/tasks/main.yml` takes a passed list exactly as given. PR 7
therefore adds `oidc-jwk-secret.yaml` to authelia's `manifests_secret_files` expression
itself, under the `authelia_k8s_manage_oidc` lever that gates the `jwks` block, in the
shape `crowdsec-secret.yaml` already uses. The file never goes in `manifests_files`, which
stages a file world-readable and outside `no_log`. Without the entry, no deploy would render
the Secret that the Deployment then mounts. `verify_secret_keys.yml` and `secret_digest` loop
over the resolved secret list, so they would miss it too.

Each dedicated template writes its key as `{{ K | to_json }}`, a double-quoted YAML scalar.
Rule 5 allows `to_json` on any key, and a JSON string is a valid YAML double-quoted scalar
whose escapes keep every newline. The `indent` form the old templates use would move rule 5's
violation into the new file, because each of these keys is in `SOPS_MANIFEST_KEYS`.

In placeholder mode the renderer emits no document for a template a role lists in the
defaults variable `render_sops_manifests`. Stub mode still renders it, so R1's shadow pair keeps
covering it.

That exclusion removes a document from both the published render and the round trip, so a
wrong entry would hide a whole Secret from the comparison meant to catch it. A check in
`secret_checks.py`, `sops-manifest-entries`, holds the list from PR 5 on. It runs on every
placeholder render and fails it with exit 1, like the five rules:

- Each entry yields exactly one document, and its kind is `Secret`. The check counts the
  documents the entry renders in placeholder mode, with sentinels, before the exclusion
  drops them, so it needs no stub file.
- Each entry's template names no secret key outside `SOPS_MANIFEST_KEYS`. Rule 5's syntax
  tree walk supplies the names, so an alias counts.
- Every entry names a `SOPS_MANIFEST_KEYS` key. Every template outside the list that names
  one carries a live `KNOWN_VIOLATIONS` entry for that key. From PR 9, with the ratchet
  empty, the entries are exactly the templates that name such a key.

A `--role` render applies the first two bullets only to the tags it rendered. The third
reads template sources, so it scans the whole tree on every run, like rule 5.

`roundtrip` counts the documents it excluded and fails unless the count equals the number of
entries in the tags it built: one after PR 5, two after PR 6 and three after PR 7.

D3 replaces each one with a SOPS-encrypted manifest that kustomize-controller decrypts,
which needs the cluster's age recipient that D3 adds. This is a second delivery path beside
`homelab-cluster-vars`, and C1 *Secret references* and C3 *Secrets* already state it (Open
question 6).

- `pi-peer-backup`: `ssh-key-secret.yaml.j2` renders Secret `pi-peer-backup-ssh-key`. The
  CronJob mounts the private key from it, and `secret.yaml.j2` drops the key.
- `authelia`: `oidc-jwk-secret.yaml.j2` renders Secret `authelia-oidc-jwk`, mounted read-only
  into the authelia pod. The `jwks[].key` value reads the mounted file through Authelia's own
  configuration template filter, enabled with `X_AUTHELIA_CONFIG_FILTERS=template`. The
  filter expression sits inside the `key: |` block scalar, as the line
  `{{- secret "<mount path>" | nindent 10 }}`. Before the filter runs, the file is valid
  YAML whose `key` is that line as a string, so the six config-parsing tests keep parsing
  it. When the filter runs, `{{-` deletes the whitespace back to the `|`, and `nindent 10`
  starts the PEM on a fresh line with each line indented to the block. The expression sits
  inside `{% raw %}` so Jinja leaves it alone.
- `home-assistant`: `google-sa-secret.yaml.j2` renders Secret `home-assistant-google-sa`
  holding `google_service_account.json`. `files/configuration.yaml` changes
  `google_assistant.service_account` to `!include google_service_account.json`, the form
  Home Assistant documents. The init step that installs `secrets.yaml` installs the JSON
  beside it. `project_id` comes from a separate SOPS variable, `google_assistant_project_id`, so
  the `from_json` parse goes.

### The round trip

`shadow_diff.py roundtrip` renders both modes into one directory. Its logic lives in
`renderer_lib/roundtrip.py`, and `shadow_diff.py` holds only the subcommand that calls it. It
walks every directory under `cluster/`, which includes the three `<tag>-deferred/`
directories R1 writes for pihole, headlamp and netpol-baseline. For each directory it writes
two throwaway Flux `Kustomization` files and runs `flux build kustomization <name> --dry-run
--kustomization-file <file> --path <dir>` once per mode.

- **Placeholder side, directory that substitutes.** The file carries every key's R1 stub
  value inline in `postBuild.substitute`, and the command adds `--strict-substitute`.
- **Placeholder side, directory that does not substitute.** The file has no `postBuild`.
  The comparison then proves that the renderer escaped nothing there.
- **Stub side.** The file has no `postBuild`, so nothing is substituted.

It then compares the two outputs byte for byte and names the first differing object.

This calls the controller's own `SubstituteVariables` per resource, so the CLI's line limit
and its line-by-line newline handling never apply. Both sides are serialised by the same
code, so no newline or whitespace normalisation is needed. `flux build` masks a Secret value
only when it holds both `sops` and `ENC[` (fluxcd/flux2 `internal/build/build.go`, function
`maskSopsData`). No stub holds either, so every Secret value reaches the comparison. YAML
comments drop out of both outputs. Rule 2 reads the raw render, comments included, so an
unescaped `${` in a comment still fails CI.

`roundtrip` needs the pinned flux CLI. It runs in CI. Where `flux` is not on `PATH`, it
exits 2 and says so, and never reports a pass.

**`roundtrip` outlives Ansible.** Its subject is placeholder mode against stub mode, and both
modes stay in the renderer after the Ansible k8s path goes. It needs stub mode and the stub
file that `shadow_diff.py stubs` writes. [R4](r4-delete-k8s-ansible-path.md)'s PR 9 deletes
only the `compare` subcommand. It keeps `stubs`, `digest` and `roundtrip` under their names,
so this slice's CI step needs no edit (Open question 9).

C1 *Modes* says that stub mode "exists only for R1's shadow comparison". From this slice's
PR 1, `roundtrip` reads stub mode too, and it keeps reading it after R4. C1 *Modes* therefore
needs a sentence that names `roundtrip` as a reader of stub mode (Open question 5).

### configarr and janitorr

configarr and janitorr are the first two roles through the round trip. Their Secrets embed
whole app configs with several placeholders inside an indented block scalar, which exercises
the `indent` path with single-line values. The templates that mix shell `${` with a secret
are their health scripts. Those install on daniel-box, so they are host-plane output, and C5
rules that a package carries only ciphertext. Those scripts therefore never carry a
placeholder. They read the token at run time, and the H-slice that packages them owns that
change.

### File tree

```diff
+ scripts/renderer/renderer_lib/placeholders.py   (passes; per-directory key sets)
+ scripts/renderer/renderer_lib/secret_checks.py
+ scripts/renderer/renderer_lib/roundtrip.py
+ scripts/renderer/tests/   (placeholder, rule and round-trip tests)
  scripts/renderer/homelab_render.py          (placeholder mode; PLACEHOLDER_DEFERRED;
                                               the pinned-fact refusal)
  scripts/renderer/shadow_diff.py             (+ roundtrip subcommand)
  .github/workflows/ci.yml                    (+ pinned flux CLI, + round-trip step)
  ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2
  ansible/roles/k8s/qbittorrent/templates/secret.yaml.j2
  ansible/roles/k8s/mosquitto/templates/config-secret.yaml.j2
  ansible/roles/k8s/mosquitto/tasks/main.yml  (+ the colon-count assert before the include)
  ansible/roles/k8s/mosquitto/CLAUDE.md
  docs/secret-rotation.md                     (+ the no-prefix rule for mqtt_password_hash)
  ansible/roles/k8s/pi-peer-backup/templates/{secret,cronjob}.yaml.j2
+ ansible/roles/k8s/pi-peer-backup/templates/ssh-key-secret.yaml.j2
  ansible/roles/k8s/home-assistant/templates/{config/secrets.yaml.j2,deployment.yaml.j2}
+ ansible/roles/k8s/home-assistant/templates/google-sa-secret.yaml.j2
  ansible/roles/k8s/home-assistant/files/configuration.yaml
  ansible/roles/k8s/authelia/templates/{config-secret,deployment}.yaml.j2
+ ansible/roles/k8s/authelia/templates/oidc-jwk-secret.yaml.j2
  ansible/roles/k8s/authelia/tasks/main.yml   (+ oidc-jwk-secret.yaml in the explicit
                                               manifests_secret_files expression;
                                               + the count assert before the include)
  ansible/roles/k8s/{pi-peer-backup,home-assistant,authelia}/defaults/main.yml
                                              (+ render_sops_manifests)
```

## Steps, as PRs

R2 moves no object between writers, so C4 has nothing to check inside the slice. Two points
carry forward. The three dedicated Secrets start life owned by Ansible. The PR that flips
each of those roles to `owner: flux` must add the role's SOPS-encrypted manifest in the same
commit, or the Secret has no writer under prune.

PRs 3 and 4 each need the **operator-only** pre-merge digest check under Verification. An
agent session must not merge either one until the operator has run it and both digests
match.

PRs 3 and 4 change no rendered YAML `#` comment. The digest covers every byte of a rendered
Secret, comments included (`ansible/roles/k8s/manifests/files/secret_hmac.py:digest`), so
one edited comment moves the digest while every value stays equal. Two rendered comments
become false through these PRs, and PR 4b rewrites them after PR 4:

- `qbittorrent/templates/secret.yaml.j2` says "`| trim` is load-bearing on both values",
  which PR 4 makes false.
- `pi-peer-backup/templates/secret.yaml.j2` says "Rendered empty when no key is
  configured" above the `{% if %}` that PR 3 rewrites.

mosquitto's comment on the split is a Jinja `{# ... #}` block, which renders nothing, so
PR 4 deletes it.

1. **Placeholder mode and the round trip** (about 200 lines).
    - *Merge gate.* The four amendments of Open question 5 have landed. The gate is met
      when all four of these hold:
        - C1 *Substitution and escaping* no longer contains the sentence "A tag
          *substitutes* when its render holds at least one placeholder", and no longer names
          `spec.postBuild` as what R2's CI rules and round trip read. Its unit is then the
          Kustomization directory, and its one source is the renderer's in-process key set.
        - C1 *Only placeholder mode publishes* no longer contains "a tag whose
          Kustomization has no `postBuild`". Its refusal is then stated per directory.
        - C1 *Modes* names `roundtrip` as a reader of stub mode.
        - H1 no longer contains the bullet "One per-tag flag decides substitution". Its
          unit is then the directory, and it names the key set as what R2's CI rules and
          round trip read.

      Until all four hold, PR 1 does not merge. Each check is a `grep -c` on
      `contracts.md` or H1's file that an executor can run. contracts.md does not yet list
      any of the four requests under *Requested amendments not yet applied*, so the
      contracts.md maintainer records them first, each with the gate "before R2 PR 1
      merges" (Open question 5).
    - `placeholders.py` with the per-directory key sets and the mode selection,
      `PLACEHOLDER_DEFERRED` holding `home-assistant`, `renderer_lib/roundtrip.py`, and
      `shadow_diff.py roundtrip`.
    - Tests in `scripts/renderer/tests/`: a directory with a secret escapes and one without
      does not; a `<tag>-deferred` directory gets its own key set; `cluster` without
      `--secret-stubs` renders placeholders.
    - A pytest unit test drives `roundtrip`'s comparison and reporting through a fake
      `flux`. It covers that code only and proves nothing about Flux.
    - CI gains a step that installs the flux CLI through the official action, pinned by
      commit SHA. The step first runs the pinned binary over a small fixture directory and
      fails unless every expected output appears:
        - a placeholder for a variable absent from `postBuild.substitute` fails under
          `--strict-substitute`;
        - `$${HOME}` comes back as `${HOME}`;
        - `$$$`, which pass 2 writes for a bare `$$`, comes back as `$$`;
        - `$$$$$${x}`, which pass 2 writes for a literal `$$${x}`, comes back as `$$${x}`;
        - `$$${K}`, which passes 2 and 3 write for a `$` directly before a secret
          reference, comes back as `$` plus the value;
        - `${HOME}` in a second fixture directory with no secret, built with no `postBuild`,
          comes back unchanged.
        - A `stringData` value written `"${n}"`, built once with `n=0123` and once with
          `n=true`. The step prints whether each result is still a string.

      The author of PR 1 runs the step on the branch first, records the two type results in
      `docs/homelab-render.md`, and then makes the case assert the measured result before
      PR 1 merges. A later bump of the flux CLI pin that changes the behaviour then fails
      CI. If either value comes back as a number or a boolean, Flux's substitution has
      dropped the template's quotes. The remedy is then a second `roundtrip` pass in which
      every key's stub is `0123` on both sides, and PR 1 ships it:
        - The stub side is a stub-mode render from a second stub file, so Jinja keeps the
          template's quotes there. Every site where substitution changes a value's type then
          shows as a difference, and exit criterion 1 cannot pass with one.
        - A C3 value rule cannot be the remedy, because `mullvad_account`'s real value is
          all digits and such a rule would refuse it.
    - The step then runs `roundtrip --role configarr --role janitorr`.
    - This PR records the measured behaviours, the per-directory rule and the mode selection
      in `docs/homelab-render.md`.
    - D2's publish job needs this PR and PR 2 (Open question 8).
    - **PR 1b, the pass-through filters** (about 70 lines), after PR 1 and before PR 2. It
      creates `secret_checks.py` holding only `PASSTHROUGH_FILTERS`, with `homepage_href`
      and `authelia_service_rules`, and a membership test per member. PR 2 builds on it, so
      D2's wait on PR 2 covers it.
2. **The five rules, ratcheted** (about 240 lines, over the guide because D2 waits on all of
   it and a split would add a sub-edge). The five rules in `secret_checks.py`, with their
   rule pairs and named members, and the `--role` scoping of named members and stale
   entries. `KNOWN_VIOLATIONS` holds every violation the first fleet render finds. For rule 5
   the census predicts the 18 truthiness tests, the two trims, mosquitto's split, HA's
   `from_json`, and the `indent` on each multi-line key. It predicts no entry for the 22
   filter arguments, which rule 5 accepts through `PASSTHROUGH_FILTERS`, or for the five
   bindings. The CI step widens to every tag outside `PLACEHOLDER_DEFERRED`, which is 55
   tags. It excludes the documents that `KNOWN_VIOLATIONS` names.
    - Rule 5's `vars`-lookup clause and `NON_SECRET_VARS_LOOKUPS`, with the prometheus site
      as its one member.
    - The pinned-fact refusal under *Mode selection*, which reads R1's reader tags from
      `PINNED_LIVE_FACTS`. D2's publish needs it, and D2 already waits on this PR. If R3's
      PR 5 has already deleted the constant, this PR ships no refusal (Open question 11).
    - **PR 2b, rule 5's aliases** (about 130 lines), after PR 2. Both alias clauses: the one
      over role defaults and inventory, with `headlamp_k8s_oidc_issuer_url` as its named
      member, and the template-local one, with the `tags` binding and the
      `ingressroute.yml.j2` macro's `host` as its named members. The census predicts no new
      entry. Every use of the five bindings is accepted, and the uses of HA's `ga` add none
      beside its `from_json` entry. D2 does not wait on this PR, because an alias clause
      only adds refusals of template sources.
        - The tree at `70591c307` has five set blocks, all in `observability`, and no
          `{% with %}`. No set block names a secret key. The prometheus block is the one
          that holds a candidate reference, its `NON_SECRET_VARS_LOOKUPS` site, so it is a
          named member that rule 5 must accept.
        - No macro call passes a key, so the set-block, `{% with %}`, unpacked-target and
          macro-parameter forms have no member in the tree. Each has an `_is_flagged` and
          `_is_clean` fixture pair under Verification instead, so none can stop being
          tracked while the tests stay green.
3. **Branches on a secret's value** (about 70 lines). The 18 truthiness tests become
   `<key> is defined`. Ansible defines every key in `secrets.yml`, so the rewrite keeps each
   branch for every non-empty value. An empty value would now take the other branch and
   change the rendered Secret. The pre-merge digest check catches that without anyone reading
   a value. Rule 5's entries for these tests leave `KNOWN_VIOLATIONS`. The rendered comment
   above pi-peer-backup's `{% if %}` stays as it is until PR 4b.
4. **Transforms on secrets** (about 100 lines, with the guard and its test). Drop both
   `| trim` filters in qbittorrent, and leave the rendered comment above them as it is. In
   mosquitto, the password-file line
   becomes `{{ mqtt_username }}:{{ mqtt_password_hash }}`, and the Jinja comment that
   explains the split goes with it. A value with surrounding whitespace, or a stored hash
   that carries a `user:` prefix, would change the rendered Secret, and the pre-merge digest
   check catches either. Open question 4 says what to do then.
   `ansible/roles/k8s/mosquitto/CLAUDE.md`, *Secrets*, gains the rule that the stored hash
   carries no `user:` prefix. Rule 5's three entries leave `KNOWN_VIOLATIONS`.
    - The split was the only guard against a prefixed hash. `mosquitto_passwd` writes
      `username:hash` lines, and `ansible/secret_rotation.yml` lists `mqtt_password_hash`
      at tier `assisted`, so a person performs each rotation. A rotation that stored the
      whole line would render `user:user:$7$...`, and every MQTT client would fail auth on
      the next deploy.
    - `docs/secret-rotation.md` gains a bullet in the `assisted` examples: store only the
      part after the first `:` of the `mosquitto_passwd` line.
    - `ansible/roles/k8s/mosquitto/tasks/main.yml` gains two tasks before the
      `k8s/manifests` include. The first, under `no_log`, sets as a fact only the number of
      `:` in `mqtt_password_hash`. The second is an `assert` without `no_log` that fails the
      deploy unless the number is 0. Its message prints the number and no value. The deploy
      stops before the apply, so the old Secret stays live. A `$7$` hash holds no `:`.
    - A test in `ansible/tests/services/` evaluates the first task's expression through
      Ansible's templar over fixture values. `$7$abc` counts 0 and passes the assert, and
      `user:$7$abc` counts 1 and fails it. The test also asserts that both tasks precede the
      include and that the first carries `no_log`.
    - The assert runs only while Ansible renders mosquitto. D5's flip PR for mosquitto
      makes Ansible stop rendering it, and from then on `homelab-cluster-vars` supplies the
      value. D3's `cluster_vars.py write` is the one writer of that Secret, so its
      refusal of a `:` in `mqtt_password_hash`, which D3 plans in D3.2d, is the successor
      guard. D5's flip PR for mosquitto must wait until D3.2d is merged (Open question 12).
    - **PR 4b, the comment follow-up** (about 10 lines), after PR 4 deploys. It rewrites
      the two rendered comments named above and changes nothing else. A reviewer checks it
      from the diff alone: every changed line is a YAML `#` line, so no value can move, and
      it needs no digest check. Its deploy moves `secret_digest` by design, so the
      post-deploy record comparison under Verification does not apply to it.
5. **Home Assistant's service account** (about 170 lines, with the entries check). This is
   the first `render_sops_manifests` entry. C1 *Secret references* and C3 *Secrets* already
   state that path, so this PR has no contract gate (Open question 6).
    - **Operator-only:** add `google_assistant_project_id` with `sops` and run
      `uv run python scripts/secrets_mgmt/secret_rotation.py sync`.
    - Then the template split, the `!include`, the init step, and
      `render_sops_manifests: [google-sa-secret.yaml]`.
    - `home-assistant` leaves `PLACEHOLDER_DEFERRED`, so the CI round trip covers all 56
      tags from here on.
    - The `sops-manifest-entries` check, with its `_is_clean` and `_is_flagged` pair, and
      `roundtrip`'s assert that it excluded one document per entry. Until PRs 6 and 7 land,
      pi-peer-backup's `secret.yaml.j2` and authelia's `config-secret.yaml.j2` still name
      their keys, and their live `KNOWN_VIOLATIONS` entries are what let the check pass.
    - `scripts/home_assistant/validate_ha_config.py` checks `!include` targets, so it learns
      that the JSON, like `secrets.yaml`, arrives at run time.
6. **pi-peer-backup's SSH key** (about 90 lines). The dedicated Secret, the CronJob mount and
   `render_sops_manifests: [ssh-key-secret.yaml]`. `verify_secret_keys.yml` patches the
   dropped key out of the old Secret on the deploy. [R3](r3-remove-live-reads.md)'s PR 1 edits
   the same `secret.yaml.j2`, so whichever lands second rebases.
7. **Authelia's OIDC key** (about 150 lines). The dedicated Secret, the volume mount, the
   environment filter and the block-scalar `jwks[].key` expression.
    - `ansible/roles/k8s/authelia/tasks/main.yml` adds
      `(['oidc-jwk-secret.yaml'] if authelia_k8s_manage_oidc | bool else [])` to the
      `manifests_secret_files` expression of its `k8s/manifests` include, never to
      `manifests_files` (*Multi-line keys become dedicated Secrets*). The Deployment's
      volume, its mount and `X_AUTHELIA_CONFIG_FILTERS` sit behind the same lever, so a
      cluster with OIDC off neither renders the Secret nor mounts it.
    - A test in `ansible/tests/services/` evaluates authelia's `manifests_secret_files` and
      `manifests_files` values through Ansible's templar with the role's defaults, as
      `ansible/tests/k8s/test_derived_manifest_files.py:ansible_resolve` does for the
      resolve task. With the lever on, `oidc-jwk-secret.yaml` is in the secret list and not
      in the files list. With the lever off, it is in neither. It does not use
      `scripts/lib/k8s_roles.py:resolved_manifest_files`, which counts every basename in a
      Jinja value, a conditional branch included, so it would pass with the lever arm
      broken.
    - A test in `ansible/tests/k8s/` asserts that every top-level manifest template of each
      `JINJA_LIST_ROLES` role is named in one of the role's two lists. Here
      `resolved_manifest_files`' over-inclusion is the right reading, because the question
      is whether a template is named anywhere. At `70591c307` it is green: authelia's nine
      templates, traefik's fourteen and navidrome's two are all named. A fixture role that
      passes an explicit list and ships one unlisted template is flagged. After PR 7,
      `oidc-jwk-secret.yaml.j2` is its named member.
    - `ansible/tests/services/test_authelia_config_schema.py` gains three assertions. The
      filter variable and the mount path agree. The rendered config holds no `{{` other
      than the one filter expression, because the filter runs Go templating over the whole
      file. Resolving that expression with a fixture PEM, under the documented semantics of
      `secret` and `nindent`, yields a config whose `jwks[0].key` equals the fixture byte
      for byte.
    - The schema test renders stub values, so it never sees a real one. A real value that
      held `{{` would open a second Go template action, and Authelia would refuse to start.
      `ansible/roles/k8s/authelia/tasks/main.yml` therefore gains two tasks before the
      `k8s/manifests` include. The first renders `config-secret.yaml.j2` with
      `lookup('template')` under `no_log`, and sets only the number of `{{` in its
      `configuration.yml` as a fact. The second is an `assert` without `no_log` that fails
      the deploy unless the number is 1. Its message prints the number and no value. The
      deploy stops before the apply, so the old config stays live. Both tasks run only when
      `authelia_k8s_manage_oidc` is true. With the lever off, the config holds no filter
      expression and the filter is not enabled, so a `{{` in a value is plain text.
    - A test in `ansible/tests/services/` asserts that both tasks precede the include, that
      the first carries `no_log`, that the second compares the count to 1, and that both
      carry the lever's `when`.
    - The count assert runs only while Ansible renders authelia. After D5's flip PR for
      authelia, Flux substitutes the values from `homelab-cluster-vars`, and no deploy task
      reads the rendered config. The successor guard is the refusal of `{{` in any value,
      which D3 plans in `cluster_vars.py write`'s value rule from D3.2a. D5's flip PR for
      authelia must wait until it and D3.2d's argon2 refusal are merged, and C3's value
      rule must list `{{` (Open question 12). Automatic rotation never writes `{`, because
      `scripts/secrets_mgmt/secret_rotation.py:cmd_rotate` draws new values with
      `token_hex`. A refusal can therefore only meet a value a person stored.
    - Five more tests in `ansible/tests/services/` parse the same config and must stay green
      unchanged: `test_authelia_redis_sessions.py`, `test_smtp_wiring.py`,
      `test_authelia_access_tiers.py`, `test_grafana_authelia_oidc.py` and
      `test_authelia_argon2_parameters_agree.py`. The block scalar is what keeps them
      parsing.
    - [R3](r3-remove-live-reads.md)'s PR *authelia's digests from SOPS* edits the same
      `config-secret.yaml.j2` and needs its own SSO window. Whichever lands second rebases.
      The two deploys never share a window, and the second starts only after the first one's
      Verification checks pass.
    - Authelia is the SSO for the fleet, so deploy it in a window with the rollback below at
      hand.
8. **Confinement moves, if PR 2 found any** (one PR per role, each under 100 lines). Each
   moves a credential from a non-Secret document into the role's Secret, with `secretKeyRef`
   or a mounted file. The source census predicts none beyond the two identifier keys.
9. **Close the ratchet** (about 40 lines). `KNOWN_VIOLATIONS` and `PLACEHOLDER_DEFERRED` are
   empty, the round-trip step covers every tag, and the docs state the five rules as
   enforced. With the ratchet empty, the `sops-manifest-entries` check reads as equality:
   the three entries are exactly the templates that name a `SOPS_MANIFEST_KEYS` key.

## Verification

**Exit checks.** The render and the tests run anywhere:

```bash
uv run python scripts/renderer/homelab_render.py cluster --out /tmp/r2
uv run pytest scripts/renderer/tests
```

`homelab_render.py` exits 0 only when all five rules pass, and its summary line must name no
deferred tag. The round trip runs in CI only, because daniel-box has no flux CLI. Read its
summary line from the CI run for the commit:

```bash
gh run view <run-id> --log | grep 'roundtrip:'
```

It must read `roundtrip: 56 tags, 3 deferred directories, <s> substituting, <n> objects,
0 differences, 3 documents in render_sops_manifests`. The last field is asserted, not only
printed: `roundtrip` exits 1 when the excluded count differs from the number of entries.

**Before merging PRs 3 and 4: the Secret bytes must not move (operator-only).** A
render-record dry run digests secret manifests with the host-local HMAC key, so it compares
the bytes without printing them. It decrypts SOPS and writes as root (Baseline), so the
operator runs it, on daniel-box, from a worktree at the PR's base and then at its head:

```bash
mkdir -p /var/tmp/r2-base /var/tmp/r2-head
./scripts/deploy.sh --dry-run --skip-staleness-check --tags "uptime-kuma,pi-peer-backup" \
  -e manifests_render_record=true -e manifests_render_record_dir=/var/tmp/r2-base \
  > /var/tmp/r2-base/play.log 2>&1 < /dev/null
# Repeat at the head commit with /var/tmp/r2-head, then compare:
jq -r .secret_digest /var/tmp/r2-base/uptime-kuma.json /var/tmp/r2-head/uptime-kuma.json
```

Each service's two `secret_digest` values must be equal and non-empty. PR 4 runs the same
check with `--tags "qbittorrent,mosquitto"`. The digest covers rendered comments too, so the
check means "no value moved" only because PRs 3 and 4 leave every rendered `#` comment as
it is (*Steps, as PRs*).

**After each of those deploys, an agent confirms it** for every service the PR deployed,
here mosquitto. The release record keeps one step of history, and both files are
world-readable:

```bash
jq -r .secret_digest /var/lib/homelab/k8s-releases.d/mosquitto.json \
  /var/lib/homelab/k8s-releases.d/mosquitto.previous.json
```

The two values must be equal and non-empty. This detects a moved Secret after the fact and
prevents nothing. On a mismatch, revert the PR and redeploy the role. The pre-merge check
remains the gate.

**Per-role behaviour after each restructure deploys:**

- **uptime-kuma (PR 3).** `uv run python scripts/diagnostics/probe.py kuma-drift` reports no
  monitor added or removed.
- **qbittorrent (PR 4).** `probe.py health qbittorrent` passes, and the VPN sidecar holds its
  tunnel.
- **mosquitto (PR 4).** `probe.py health mosquitto` and `probe.py health zigbee2mqtt` pass,
  so the broker accepts its clients. The deploy's play log shows the colon-count assert
  passed.
- **home-assistant (PR 5).** `probe.py health home-assistant` passes. The HA log shows the
  `google_assistant` integration set up with no error, and a `request_sync` from the HA UI
  succeeds.
- **pi-peer-backup (PR 6).** Start a manual run with
  `kubectl -n homelab create job ppb-r2 --from=cronjob/pi-peer-backup` (**operator-only**, a
  write verb). The run must push `up` with a file count.
- **authelia (PR 7).** Four checks:
    - **Operator-only, before the window:** a `--dry-run` deploy of `authelia` from the PR's
      head, with `-e manifests_render_record=true` and a record directory as in the digest
      check above, runs the count assert under PR 7 against the real values. It must pass.
      Then `jq -r '.secret_manifests[]' <dir>/authelia.json` must list
      `oidc-jwk-secret.yaml`, which proves the deploy renders the Secret the Deployment
      mounts. The server-side dry run alone would accept a Deployment that names a missing
      Secret. The deploy in the window runs the same assert before its apply.
    - `probe.py health authelia` passes.
    - Agent-runnable: the portal serves the same signing key. Read the portal's LAN host
      name from its IngressRoute with read-only `kubectl`. Fetch
      `/.well-known/openid-configuration` there, take its `jwks_uri`, and record
      `jq -c '[.keys[] | {kid, n}]'` of that document before and after the deploy. The two
      must be equal.
    - **Operator-only:** `uv run pytest -m ui -k grafana` signs in through Authelia's OIDC
      client. Its fixtures decrypt SOPS, so the operator runs it. pytest's summary line
      must show passed tests and no skipped ones, because a skip is not evidence.

**Red-proofs:**

- Each rule: an `_is_clean` and `_is_flagged` pair over a small fixture, plus the named
  members each rule must find in the real render or the real templates. Rule 5's pair flags
  mosquitto's `(K | string).split(':', 1) | last` and an `{% if K %}`. It passes `{{ K }}`,
  `K | default('')`, `('x' ~ K) | to_json`, `K is defined`, and `{{ M | to_json }}` for a
  fixture variable `M` in `SOPS_MANIFEST_KEYS`, the form the dedicated Secret templates use.
- Rule 5's pass-through filters: a fixture filter outside `PASSTHROUGH_FILTERS` that
  receives a key is flagged. The same call, with the fixture filter added to a fixture set,
  is clean. A fixture filter that returns its argument upper-cased fails the membership
  test, and each real member passes it.
- Rule 5's template-local aliases: `{% set x = 'a.' ~ K %}{{ x | trim }}` and
  `{% for h in ['a.' ~ K] %}{{ h | trim }}{% endfor %}` are flagged, and so is
  `{% set x = 'a.' ~ K %}{% if x %}`. The same two bindings used bare are clean, and so is
  `{% set t = [{'v': 'a.' ~ K}] %}{% if t %}{{ t | to_json }}{% endif %}`. Four more
  binding forms have no member in the tree, so each gets a pair, flagged with `| trim` on
  the bound name and clean with the name used bare:
    - a set block, `{% set x %}a.{{ K }}{% endset %}{{ x | trim }}`;
    - `{% with x = 'a.' ~ K %}{{ x | trim }}{% endwith %}`;
    - an unpacked loop target,
      `{% for a, b in [('a.' ~ K, 1)] %}{{ a | trim }}{% endfor %}`;
    - a macro parameter,
      `{% macro m(h) %}{{ h | trim }}{% endmacro %}{{ m('a.' ~ K) }}`.
- Rule 5's `NON_SECRET_VARS_LOOKUPS` sites and aliases: a fixture set block holding only
  the allowlisted lookup, used as `| hash('sha1')`, is clean. The same block with a key
  added is flagged. Against the real tree, the `observability_prometheus_config` block is
  accepted.
- The count assert in PR 7: the test evaluates the first task's count expression through
  Ansible's templar, over a fixture config in place of the lookup. A fixture holding only
  the filter expression counts 1 and passes the assert. The same fixture with a value of
  `x{{y` counts 2 and fails it. With `authelia_k8s_manage_oidc` false, both tasks are
  skipped.
- Authelia's secret list in PR 7: the templar test above is red at PR 7's base, where
  `oidc-jwk-secret.yaml` is in neither list, and green at its head. The orphan test flags
  its fixture role and passes the real tree.
- The colon-count assert in PR 4: `$7$abc` passes and `user:$7$abc` fails, as stated
  under PR 4.
- The `sops-manifest-entries` check: a fixture entry naming `secret.yaml`, a template that
  renders a Secret of ordinary keys, is flagged. So is an entry whose template renders two
  documents, and a fixture template that names a `SOPS_MANIFEST_KEYS` key with no entry and
  no ratchet entry. The real tree is clean at each PR from 5 on. A fixture where the
  renderer drops one document more than the entries list makes `roundtrip` exit 1.
- Rule 5's `vars` lookups: `{{ lookup('vars', t) | trim }}` is flagged and
  `{{ lookup('vars', t) }}` is clean. `{{ lookup('vars', 'K') | trim }}` is flagged as a use
  of `K`. A `NON_SECRET_VARS_LOOKUPS` entry fails when a fixture `replicas_var` names a key
  of `secrets.yml`.
- Rule 5's aliases, over a fixture role default: `x: "{{ K | trim }}"` is flagged, and so is
  `x: "{{ K }}"` used in a template as `{{ x | trim }}`. `x: "https://a.{{ K }}"` used as
  `{{ x }}` is clean.
- The pinned-fact refusal, over a fixture constant that pins one fixture fact read by one
  fixture tag. With that tag marked `owner: flux`, `cluster --role <tag>` exits 1 and names
  the fact (`_is_flagged`). With the tag unmarked, or with the fact removed from the
  fixture constant, the same render exits 0 (`_is_clean`). Against the real tree, the
  refusal set equals the reader set `PINNED_LIVE_FACTS` records, and is non-empty while the
  constant holds a fact.
- Escaping: a fixture template holding shell `${HOME}`, a bare `$$`, `$$${x}`, a `$` before
  a secret reference, and a secret renders to `$${HOME}`, `$$$`, `$$$$$${x}`, `$$${key}` and
  `${key}`. The same template without the secret, in a fixture directory of its own, renders
  every `$` unchanged, and that directory's key set is empty.
- The round trip, in CI against the pinned binary: the six asserted fixture cases in PR 1
  above, plus the two measured type cases, whose results PR 1 records. A
  one-byte edit to a `stringData` value of a Secret in the placeholder render makes
  `roundtrip` report that Secret. The edit targets a Secret because Secrets hold nearly
  every placeholder.
- `roundtrip` with no `flux` on `PATH` exits 2 and reports no pass.
- `--role` scoping: a `--role bento-pdf` placeholder render exits 0 although `homepage`,
  `authelia` and `qbittorrent` are absent. The same named members, removed from an
  unfiltered fixture render, each fail it.

## Rollback

- **PRs 1, 1b, 2, 2b and 9** change only the renderer and CI. A revert restores R1's
  behaviour. Reverting PR 1b alone needs PR 2 reverted first, because rule 5 reads its set.
- **PRs 3 and 4** change no rendered byte, which the operator's pre-merge digest check
  proves. A revert restores the old template and needs no redeploy. Reverting PR 4 also
  removes mosquitto's colon-count assert, so a prefixed hash is again stripped rather than
  refused.
- **A failed colon-count assert in a deploy** stops it before the apply, so the live
  mosquitto Secret keeps its old password file. The operator fixes the stored value
  (Open question 4) and redeploys.
- **PR 4b** changes only rendered comments. A revert restores the old comments, and the
  next deploy of each role applies them, with no value changed.
- **PRs 5 to 8** add a Secret. A revert and a redeploy restore the old template, and the
  apply puts the moved keys back into the old Secret. Secret is not a pruned kind, so the
  dedicated Secret stays live, unused. Deleting it needs a write verb (**operator-only**).
- **A partial failure inside PR 7** can stop Authelia from starting, and with it SSO for every
  route behind it. Revert the PR and deploy `--tags authelia` from a worktree at the revert.
  The old config needs nothing the new Secret holds. The operator's pre-window dry run reads
  `secret_manifests` from the render record, so a missing list entry stops PR 7 before the
  window rather than inside it.

## Risks

- **A value that is not safe in its context.** A placeholder inside a JSON string, such as the
  Discord webhook in `static-monitors.yaml.j2`, or after `to_json`, needs a value with no
  quote, backslash or control character. For those characters Ansible has the same exposure
  today, since it embeds the values raw. C3 *Secrets* states the rule, and D3's `write`
  enforces it (Open question 7).
- **A value whose YAML type substitution can change.** `mullvad_account` is all digits, and
  qbittorrent's Secret writes it inside double quotes. Ansible has no type exposure here,
  because Jinja writes the template's quotes into the applied text. Flux substitutes into a
  re-serialised resource, and this plan has not established that the quotes survive that
  step. If they do not, a value such as `0123` or `true` changes type, and the Secret fails
  to apply. The round trip cannot show it, because every stub is a plain-safe string.
  *Mitigation:* PR 1's two measured type cases decide it against the pinned CLI and then
  assert the measured result. If the quotes are lost, PR 1 ships a second round-trip pass
  with type-hostile stubs. A cross-slice note asks D3 to include an all-digit value in a
  quoted field in its proof in the cluster.
- **A wrong `render_sops_manifests` entry (failure class 3).** The entry removes a document
  from both the publish and the round trip, so a widened list reads as clean.
  *Mitigation:* the `sops-manifest-entries` check holds each entry to one Secret of
  multi-line keys and the list to the templates that name one, and `roundtrip` asserts the
  excluded count.
- **A name-derived list against an explicit one (failure class 4).** A new template joins a
  role's lists by its basename only when the role passes no list. authelia, navidrome and
  traefik pass theirs. *Mitigation:* PR 7 lists authelia's file in the expression, a
  templar test pins it under both lever states, the orphan test fails on a template no list
  names, and the operator's pre-window dry run reads `secret_manifests`.
- **A prefixed MQTT hash after PR 4.** Without the split, a rotation that stores the whole
  `mosquitto_passwd` line breaks every MQTT client. *Mitigation:* `docs/secret-rotation.md`
  states the rule where the rotation reads it, and the colon-count assert fails the deploy
  before the apply. The assert stops running at D5's flip PR for mosquitto. D3.2d's
  refusal of a `:` in that key is its successor. If the flip merged before D3.2d, nothing
  would guard the value, so Open question 12 asks D5 to make D3.2d a precondition of the
  flip.
- **A pinned live-read value published.** Placeholder mode carries R1's five pinned values,
  which render valid objects that fail logins or backups. *Mitigation:* the pinned-fact
  refusal under *Mode selection* fails the commit that marks such a tag `owner: flux`.
  Without it, only the dependency graph (D5 after R3) kept those tags unpublished. The
  refusal reads R1's reader set rather than deriving its own, so R3's PRs 1, 2 and 4 shrink
  it with no edit here. R3's PR 5 must delete it with the constant (Open question 11).
- **A dedicated Secret's value ends differently.** Today the SSH key and the OIDC key render
  through a `|` block scalar, which ends the value in exactly one newline. `to_json` keeps
  the stored value as it is, so a stored key with no final newline renders without one.
  *Mitigation:* PR 6's manual run and PR 7's JWKS check show whether the consumer accepts
  the key. If one does not, the operator adds the newline to the stored value with `sops`
  (**operator-only**), as Open question 4 does for whitespace.
- **Restart triggers lose sight of secrets.** The five `checksum_annotation('config', ...)`
  lines in observability hash a config that holds placeholders. A rotated secret then changes
  no annotation and rolls no pod. D3 owns the restart mechanism. The design's open question 4
  already names the ciphertext-hash variant.
- **Failure class 2 (proxy).** A green round trip proves the substitution is exact for the
  stub values, through the controller's own function as the pinned CLI vendors it. It does
  not prove the in-cluster controller version behaves the same. D3's exit criterion 1 proves
  substitution in the cluster for peanut, and D5 repeats that check for each role it moves.
- **Failure class 3 (empty read as clean).** A rule that matches nothing passes. Each rule
  names a member it must find. `roundtrip` exits 2 without a flux binary, and the Grafana
  UI check counts a skip as a failure.
- **Failure class 4 (selector drift).** Rule 3 reads the rendered kind, not the template
  filename, because an app config under `templates/config/` renders inside a Secret that
  another file declares. Rule 5 walks the syntax tree rather than matching text, so a
  transform written across a line break or behind parentheses is still seen, and its floor
  counts what the walk finds. A key reached through a non-literal `vars` lookup has no name
  in the tree, so rule 5 treats that lookup as a possible secret reference. A key reached
  through a role default or an inventory value has no name in the template either, so
  rule 5 parses those values and treats an alias as the key. A name bound inside a template
  is tracked the same way, and a filter argument is accepted only for a filter whose
  membership test shows it passes the value through. Whether a
  directory substitutes has one source, its placeholder key set. The escape, rules 1 and 2,
  `roundtrip`, D3's `postBuild` writer and H1's vendored-file refusal all read it, so no two
  of them can disagree about a directory. The tags that read a pinned fact have one source
  too, R1's `PINNED_LIVE_FACTS`, which both R1's test and the pinned-fact refusal read.
- **`roundtrip` dropped by R4.** R4's PR 9 keeps `roundtrip`, `stubs` and `digest`, and
  deletes only `compare`. A later change to that PR that dropped `roundtrip` would remove the
  round trip's guarantee with no failing check. *Mitigation:* *The round trip* states the
  dependency, and the C1 *Modes* sentence that Open question 5 requests makes `roundtrip` a
  named reader of stub mode in the contract.
- **Failure class 5 (sequences as transactions).** Moving a key out of a Secret is two writes:
  the new Secret, and the old Secret's key removal. `verify_secret_keys.yml` performs the
  second on the same deploy, and the deploy's rollout gate then watches the consumer.
- **Failure class 7 (execution context).** The digest check and the Grafana UI test both
  decrypt SOPS, and the digest check also needs root. Run each as the account that holds the
  age key, which is the operator's. An agent session runs only the post-deploy record
  comparison and the JWKS check, and it does not merge PRs 3 or 4 without the operator's
  digest result.
- **Authelia's filter syntax.** If the pinned image does not support the template filter or
  `nindent`, the pod fails at start. PR 7's deploy runs in a window, and the schema test
  pins the shape.
- **A real value that Authelia's filter reads as template syntax.** The filter parses the
  whole rendered `configuration.yml`, and that file interpolates many decrypted keys. C3's
  value rule does not exclude `{`, and every test renders stub values. A real value holding
  `{{` would stop Authelia from starting, and with it SSO for the fleet. *Mitigation:* PR 7's
  count assert fails the deploy before the apply unless the rendered config holds exactly
  one `{{`, and it prints no value. The operator's dry run before the window runs the same
  assert. The assert runs only while Ansible renders authelia, and stops at D5's flip PR
  for authelia. D3's `write` refuses `{{` in any value, and that refusal is the successor.
  D5's authelia precondition asks only that three argon2 keys are present, so Open
  question 12 asks D5 to require the refusals as well, and C3 to list `{{` in its value
  rule.
- **Rule 5 has no types.** A part of a container alias that holds a string, such as `host`
  in authelia's `rule.domain` loop, could be iterated character by character and pass the
  rule. *Mitigation:* none in this slice. No template at `70591c307` iterates such a part,
  and a reviewer reads any new `{% for %}` over a part as a transform.

## Tests and docs that change in the same PR

- PR 1 extends `docs/homelab-render.md` with the placeholder and escape rules, the
  per-directory key set, the mode selection, `PLACEHOLDER_DEFERRED` and the measured CLI
  behaviours, the two type results among them. R1's shadow-pair procedure there needs no
  change, since it passes `--secret-stubs`.
- PR 1b adds `PASSTHROUGH_FILTERS` and its membership test to `docs/homelab-render.md`.
- PR 2 adds the rule table and the pinned-fact refusal to `docs/homelab-render.md`.
- PR 2b adds rule 5's two alias clauses to the same page, with the residual that a part of
  a container alias is untyped.
- PR 3 changes `ansible/roles/k8s/uptime-kuma/CLAUDE.md` if a sentence there names the
  `default('')` form.
- PR 4 updates `ansible/roles/k8s/mosquitto/CLAUDE.md`, *Secrets*, and adds the
  `mqtt_password_hash` bullet to the `assisted` examples in `docs/secret-rotation.md`. It
  adds the colon-count assert's test to `ansible/tests/services/`.
- PR 4b changes no doc. Its whole diff is the two rendered template comments.
- PR 5 updates `ansible/roles/k8s/home-assistant/CLAUDE.md` and
  `ansible/roles/k8s/home-assistant/docs/platform.md` on where the Google service account
  lives. `ansible/secret_rotation.yml` gains the added key. `docs/homelab-render.md` gains
  the `sops-manifest-entries` check and `roundtrip`'s excluded-count assert.
- PR 6 updates `ansible/roles/k8s/pi-peer-backup/CLAUDE.md`, *Auth*.
- PR 7 updates `ansible/roles/k8s/authelia/CLAUDE.md`, *OIDC clients* and *Traps*, the
  second with the count assert and the explicit secret-list entry, and
  `ansible/tests/services/test_authelia_config_schema.py`. It adds the count assert's test
  and the secret-list test to `ansible/tests/services/`, and the orphan test to
  `ansible/tests/k8s/`.
- `docs/secret-rotation.md` gains a paragraph per dedicated Secret: rotating its key now means
  rendering that one Secret.
- Until G2 retires `docs/facts.lock`, when a role `CLAUDE.md` section edited in PRs 3 to 7
  gains a backticked path or symbol, that PR runs
  `uv run python scripts/dev/fact_status.py verify '<doc>#<heading>'` and commits the lock.
  After G2, the PR does neither: G2 deletes the lock, and its `lock-retired` rule fails
  while the file exists.

## Complexity delta

- **Added:** about 500 lines of placeholder, rule and round-trip code with about 470 lines
  of tests, among them rule 5's two alias clauses and `PASSTHROUGH_FILTERS` with its
  membership tests; one CI step and one pinned action; three small Secret templates and one
  SOPS key; authelia's two count-assert tasks and their test; one entry in authelia's
  explicit secret list, with a templar test and an orphan test over the three roles that
  pass explicit lists; mosquitto's two colon-count tasks and their test; the
  `sops-manifest-entries` check and `roundtrip`'s excluded-count assert, with their pair;
  two measured type cases in the CI fixture; four more alias fixture pairs and one
  `NON_SECRET_VARS_LOOKUPS` pair; the pinned-fact refusal, which reads R1's reader set and
  which R3's PR 5 deletes with `PINNED_LIVE_FACTS`. No CLI flag: the call is C1's.
- **Removed:** the `from_json` parse, mosquitto's split, two `indent` interpolations of
  credentials, 18 truthiness branches and two `trim` filters.
- **Handed over, not counted here:** D3's refusals of `{{` and of a `:` in
  `mqtt_password_hash`, with their fixture tests, which succeed PR 4's and PR 7's asserts
  once D5 flips those roles (Open question 12). D3 counts them.
- **Net for operators:** rotating one of the three multi-line keys touches one small Secret
  instead of a large config Secret.

## Open questions

1. **What does the pinned flux CLI do with `${undefined}`, `$${x}` and a bare `$$`?**
   *Answered from source* (Baseline, *Flux's substitution grammar*): `$$` becomes `$`, so
   `$${x}` becomes `${x}`; a reference opens only on `${`. The round trip always runs with
   `--strict-substitute`, so an undefined variable fails it. PR 1's CI red-proofs confirm
   each behaviour for the pinned binary.
1. **Does kustomize-controller substitute exactly as the round trip does, and does it offer a
   strict mode?** *Partly answered.* D3's baseline shows strict substitution on by default in
   kustomize-controller v1.9.6. The round trip calls the controller's own
   `SubstituteVariables` through `flux build --dry-run`. *Resolution* for the remaining
   version gap: D3's exit criterion 1 in the cluster, then D5's per-role repeat.
1. **Which Authelia filter functions does the pinned image support?** *Resolution:* PR 7
   reads the documentation for `authelia_k8s_image`'s version, confirms `secret` and
   `nindent`, and pins the expression in the schema test.
1. **What if a digest check in PR 3 or 4 fails?** Then a real value is empty, carries
   whitespace, or, for `mqtt_password_hash`, carries a `user:` prefix. *Resolution:* the
   operator fixes the value with `sops` (**operator-only**), such as deleting a trailing
   newline, an unused empty key or the prefix, rather than keeping the transform in the
   template. Then the operator reruns the check. Before that, an agent checks whether the
   PR's diff touched a rendered `#` comment. The digest covers comments, so an edited comment
   moves it with every value equal. In that case the PR restores the comment, PR 4b carries
   the rewrite, and the operator reruns the check with no `sops` edit.
1. **C1 *Substitution and escaping* differs from this slice on the unit and on the source
   (C1, then H1).** C1 as it stands says "A tag *substitutes* when its render holds at least
   one placeholder". It then says "The rendered Kustomization is the one source for this
   fact. R2's CI rules, R2's round trip and D3's `substituteFrom` rule read whether
   `spec.postBuild` is present there, and none of them derives it a second time." The
   contracts.md review log closes X-12 with "Fixed: C1 escapes exactly when the rendered
   Kustomization carries `postBuild`, and names that field as the one source." Those
   sentences are not this slice's design, so X-12 is open again from R2's side.
    - *The unit.* `postBuild` is a field of each Kustomization, and C3 gives a deferred apply
      its own, `<tag>-deferred`. A per-tag flag leaves open whether `<tag>-deferred` carries
      `postBuild` when only `<tag>` holds a placeholder. D3 *Which Kustomizations substitute
      (D3.3b)* already decides per directory.
    - *The source.* C1's source does not exist when R2's checks run (*Which directories
      substitute*). R1 writes no `DIR/flux/` directory, and D2 writes the first and depends
      on R2. From D2 on, only a tag passed with `--role` gets a Kustomization file, so the
      56-tag CI render has none to read for most tags.
    - *The output.* At `70591c307` the two predicates render the same bytes, because no
      deferred template holds a `$` or names a key (Baseline). The difference is in the
      interface and in PR 1's merge gate, not yet in the output.

   Proposed replacement for those two sentences: "A Kustomization directory under
   `DIR/cluster/` *substitutes* when its render holds at least one placeholder. `<tag>` and
   `<tag>-deferred` are decided separately. The one source for this fact is the renderer's
   placeholder key set for that directory, computed in process from the render. The escape,
   R2's CI rules, R2's round trip and the writer of each Kustomization's `postBuild` (D2,
   then D3's `substituteFrom` rule) all read that key set, and none of them derives it a
   second time. Wherever the renderer writes the directory's
   Kustomization under `DIR/flux/` (D2 on), `spec.postBuild` is present there exactly when
   the key set is non-empty. That field is the key set's published projection." The rest of
   the paragraph changes "tag" to "directory".
    - C1 *Only placeholder mode publishes* takes the same unit: "a Kustomization without
      `postBuild` whose directory holds `${<key>}`" replaces "a tag whose Kustomization has
      no `postBuild` holds `${<key>}`".
    - H1 *One per-tag flag decides substitution* needs the same change. Its unit becomes the
      directory, and its last paragraph says that R2's CI rules and round trip read the key
      set, with `spec.postBuild` as the projection.
    - C1 *Modes* needs one sentence. It says that stub mode "exists only for R1's shadow
      comparison", but `roundtrip` reads stub mode from this slice's PR 1 and keeps reading
      it after R4 (*The round trip*). The sentence names `roundtrip` as a reader of stub
      mode. [R4](r4-delete-k8s-ansible-path.md) Open question 9 asks for the same sentence,
      with R1's determinism module as a second reader, gated before R4's PR 9. R2's gate is
      earlier, before R2's PR 1 merges, because the second reader exists from then. The
      call itself stays as C1 writes it.
    - C1 *Secret references* needs nothing more. It already says that a
      `render_sops_manifests` template renders no document in placeholder mode.

   *Owner:* the contracts.md maintainer, then H1. *Status:* not yet recorded. contracts.md
   *Requested amendments not yet applied* holds nine entries, and none of them is the C1
   *Substitution and escaping* wording, the C1 *Only placeholder mode publishes* wording,
   the C1 *Modes* sentence or H1's per-tag flag. Its review log still closes X-12.
   *Resolution:* the contracts.md maintainer lists each of the four requests there with the
   gate "before R2 PR 1 merges", and reopens X-12 in the log. R2 hands the request over as
   a cross-slice note. The C1 changes then land before R2's PR 1 merges, and PR 1 states
   the gate as a check an executor can run.
1. **The second secret path in C3.** *Applied.* C3 *Secrets* says that the keys in
   `SOPS_MANIFEST_KEYS` ship as SOPS-encrypted Secret manifests, one per
   `render_sops_manifests` entry, which kustomize-controller decrypts. C1 *Secret
   references* says that such a template renders no document in placeholder mode. PR 5 has
   no contract gate left.
1. **Value encoding in `homelab-cluster-vars` (C3, D3).** *Applied for the character
   rule.* C3 *Secrets* states that a value holds no newline, double quote, backslash or
   control character, because Flux deletes newlines and a placeholder may sit inside a JSON
   string. D3's `cluster_vars.py write` enforces it. D3's `write` also refuses `{{`, which
   C3's rule does not list yet. Open question 12 asks C3 to add it. Whether
   a value's YAML type survives substitution is a separate question, which PR 1's two
   measured type cases answer for the pinned CLI. If the template's quotes are lost, PR 1
   ships a second `roundtrip` pass with type-hostile stubs, which needs no contract
   amendment (*Risks*). D3's proof in the cluster covers the controller version.
1. **Where `domain` comes from before D3, and D2's sub-edge on R2 (C3, D2 and the slice
   index).** D2's leaf, bento-pdf, takes its IngressRoute from
   `ansible/templates/ingressroute-default.yaml.j2`, which imports the `ingressroute.yml.j2`
   macro. The macro builds the Host rule as `hostname ~ '.local.' ~ domain`, and `domain` is
   a key of `secrets.yml`. Under R2, bento-pdf's directory substitutes and the artifact
   carries `${domain}`, which strict substitution refuses without a source.
    - *The source.* C3 *The source of `domain` before D3* settles it. The default is that
      D2.7 waits on D3.3 and `domain` comes from `homelab-cluster-vars`. The alternative is a
      literal `domain` in the render, which only the operator may choose
      (**operator-only**). C3 rejects a Secret the operator creates by hand.
    - *A conflict for D2 and the contract.* D2 *Where `${domain}` comes from before D3*
      plans a third source, `homelab-identifiers`, which the Ansible-owned flux role renders
      from SOPS. C3 names it neither as the default nor as the alternative. D2 and the
      contracts.md maintainer settle it (D2 Open question 5). R2 needs nothing from either
      choice, because every source substitutes the same `${domain}`.
    - *The edge.* The index row for D2 reads `D1, R1, R2`, which waits on all of R2. D2's
      publish needs only PRs 1 and 2: placeholder mode, the per-directory key set, the five
      rules, the `--role` scoping and the pinned-fact refusal. R2 asks for the sub-edge "D2
      on R2.1 and R2.2". contracts.md does not yet list that request under *Requested
      amendments not yet applied*. The contracts.md maintainer records it there, from R2's
      cross-slice note. No R2 PR waits on the sub-edge; only D2's PRs gain from it.
    - *What the sub-edge does not buy.* Under C3's default, D2.7 waits on D3.3, and the row
      `D3 | D2, R2` makes D3.3 wait on all of R2, PR 7's Authelia window included. The
      sub-edge frees D2's other PRs from R2's PRs 3 to 9. D2.7 avoids them only under C3's
      operator-only alternative, or under `homelab-identifiers` if the contract adopts it.
    - *The publish command.* D2's publish passes no `--secret-stubs`, and its test on
      `.github/workflows/promote.yml` asserts that. R2 adds no flag for it. D2's *source*
      gate refuses a `${K}` whose key the tag's declared source lacks. It pairs each `$$`
      first, as rule 2 does, so it reads the same references the key set holds.

   *Owner:* D2 and the contracts.md maintainer.
1. **What R4's PR 9 does with `roundtrip` (R4).** *Answered by R4.* PR 9 deletes only the
   `compare` subcommand of `shadow_diff.py`. `stubs`, `digest` and `roundtrip` keep their
   names, so R2's CI step stays as it is. The contract half, a C1 *Modes* sentence naming
   `roundtrip` as a reader of stub mode, is in Open question 5. *Owner:* R4.
1. **Bucket placeholders in a BackupTarget (H1 Open question 5).** H1.7b templates the
   BackupTargets `default` and `r2`, and each URL holds one placeholder,
   `${longhorn_b2_bucket}` or `${r2_bucket}`. Both are keys of `secrets.yml`
   (`grep -oE '^[a-z0-9_]*bucket[a-z0-9_]*:'` on it prints exactly those two names). Rule 3
   as written fails that render. *Resolution:* R2 accepts H1's request in this form:
    - Rule 3 gains a frozenset `PLACEHOLDER_KIND_EXEMPT` of `(key, kind)` pairs beside
      `PLACEHOLDER_ANYWHERE`. A placeholder outside a Secret passes when its key is in
      `PLACEHOLDER_ANYWHERE` or its `(key, kind)` pair is in the set. The two members are
      `(longhorn_b2_bucket, BackupTarget)` and `(r2_bucket, BackupTarget)`, and the comment
      gives H1's reason.
    - The set carries its own `_is_clean` and `_is_flagged` pair: `${r2_bucket}` in a
      BackupTarget passes, and the same placeholder in a ConfigMap fails. Its named member
      is the `default` BackupTarget in the `longhorn-backup` render.
    - H1.7b edits `secret_checks.py` and adds the set, the pair and the named member, in the
      PR that moves the BackupTargets into the renderer. R2 adds nothing earlier, because
      the set has no subject before then. H1.7b waits on D3, which waits on R2, so the file
      exists by then.
    - `homelab-cluster-vars` then carries both keys without further work, because D3 derives
      its key set from the directory key sets.

   H1's alternative, plaintext values in the inventory, needs their plaintext and stays
   **operator-only**. *Owner:* H1.
1. **What R3's changes to `PINNED_LIVE_FACTS` do to the refusal (R3).** R3 changes the
   constant in four steps. Its PR 1 removes `pi_peer_backup_known_hosts_line`, its PR 2
   removes `k8s_built_images`, its PR 4 removes the three hash facts, and its PR 5 deletes
   the empty constant and its read. R3's plan does not yet name the refusal.
    - *PRs 1, 2 and 4* need nothing from R2's side. The refusal reads R1's reader set, so a
      removed fact drops its tags from the refusal with no edit. The named-member check
      asserts equality with that set, not with a list of tags.
    - *PR 5* removes the refusal's subject. That PR deletes the refusal, its test pair and
      its sentence in `docs/homelab-render.md`.
    - *Landing order.* The index makes R2 and R3 each depend only on R1, so either can land
      first. If R3's PR 5 lands before R2's PR 2, R2's PR 2 ships no refusal and its
      sentence in `docs/homelab-render.md` is not written. If R2's PR 2 lands first, R3's
      PR 5 deletes them.

   *Owner:* R3. *Resolution:* R3's PR 5 names the refusal among the things it deletes. R2
   hands this over as a cross-slice note.
1. **Successor guards for PR 4's and PR 7's asserts (D3, C3, D5, R4).** Two deploy-time
   asserts guard a stored value that no template check can see: mosquitto's colon count
   (PR 4) and authelia's `{{` count (PR 7). Each is a task of its role, so each stops
   running at D5's flip PR for that role. From then on Flux substitutes the values from
   `homelab-cluster-vars`. D3's `cluster_vars.py write` is the one writer of that Secret,
   so the guard belongs there.
    - *D3, taken up.* D3's plan has `write` refuse `{{` in any referenced value from
      D3.2a, beside the newline, double quote, backslash and control character. D3.2d adds
      two per-key refusals: a `:` in `mqtt_password_hash`, and R3's PHC-shape and
      distinctness check for the three `_argon2` keys. Each refusal names the key and never
      the value. D3.2d's fixture tests include PR 4's pair: `$7$abc` is clean and
      `user:$7$abc` is refused. D3's red-proofs refuse a fixture value holding `{{` and
      pass one holding a single `{`. R2 needs nothing more from D3.
    - *C3, open.* The value rule in *Secrets* lists no `{{`. It gains `{{`, because
      Authelia's template filter parses the whole rendered config (PR 7). contracts.md
      *Requested amendments not yet applied* does not hold this request.
    - *D5, open.* D5's authelia precondition is that "D3's cluster file holds R3's three
      argon2 keys", which checks presence only, and D5 names no precondition on mosquitto's
      value. D3 says that D5's authelia and mosquitto PRs wait on D3.2d. D5's own text
      must say so too, for each flip PR. An executor checks the precondition by finding
      D3.2d's fixture tests in `scripts/secrets_mgmt/tests/`. D5's table of imperative
      tasks gains a row for these pre-apply value guards, beside its pre-apply live-read
      row.
    - *The PR that deletes each assert task, open.* That is D5's flip PR, its
      task-deletion follow-up, or R4. Its body names D3's refusal as the successor, as
      `.claude/rules/python-layout.md` *A check retires when its subject does* asks of a
      check that another check covers.

   No R2 PR waits on this question. PRs 4 and 7 guard their roles for as long as Ansible
   renders them, and the gap opens only at a D5 flip. *Owner:* C3 and D5, and R4 for the
   citation. *Status:* D3 has taken up its part; the rest is not yet recorded.
   *Resolution:* the contracts.md maintainer records the C3 row with the gate "before D5's
   authelia flip PR merges", and R2 hands the D5 and R4 parts over as cross-slice notes.

## Review log

### Round 1 (per-slice adversarial review)

Raised:

- R2-1 (high): Mosquitto also parses a secret; no rule catches it and the round trip
  cannot see it.
- R2-2 (high): `render_sops_manifests` departs from C1 and C3 without an Open question
  naming the contract row; two claimed contract gaps are listed nowhere.
- R2-3 (high): The pre-merge digest check decrypts SOPS and needs root, but is not marked
  operator-only.
- R2-4 (medium): PR 7's OIDC proof errors or skips in an agent session.
- R2-5 (medium): PR 2's fleet-wide placeholder render crashes on home-assistant until PR 5
  lands.
- R2-6 (medium): The round-trip oracle is line-based `flux envsubst`, and its class-2
  mitigation points at a stage cluster that does not exist.
- R2-7 (medium): A bare `$$` is rewritten by Flux, but no pass escapes it and no rule
  refuses it.
- R2-8 (medium): The `--strict` red-proof runs against a fake, and `roundtrip` needs a flux
  binary that daniel-box lacks.
- R2-9 (medium): PR 7 breaks config-parsing tests the plan does not list, and overlaps R3
  on the same authelia template.
- R2-10 (medium): The `--secrets placeholder` default changes what R1's commands render.

Fixed, all ten:

- **R2-1.** Confirmed in `mosquitto/templates/config-secret.yaml.j2`. The Baseline now
  carries a full census of expressions on secret keys. Rule 5 is widened to
  `no-secret-expression`, a syntax-tree rule with an allowlist of identity-preserving forms
  and two persistent named members. PR 4 drops the split under the operator's digest check,
  and Open question 4 covers a prefixed stored hash. The census also showed that `~` and
  `to_json` over `domain` must be allowed, or HA would keep a violation that no PR removes.
  The mosquitto change joins PR 4 rather than a new PR, because R3 cites R2's PRs 6 and 7
  by number.
- **R2-2.** Open questions 5 and 6 name C1 and C3 with proposed wording and a merge gate.
  Open questions 7 and 8 replace the two "contract gap" sentences, each with a contract row,
  an owner and a resolution. Question 8 records that D2 needs R2, a missing dependency edge.
- **R2-3.** Confirmed: `render_record.yml` writes with `become: true` and reads a root-only
  key, and `load_secrets.yml` decrypts. The check is marked operator-only, and failure
  class 7 is in Risks. The finding's post-merge alternative would need a merge without
  `land.sh`, which has no merge-only option (`scripts/deploy_tools/land_lib/options.py`), so
  it is not used. An agent instead compares `secret_digest` in the release record with its
  `.previous.json` after the deploy, labelled as detection rather than prevention.
- **R2-4.** Confirmed: both fixtures call `core.sops_extract`. The Grafana test is
  operator-only and must show no skip. The agent's proof is equal `kid` and `n` in the
  served JWKS before and after.
- **R2-5.** `PLACEHOLDER_DEFERRED` holds `home-assistant` from PR 1, the first PR with
  placeholder mode, until PR 5. The summary line names it, and exit criterion 2 requires the
  set empty.
- **R2-6.** Confirmed in fluxcd/flux2 `cmd/flux/envsubst.go`, function `runEnvsubstCmd`.
  The round trip uses `flux build kustomization --dry-run --strict-substitute` rather than
  `kustomize build | flux envsubst`. Reading fluxcd/pkg `kustomize/kustomize_varsub.go`
  showed that dry run skips only the `substituteFrom` fetch and that this path calls the
  controller's own per-resource function. That removes both the line limit and the newline
  question. fluxcd/flux2 `internal/build/build.go`, function `maskSopsData`, masks only
  values holding `sops` and `ENC[`, so Secrets are still compared. The walk covers R1's
  three deferred directories. The class-2 mitigation points at D3's exit criterion 1 and
  D5's repeat.
- **R2-7.** Confirmed in fluxcd/pkg `envsubst/parse/scan.go`, function `scanEscaped`. Pass 2
  doubles every `$` before `$`, `{` or a sentinel. The sentinel case covers a `$` written
  before a secret reference, which a plain `$$` rewrite would mis-pair. No render input holds
  `$$` (Baseline), so the defect is latent. Open question 1 is answered from source, and
  Open question 5 carries the C1 wording.
- **R2-8.** The Flux behaviour red-proofs run in CI against the pinned binary. The fake
  `flux` tests only `roundtrip`'s own comparison. `roundtrip` is CI-only and exits 2 without
  a binary.
- **R2-9.** The `jwks[].key` expression sits inside the block scalar, so the config is valid
  YAML before Authelia's filter runs. The schema test resolves the expression with a fixture
  PEM to prove the shape after the filter. The finding counted five parsing tests. There are
  six: `test_authelia_argon2_parameters_agree.py` also parses the config through
  `_k8s_render.rendered_docs`. PR 7 lists all six and serialises its window with R3's
  *authelia's digests from SOPS* PR, which R3 has since renumbered from PR 3 to PR 4.
- **R2-10.** The `--secrets` flag is gone. `--secret-stubs FILE` selects stub mode, so R1's
  documented commands and its CI step render exactly what they did before.

Refuted: none.

### Integration round (cross-slice review)

Raised:

- X-01 (high): D2's publish job cannot render bento-pdf: no stub file, and no source for
  `domain` before D3.
- X-12 (medium): The `$${` escape rule is split: C1 is unconditional, H1 exempts tags
  without `postBuild`, and R2's CI and round trip assume `postBuild` everywhere.
- X-17 (medium): R4 PR 9 deletes `shadow_diff.py` and handles only its `stubs` CI call, not
  R2's `roundtrip` check.
- X-21 (medium): Many slices run `fact_status.py verify` and commit `docs/facts.lock`
  unconditionally, though G2 deletes both with no ordering against them.

Fixed, R2's part of all four. The parts that belong to other slices and to contracts.md are
handed over as cross-slice notes, and this log does not record them as done.

- **X-01.** Confirmed. `ansible/templates/ingressroute.yml.j2` builds the Host rule as
  `hostname ~ '.local.' ~ domain`, and `grep -c '^domain:' ansible/vars/secrets.yml` prints
  1. R2's part has three pieces:
    - `--placeholders` spells the default mode, so D2's publish command can name its mode.
      Placeholder mode stays the default. Making the flag mandatory would have broken the
      bare calls that D4, R4, D5 and H7 write, and the resolution of R1's Open question on
      C1 *Secret references*. A job that publishes the artifact writes the flag.
    - A `--role` render asserts only the named members of the tags it rendered, and checks
      only those tags' `KNOWN_VIOLATIONS` entries for staleness. Before this, a
      `--role bento-pdf` render failed on homepage's and authelia's named members, so D2's
      publish would have failed even with the edge in place.
    - Open question 8 now carries a PR-level edge (R2's PRs 1 and 2), the explicit publish
      command, and a pre-push coverage check that reads the key set. The coverage check
      replaces the finding's proposed check of the real domain, which no artifact check can
      perform. The `domain` source stays **operator-only**.
- **X-12.** Confirmed. *Which directories substitute* makes the placeholder key set of each
  Kustomization directory the one source. The escape, rules 1 and 2, `roundtrip`, D3's
  `postBuild` writer and its dotenv key-set check, and H1's vendored-file refusal all read
  it. A directory with an empty set gets no escape and no `postBuild`. A new CI fixture case
  and a red-proof cover it. Open question 5 merges the C1 wording with H1's Open
  question 2.
- **X-17.** Confirmed in R4's PR 9 and file tree. `roundtrip`'s logic moves to
  `renderer_lib/roundtrip.py`. *The round trip* states that its subject outlives Ansible
  and that it depends on the `stubs` writer and on stub mode. Open question 9 and a risk ask
  R4 to keep or move both and to edit the CI step in the same PR.
- **X-21.** Confirmed: G2's `lock-retired` rule fails while `docs/facts.lock` exists. The
  `fact_status.py verify` step now runs only until G2 retires the lock, in R4's wording.

Also corrected while editing: the Design said rule 2 catches a sentinel inside a
`b64encode`. A base64 sentinel leaves no textual fragment, so rule 5 catches it at its
source, and rule 2 is the backstop only for a sentinel that is partly altered.

Refuted: none.

### Round 10 (per-slice adversarial review)

Raised:

- R2-10-1 (high): R2's substitution predicate contradicts C1 as amended, Open question 5
  quotes C1 wording that no longer exists, and PR 1's merge gate cannot be met.
- R2-10-2 (medium): `--placeholders` is in neither C1 nor D2, and D2 states the opposite.
- R2-10-3 (medium): Open question 8 is stale: it proposes a `domain` source that C3 rejects,
  and its no-wait claim contradicts the index.
- R2-10-4 (medium): H1's request that rule 3 exempt the bucket keys for `kind:
  BackupTarget` is not acknowledged.
- R2-10-5 (medium): Placeholder mode, the only publishing mode, injects R1's pinned stub
  values for live-read facts, and nothing refuses to publish them.
- R2-10-6 (medium): Rule 5 cannot see a secret reached through `lookup('vars', <expr>)`,
  and neither backstop catches an identity-on-stub transform there.
- R2-10-7 (medium): Rule 5's 78-file floor counts text matches, not what the rule's
  syntax-tree walk counts.

Fixed, all seven:

- **R2-10-1.** Confirmed. C1 *Substitution and escaping* names a per-tag unit and
  `spec.postBuild` in the rendered Kustomization as the source. The sentence the old Open
  question 5 quoted, "A literal `${` in a template renders as `$${`", appears nowhere in
  contracts.md. The C1 source cannot serve R2's
  checks: R1 writes no `DIR/flux/`, D2 writes the first and depends on R2, and D2 emits a
  Kustomization only per `--role` tag. Open question 5 now quotes the two C1 sentences,
  proposes the per-directory unit with the in-process key set as the one source and
  `spec.postBuild` as its projection, adjusts C1 *Only placeholder mode publishes* to the
  same unit, and names H1 *One per-tag flag decides substitution* as the second text. PR 1's
  gate is now a check an executor can run against contracts.md. *Which directories
  substitute* states why the Flux file is a projection, not the source. The divergence is
  latent in output: `grep -c '\$'` over the six deferred templates prints 0 for each, and
  none names a key (Baseline).
- **R2-10-2.** Option (a). `--placeholders` is gone from the design, PR 1, the file tree,
  Verification, the docs list and the complexity delta. The call is C1 *Modes* as written,
  and D2's existing test, which asserts the publish step passes no `--secret-stubs`, is the
  stated mode. The refusal R2-10-5 asked for needs no flag (below).
- **R2-10-3.** Confirmed against C3 *The source of `domain` before D3* and the index rows
  for D2 and D3. Open question 8 cites C3's default, its operator-only alternative and the
  rejected hand-made Secret, and records D2's `homelab-identifiers` as a conflict for D2 and
  the contract maintainer. It asks for the sub-edge "D2 on R2.1 and R2.2" instead of an edge
  that exists, and it states that D2.7 still waits on all of R2 under C3's default.
- **R2-10-4.** Accepted, as Open question 10. A `(key, kind)` frozenset,
  `PLACEHOLDER_KIND_EXEMPT`, sits beside `PLACEHOLDER_ANYWHERE` with its own rule pair and
  named member. H1.7b adds it to `secret_checks.py`. Both keys exist:
  `grep -oE '^[a-z0-9_]*bucket[a-z0-9_]*:' ansible/vars/secrets.yml` prints
  `longhorn_b2_bucket:` and `r2_bucket:`. Exit criterion 2 now says "no ratchet entries",
  because rule 3's own allowances are part of the rule.
- **R2-10-5.** Confirmed: D2's `no-stub` gate checks only `stub-<key>`. The fix keys the
  refusal on `owner: flux` rather than on a publish flag, because the publish job renders
  only flux-owned tags. That also fails the ownership PR in CI, before any publish runs. The
  tags that read a pinned fact are derived by a Jinja parse, and `authelia`, `n8n` and
  `pi-peer-backup` are named members (Baseline grep). PR 2 carries it, with a red-proof
  pair, and Open question 11 asks R3 to delete it with the constant.
- **R2-10-6.** Confirmed: the three non-literal `vars` lookups are in
  `static-monitors.yaml.j2`, `env-secret.yaml.j2` and `prometheus.yaml.j2` (the Baseline
  parse prints 3). Rule 5 now
  treats a non-literal `lookup('vars', X)` as a possible secret reference held to the
  identity forms, checks a literal argument as that key, and allowlists the prometheus
  `replicas_var` site in `NON_SECRET_VARS_LOOKUPS`, whose entry asserts that no
  `replicas_var` names a secret key. Red-proofs flag `lookup('vars', t) | trim` and pass the
  bare form.
- **R2-10-7.** Confirmed with a different number. The finding estimated about 70 by a text
  match inside Jinja delimiters. A Jinja parse of all 294 templates finds 68 that name a key
  as a loaded variable or a literal `vars`-lookup argument, of which 9 are `*.sh.j2`. The
  floor is now 59 in-scope templates, with the parse command in the Baseline. Rule 5's scope
  excludes `*.sh.j2` and includes `Dockerfile*.j2`, which image-builder embeds into a
  cluster ConfigMap. `is_manifest_template` is not reused, because it answers which files
  parse as YAML.

Also corrected while editing: Open questions 6 and 7 asked for C3 wording that C3 *Secrets*
already carries, and PR 5's gate waited on them. Both are marked applied, and PR 5 has no
contract gate. *Multi-line keys become dedicated Secrets* now cites C1 and C3 as they stand.

Refuted: none.

### Round c1 (per-slice adversarial review)

Raised:

- R2-c1-1 (high): PR 1's merge gate waits on contract amendments that contracts.md does not
  track, and the contract log records the issue as closed.
- R2-c1-2 (medium): The pinned-fact refusal's named members and red-proof go red inside R3's
  PRs 1, 2 and 4, and the refusal derives the reader tags a second time instead of reading
  R1's.
- R2-c1-3 (medium): PR 4's operator digest gate fails on a correct PR, because the rendered
  qbittorrent comment that PR 4 makes false is part of the digested bytes.
- R2-c1-4 (medium): Rule 5 refuses the form the three dedicated Secret templates would
  naturally use, so their violations move rather than empty.
- R2-c1-5 (medium): Rule 5 does not read role defaults, so a transform or alias written in
  `defaults/main.yml` passes all three checks.

Fixed, all five:

- **R2-c1-1.** Confirmed. contracts.md *Requested amendments not yet applied* holds nine
  entries and none of R2's. C1 *Substitution and escaping* still names the per-tag unit and
  `spec.postBuild`, and the log closes X-12. This round cannot edit contracts.md.
    - Open questions 5 and 8 now say the requests are not yet recorded, and name the
      contracts.md maintainer as the one who records them. The cross-slice notes carry the
      four requests and the reopening of X-12.
    - Open question 5 no longer says that C1 *Modes* needs nothing. C1 says that stub mode
      "exists only for R1's shadow comparison", and `roundtrip` reads stub mode from PR 1.
      PR 1's merge gate now also requires the C1 *Modes* sentence.
    - One departure from the finding: the D2 sub-edge is requested with no R2 gate, because
      no R2 PR waits on it. Only D2's PRs gain from it.
- **R2-c1-2.** Confirmed. R3's plan names no refusal. R1 *Stub output is never published*
  already supplies the reader tags in `PINNED_LIVE_FACTS`, held to the templates by R1's
  test.
    - The refusal reads that set. Its named-member check asserts equality with the set, and
      non-emptiness while the constant holds a fact.
    - The red-proof pair runs over a fixture constant, fact and tag.
    - Open question 11 names R3's PRs 1, 2 and 4, which shrink the set, and PR 5, which
      deletes it. It covers both landing orders. A cross-slice note asks R3 to name the
      refusal in its PR 5.
- **R2-c1-3.** Confirmed. `ansible/roles/k8s/manifests/files/secret_hmac.py:digest` hashes
  the whole file. `qbittorrent/templates/secret.yaml.j2` and
  `pi-peer-backup/templates/secret.yaml.j2` both carry YAML `#` comments that render.
  mosquitto's split comment is a Jinja `{# ... #}` block, as the finding says.
    - PRs 3 and 4 now change no rendered comment.
    - PR 4b rewrites the two comments after PR 4, and a reviewer checks it from its diff.
    - Open question 4 checks the diff for a touched comment before anyone edits a value.
    - Verification, Rollback and the docs list state the same.
- **R2-c1-4.** Confirmed. Each dedicated template writes `{{ K | to_json }}`. A local check
  ran `yaml.safe_load` over `json.dumps` output for three values: a PEM-shaped key, a JSON
  document holding `\n` and a non-ASCII character, and a value with no final newline. Each
  came back unchanged, with and without `ensure_ascii`. Rule 5's clean case gains the form.
  A new risk records that `to_json` keeps a stored value's ending, where the `|` block
  scalar adds one newline.
- **R2-c1-5.** Confirmed as latent, with a measurement. A Jinja parse of the 68 files under
  `ansible/roles/k8s/*/defaults/main.yml`, `ansible/inventory/group_vars/` and
  `ansible/inventory/host_vars/` finds 184 Jinja strings. One names a key:
  `headlamp_k8s_oidc_issuer_url`, an interpolation of `domain` that its template uses bare.
  Rule 5 now parses those values, treats an alias as the key, and refuses a transform in
  either place. The headlamp alias is a named member, and an alias red-proof pair joins
  PR 2. PR 2's size and the complexity delta grow by about 20 lines.

Also corrected while editing: *The round trip*, Open question 9 and the matching risk said
that R4's PR 9 deletes `shadow_diff.py`. R4 now deletes only `compare` and keeps `stubs`,
`digest` and `roundtrip`, so R2's CI step needs no edit, and Open question 9 is answered.

Refuted: none.

### Round c2 (per-slice adversarial review)

Raised:

- R2-c2-1 (high): Rule 5's census misses 22 uses of a secret key as a filter argument, so
  exit criterion 2 cannot be met.
- R2-c2-2 (medium): Rule 5 does not track an alias bound by a for-loop target or by a
  `{% set %}` of an expression, though five such sites exist.
- R2-c2-3 (medium): PR 7 runs Authelia's Go-template filter over every real secret value in
  the config, and no check sees those values.

Fixed, all three:

- **R2-c2-1.** Confirmed. A parse of the 284 in-scope templates finds 21
  `homepage_href` arguments and one `authelia_service_rules` argument, and both filters only
  interpolate the value (`homepage_tiles.py:homepage_href`,
  `authelia_access.py:authelia_service_rules`). The finding's count of 285 templates is one
  high: 294 `.j2` files less ten `*.sh.j2` leaves 284.
    - Rule 5 gains the closed set `PASSTHROUGH_FILTERS`, a membership test per member, a
      red-proof pair and a failing fixture filter. The `homepage_href` calls and the
      `authelia_service_rules` loop are named members, accepted rather than entered.
    - The Baseline census is now the tree walk, with its script and its output: 323 uses,
      268 of them bare.
    - The set lands in PR 1b, ahead of PR 2. PR 2 also hands the defaults alias clause to
      PR 2b, so it shrinks from about 260 lines to about 240.
- **R2-c2-2.** Confirmed, with one correction: not every later use is bare or under
  `to_json`. A scope-free walk of every binding found `{% if tags %}`, a truthiness test on
  a list, and the attribute selections on authelia's `rule`. Rule 5 now tracks
  `{% set %}`, set blocks, `{% with %}`, loop targets and macro parameters. It splits
  aliases into value and container shapes, so those uses pass and a transform on either
  shape fails. The `static-monitors.yaml.j2` member is now named by construct, the bridge-row
  `tags` binding. The clause lands in PR 2b, which predicts no new entry. A new risk names
  the residual that a part of a container alias is untyped.
- **R2-c2-3.** Confirmed. `X_AUTHELIA_CONFIG_FILTERS=template` parses the whole file, and
  C3's value rule does not exclude `{`. PR 7 adds a count assert to the authelia role
  before the `k8s/manifests` include. The count is a fact set under `no_log`, and the
  assert prints only the number. The operator's pre-window dry run exercises it, and a
  risk, a red-proof and a test record it. The assert stops running once Flux renders
  authelia, so a cross-slice note asks C3 and D3 to have `write` refuse `{{`.

Refuted: none.

### Round c3 (per-slice adversarial review)

Raised:

- R2-c3-1 (critical): PR 7's dedicated authelia Secret is never rendered, because authelia
  passes explicit manifest lists, so the basename rule never applies and the Recreate
  rollout takes SSO down.
- R2-c3-2 (medium): PR 4 removes the only guard against a `user:`-prefixed MQTT hash,
  records the rule only in the role doc, and adds no check.
- R2-c3-3 (medium): `render_sops_manifests` is an unguarded exclusion from both the publish
  and the round trip.
- R2-c3-4 (medium): The alias clause's red-proofs cover two of the five binding forms it
  claims to track.
- R2-c3-5 (medium): Whether a `NON_SECRET_VARS_LOOKUPS` site makes its enclosing set block
  an alias is unspecified, and the prometheus block is then hashed.
- R2-c3-6 (medium): The round trip's stub values cannot show YAML type or quoting hazards,
  and the claim that Ansible has the same exposure is not shown for Flux.

Fixed, all six:

- **R2-c3-1.** Confirmed. The `k8s/manifests` include in
  `ansible/roles/k8s/authelia/tasks/main.yml` passes `manifests_files` and
  `manifests_secret_files` as Jinja expressions. The task "Resolve the manifest file lists"
  in `ansible/roles/k8s/manifests/tasks/main.yml` takes a passed list as given.
  `ansible/tests/k8s/test_derived_manifest_files.py:world_readable_secrets` skips a template
  outside the files list. The secret-key reconcile in `manifests/tasks/main.yml` and the
  digest loop in `manifests/tasks/release_digest.yml` both loop over
  `manifests_secret_files_resolved`.
    - PR 7 adds the file to the secret-list expression, never to `manifests_files`.
    - The *Multi-line keys* section and the file tree say that authelia lists the file
      explicitly.
    - A templar test in `ansible/tests/services/` pins the entry under both lever states.
    - An orphan test in `ansible/tests/k8s/` covers the three `JINJA_LIST_ROLES` roles. A
      listing of their templates against their include values shows it green at
      `70591c307`.
    - The operator's pre-window dry run reads `secret_manifests`, the field
      `ansible/roles/k8s/manifests/tasks/render_record.yml` writes.
    - Risks, Rollback, Verification and the complexity delta follow.
- **R2-c3-2.** Confirmed. The template comment, the `assisted` tier and the 2026-04-23 date
  are as the finding says.
    - PR 4 adds the rule to the `assisted` examples in `docs/secret-rotation.md`.
    - PR 4 adds a guard before mosquitto's include, with a templar test.
    - One departure from the finding: the guard counts the `:` in `mqtt_password_hash`
      itself and asserts 0, rather than counting the rendered line and asserting 1. That
      states the rule without assuming anything about `mqtt_username`, and it needs no
      template lookup.
    - A cross-slice note asks D3's `write` to refuse a `:` in that key.
- **R2-c3-3.** Confirmed. PR 5 adds the `sops-manifest-entries` check, and `roundtrip`
  asserts one excluded document per entry. One departure from the finding: plain equality
  between the entries and the templates that name a multi-line key would fail at PR 5,
  because pi-peer-backup's and authelia's templates still name their keys until PRs 6 and
  7. Until PR 9, each unlisted template that names such a key must carry a live
  `KNOWN_VIOLATIONS` entry, and from PR 9 the check is equality.
- **R2-c3-4.** Confirmed. A grep of the in-scope templates at `70591c307` finds five set
  blocks, all in `observability`, and no `{% with %}`. PR 2b gains the four fixture pairs
  the finding names, each loop closed with `{% endfor %}`.
- **R2-c3-5.** Confirmed. Each of the five observability set blocks was checked against
  the 184 secret key names in `secrets.yml`, and no block contains one. The prometheus
  block's only candidate reference is the non-literal `vars` lookup. Rule 5 now states that
  a `NON_SECRET_VARS_LOOKUPS` site is no secret reference for the alias clauses either. The
  block is a named member that rule 5 accepts, and a fixture pair joins the red-proofs.
- **R2-c3-6.** Confirmed as unverified. No one in this round can run the pinned CLI, so
  the plan states no result.
    - PR 1's CI fixture gains `"${n}"` with `n=0123` and with `n=true`, and records whether
      each result is a string.
    - The case then asserts the measured result, so a later CLI pin bump that changes it
      goes red. If the quotes are lost, PR 1 ships a second round-trip pass with
      type-hostile stubs. The plan names that remedy and rejects a C3 value rule, which
      would refuse the all-digit `mullvad_account`.
    - The "same exposure" sentence now covers only quote, backslash and control
      characters. Ansible has no type hazard, because Jinja writes the template's quotes
      into the applied text. A new risk and Open question 7 carry the type question.

Also corrected while editing: the `jwks` block, and so the key, sits behind
`authelia_k8s_manage_oidc`. Its defaults say a cluster with the lever off holds no OIDC
credential. The new list entry, the Deployment's volume, mount and filter variable now ride
that lever. PR 7's count assert would have failed every deploy with the lever off, because
the config then holds no `{{`. Both count-assert tasks now run only with the lever on, and
the red-proof gains the lever-off case.

Refuted: none.

### Round integration-2 (cross-slice review)

Raised against R2, among other targets:

- X2-03 (high): D3's `cluster_vars.py write` lacks the successor refusals R2 and R3 rely on
  once Flux renders authelia and mosquitto.
- X2-09 (medium): Substitution is decided per tag in C1 and H1 but per directory in R2 and
  D3, and X-12 is recorded as fixed.
- X2-11 (medium): The register and slice index lack about 20 requested rows and sub-edges
  that slices name as merge gates.

Fixed, all three, for R2's part:

- **X2-03.** Confirmed for R2. PR 4's colon-count assert and PR 7's `{{` count assert are
  tasks of their roles, so each stops running at D5's flip PR for that role. R2 had only
  asked, in a risk and a review-log line, for D3 to refuse the value. It named no
  precondition for the flip. D3 has taken up its part in this round. Its plan refuses `{{`
  in any referenced value from D3.2a. D3.2d adds the `:` refusal for `mqtt_password_hash`
  and R3's argon2 refusal, each with a fixture test.
    - PR 4 and PR 7 each gain a bullet that names D3's refusal as the assert's successor
      and D5's flip PR as the point where the assert stops.
    - The two risks name the successor, and the gap that a flip ahead of D3.2d would open.
    - New Open question 12 records what is still open. C3's value rule gains `{{`. D5's
      own text makes the authelia and mosquitto flip PRs wait on D3.2d. The PR that deletes
      each assert task names the successor.
    - Open question 7 no longer says that R2 adds nothing to the character rule.
    - The complexity delta lists the refusals as handed over, and D3 counts them.
    - A refusal of `{{` in any value never meets a value the automatic rotation writes,
      because `scripts/secrets_mgmt/secret_rotation.py:cmd_rotate` draws new values with
      `token_hex`.
- **X2-09.** Confirmed. contracts.md C1 *Substitution and escaping* still reads "A tag
  *substitutes* when its render holds at least one placeholder". H1 still has "One per-tag
  flag decides substitution", and the contracts.md review log still closes X-12. R2 also
  disagreed with itself: Open question 5 gates all four amendments before PR 1, but PR 1's
  merge gate checked only two. The gate now checks all four, each with a `grep -c` that an
  executor can run. The contracts.md and H1 edits go to cross-slice notes.
- **X2-11.** Confirmed for R2's rows. *Requested amendments not yet applied* holds nine
  entries. None of them is one of R2's four C1 and H1 rows or the D2 sub-edge on R2's PRs 1
  and 2. R2 already asks for both (Open questions 5 and 8). The fix in R2 is the PR 1 gate
  above. The rows go to contracts.md as a cross-slice note.

Refuted: none.
