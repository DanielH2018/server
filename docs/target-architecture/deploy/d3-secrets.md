# D3 — Secrets through Flux

Status: PLANNED

Depends on: [D2](d2-first-leaf.md), [R2](../render/r2-secret-placeholders.md). D3.0 to D3.3c
need only D2.1 to D2.6c, and D3.3a also needs D4.3a, as the slice index says. D3.4 to D3.9
need all of D2.

Unblocks: [D5](d5-bulk-move.md), and PR D2.7 of [D2](d2-first-leaf.md), which waits on D3.3b
under C3's default source for `domain`.

## Goal and exit criterion

After D3, Flux renders a flux-owned role's Secrets by substituting values from one Secret,
`homelab-cluster-vars`. kustomize-controller builds that Secret by decrypting
`ansible/vars/cluster-vars.sops.env` with an age key that only the cluster holds. CI holds no
key, yet proves the file is in step with `ansible/vars/secrets.yml`. A changed secret value
restarts the pods that read it, through a checksum of ciphertext, never of plaintext. `peanut`
is the first role with a Secret that Flux owns. Every routed flux-owned tag substitutes
`${domain}` from the same Secret. Under C3's default, D2.7 flips bento-pdf only after D3.3b,
so no tag ever substitutes from another source. R2's three SOPS-encrypted manifests get their
delivery path, which D5 uses when their roles move.

**Exit criterion.** Five checks, each under *Verification*:

1. **peanut runs on substituted values.** Flux owns `peanut-secrets`, `probe.py health peanut`
   passes, and the PeaNUT page renders through Traefik. An **operator-only** comparison of
   value hashes prints `match` for each of its three keys.
2. **A dropped key disappears live.** A literal canary key added to `peanut-secrets` in one PR
   is absent from the live Secret after the next PR removes it. The operator lists key names
   only (**operator-only**).
3. **A rotation restarts the reader.** A changed `peanut_password` reaches the live Secret,
   and peanut's pod restarts in the same revision.
4. **CI refuses drift.** A PR that changes a cluster-referenced key in `secrets.yml` without
   regenerating the cluster file fails `cluster_vars.py check`.
5. **D2's leaf substitutes from this Secret.** After D2.7 settles, bento-pdf's Kustomization
   substitutes from `homelab-cluster-vars` and depends on `cluster-vars`, and the bento-pdf
   page loads through Traefik.

## Baseline at `70591c307`

- **`secrets.yml` is flat.** It holds 184 top-level keys, each one `ENC[...]` scalar. Key
  names are plaintext. `grep -c '^[A-Za-z0-9_]*: ENC\[' ansible/vars/secrets.yml` prints
  184, and the only indented lines belong to the `sops:` metadata block.
- **A value's ciphertext moves only when the value or the data key changes.** This command
  prints line counts and never decrypts:

  ```bash
  git log --no-textconv --numstat --format='%h %ad' --date=short -20 -- ansible/vars/secrets.yml
  ```

  Of the last 20 commits that touched the file, 19 changed 4 to 6 lines: one value, `mac` and
  `lastmodified`. One, `1eb3891b5` (2026-10-03), changed 197 and removed 206: the Pi's
  recipient removal with `sops rotate`, which `ansible/.sops.yaml` describes. This answers
  the design's open question 4. A per-key ciphertext hash over-triggers only on a data-key
  rotation. A whole-file hash moves on every commit, because `mac` and `lastmodified` always
  change.
- **`.sops.yaml` has one creation rule and a one-rule runbook.** The rule matches
  `.*(vars|secrets)\/.*\.ya?ml$` and lists three recipients: daniel-server, the off-box
  recovery key and daniel-box. It does not match a `.env` file. The file's header tells the
  operator to append a host's key to the one `age:` string and run `sops updatekeys` on
  `secrets.yml`, and to remove one with `sops rotate -i --rm-age`. The *Notable* section of
  `ansible/roles/setup/sops_setup/CLAUDE.md` repeats the one-file onboarding. The header also
  states that past revisions stay readable with a removed key.
- **SOPS finds `.sops.yaml` from the working directory, not from the file.** Read through
  `gh` at getsops/sops v3.13.3: `findConfigFile` in `cmd/sops/main.go` calls
  `config.LookupConfigFile(".")`, which searches upward from the working directory.
  `parseCreationRuleForFile` in `config/config.go` strips the config file's directory from
  the file's path, matches each `path_regex` in order, and takes the first match. `git
  ls-files` lists only `ansible/.sops.yaml`, and the *Notable* section of
  `sops_setup/CLAUDE.md` says to run `sops updatekeys` from `ansible/`. An encrypt run from
  the repo root therefore finds no creation rule. `sops set` on an existing file reads that
  file's own metadata, so it works from any directory. The global `--config` flag names the
  file explicitly.
- **SOPS rewrites the MAC and `lastmodified` on every write.** `EncryptTree` in
  `cmd/sops/common/common.go` sets `LastModified` to the current time and encrypts the MAC
  with it. A hand edit of a value line leaves both lines unchanged.
- **The SOPS dotenv store writes its metadata as plain `key=value` lines.** `SopsPrefix` in
  `stores/metadata.go` is `sops_`, and `EmitPlainFile` in `stores/dotenv/store.go` writes
  each entry unquoted. The MAC line of a SOPS dotenv file therefore reads `sops_mac=ENC[...`.
- **The protected-edits hook recognises a SOPS file by its YAML or JSON MAC line.**
  `.claude/hooks/block-protected-edits.py:is_sops_encrypted` matches `_SOPS_MAC_RE`, a `mac`
  key whose value starts `ENC[AES256_GCM,`, and the hook denies an Edit or Write to such a
  file. `.claude/hooks/block-protected-bash.py` loads `classify` from that file through
  `_load_edits_hook`, so the Bash arm uses the same detector. Their tests are
  `.claude/hooks/tests/test_block_protected_edits.py` and
  `.claude/hooks/tests/test_block_protected_bash.py`. The `.gitleaks.toml` comment rests the
  `secrets.yml` allowlist on this hook and on `.sops.yaml`.
- **sops also reads the host's default age key.** In `age/keysource.go`, `loadIdentities`
  collects identities from `SOPS_AGE_KEY`, `SOPS_AGE_KEY_FILE` and `SOPS_AGE_KEY_CMD`. It
  then also opens `<UserConfigDir>/sops/age/keys.txt` whenever that file exists. On Linux,
  `os.UserConfigDir` returns `XDG_CONFIG_HOME` when it is set. Each host keeps its key at
  `~/.config/sops/age/keys.txt` (`sops_setup/CLAUDE.md`).
- **What kustomize-controller v1.9.6 can decrypt**, read from
  `internal/decryptor/decryptor.go` through `gh`:
    - `DecryptSources` decrypts the `files` and `envs` sources of a kustomize
      `secretGenerator`. The format follows the file's extension, and an `envs` source with
      an unknown extension is read as dotenv. It checks the SOPS MAC.
    - `DecryptResource` decrypts a Secret manifest that SOPS encrypted as a whole.
    - `Decryptor.ImportKeys` imports every data entry of the decryption Secret whose name
      ends in `.agekey`. One `sops-age` Secret can therefore hold two age keys while the
      cluster key is rotated.
    - `secrets.yml` used as an `envs` source would decrypt as YAML, and kustomize would then
      parse YAML lines as dotenv, so it cannot be the substitution source as it stands.
- **A SOPS Secret needs `decryption`, and it is substituted after decryption.** In
  kustomize-controller v1.9.6 `internal/controller/kustomization_controller.go`,
  `KustomizationReconciler.build` runs `DecryptResource` on each resource when
  `spec.decryption` is set, then `SubstituteVariables` when `spec.postBuild` is set. Its
  `apply` refuses a Secret that `decryptor.IsEncryptedSecret` still matches, with the error
  that configuring decryption is required.
  fluxcd/pkg `kustomize/kustomize_varsub.go`, at the `kustomize/v1.35.7` tag that controller
  pins, skips a resource whose label or annotation `kustomize.toolkit.fluxcd.io/substitute`
  equals `DisabledValue`, `disabled`.
- **Strict substitution is on by default.** `internal/features/features.go` in
  kustomize-controller v1.9.6 sets `StrictPostBuildSubstitutions: true` ("opt-out from
  v1.9"). An undefined `${var}` with no default fails the build. The survey's premise that
  Flux substitutes an empty string does not hold for this version, which answers R2's open
  question 2 from source.
- **`postBuild.substituteFrom` reads a Secret in the Kustomization's own namespace**, which
  is `flux-system` (C3).
- **Every route carries `domain`, a SOPS key.** `grep -c '^domain: ENC\['` and
  `grep -c '^email: ENC\['` on `secrets.yml` each print 1. The `ingressroute` macro in
  `ansible/templates/ingressroute.yml.j2` builds each Host rule as `hostname ~ '.local.' ~
  domain`. bento-pdf and texbrain take their route from
  `ansible/templates/ingressroute-default.yaml.j2`, and peanut's own
  `templates/ingressroute.yaml.j2` imports the same macro. Under R2's placeholder mode each
  renders `${domain}`, which R2's `PLACEHOLDER_ANYWHERE` allows outside a Secret. Under C3's
  default, D2.7 waits on D3.3b, so no flux-owned tag substitutes before
  `homelab-cluster-vars` exists. D2 has withdrawn the Secret `homelab-identifiers` that its
  D2.6a once added, so no other source of `domain` exists to retire.
- **The two leaves read no configuration.** A search for `configMap`, `secret` and `envFrom`
  in bento-pdf's and texbrain's `templates/deployment.yaml.j2` finds nothing. peanut's names
  `peanut-secrets` twice through `secretKeyRef` and once as a volume's `secretName`.
- **Every peanut key has a second reader.** `peanut_username` and `peanut_password` also
  render into homepage's PeaNUT widget, in
  `ansible/roles/k8s/homepage/templates/config/services.yaml.j2`. `nut_ha_password` also
  renders into `ansible/roles/k8s/nut/templates/config-secret.yaml.j2`.
- **The shared role restarts two kinds of target.** In
  `ansible/roles/k8s/manifests/tasks/main.yml`, the task "Roll the deployment after a config
  change" restarts the `manifests_rollout` workload, and "Roll the extra deployments after a
  config or image change" restarts each `manifests_extra_rollouts` entry. The comment above
  `manifests_rolled_by_apply` says a `manifests_self_rollouts` entry gets "the owning role's
  private restart" instead.
- **Ten roles pass an empty `manifests_rollout`.** A search of
  `ansible/roles/k8s/*/tasks/*.yml` for `manifests_rollout: ''` or `""` finds cloudflare-ddns,
  configarr, deploy-ui, dri-device-plugin, longhorn-ui, media-volume, netpol-baseline,
  observability, pi-peer-backup and pihole. Of those, cloudflare-ddns names two
  `manifests_extra_rollouts`, and only pihole and observability pass
  `manifests_self_rollouts`. The other seven name no restart target, so Ansible restarts
  nothing for them on a config change.
- **Values already survive raw embedding.** Secret templates put values in double quotes, as
  `ansible/roles/k8s/speedtest/templates/secret.yaml.j2` does for `APP_KEY`. Ansible embeds
  each value raw today, so every live value is valid in its context. That does not exclude a
  backslash or a double quote inside a block scalar or a config file. R2 names value encoding
  as D3's job.
- **Stale keys need a manual patch today.** `ansible/roles/k8s/manifests/tasks/verify_secret_keys.yml`
  exists because a client-side apply cannot remove a map key. Flux's server-side apply
  removes the keys it no longer applies (survey appendix, claim 25).
- **The read-only ServiceAccount cannot read Secrets.** Every Secret read below is
  **operator-only**, through `sudo k3s kubectl`, and prints key names, manager names or
  booleans, never values.
- **The weekly rotation deploys before its PR exists.**
  `ansible/roles/setup/initial_setup/templates/secret-rotate.sh.j2` runs
  `secret_rotation.py rotate --commit --deploy` on Sunday at 09:00, in the primary checkout
  (`REPO=/home/{{ sys_user }}/server`, then `cd "$REPO"`). It commits its `TRACKED`
  paths, `ansible/vars/secrets.yml` and `ansible/secret_rotation.yml`, with `--no-verify`,
  and opens a PR on which CI still runs. Its `revert()` checks out only `TRACKED`, and it
  aborts a run while a previous rotation PR is open. `--deploy` redeploys the tags that
  `scripts/secrets_mgmt/consumers.py:consumer_tags` returns, before the PR exists. Auto-tier
  keys feed cluster renders: `ansible/secret_rotation.yml` marks
  `cloudflare_ddns_direct_push_token` and `arr_autoblock_push_token` `tier: auto`, and
  cloudflare-ddns's `templates/secret.yaml.j2` and autofix-bridge's
  `templates/env-secret.yaml.j2` reference them.
- **`rotate --commit` deploys last, and a value already live is never reverted.** In
  `scripts/secrets_mgmt/secret_rotation.py:cmd_rotate`, `--commit` writes each value with
  `rotation_tools.py:sops_set`, which sends it on stdin with `--value-stdin`. It then saves
  the registry, and only then deploys. A `sops_set` failure returns before the deploy. The
  cron's comment on its commit guard says that a value already live must not be reverted.
- **A rotation maps to no deploy tag.** `scripts/deploy_tools/land_lib/land_tags.py:_ROTATION_NOTE`
  says so, and sends the operator to `scripts/secrets_mgmt/consumers.py:consumer_commands`.
  That function prints one `./scripts/deploy.sh --tags "<tags>"` line over every k8s
  consumer. `land_tags.py` carries the marker `DECIDED: fire on ANY change to secrets.yml,
  and never try to name which keys moved`. In
  `ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py:services_from_changed_paths`,
  `secrets.yml` sets only `cs.secrets`, and another path under `ansible/vars/` matches no
  plane and adds nothing.
- **The rotation registry holds only `secrets.yml` keys.** `secret_registry.py:registry_drift`
  reports a registry row with no matching key in `secrets.yml` as stale, and
  `secret_rotation.py audit --check` fails on a stale row. An age key is not a `secrets.yml`
  key, so the registry cannot track one.
- **gitleaks allowlists one SOPS file and two digest files.** `.gitleaks.toml` extends the
  default rules with a path-scoped allowlist for `ansible/vars/secrets\.yml`, whose comment
  says the default rules false-positive on ciphertext beside a suggestive key name. It
  scopes `generic-api-key` off `docs/facts.lock`, whose comment records that rule firing on a
  64-hex digest beside a name about secrets.
- **ansible-lint lints every tracked YAML file under `ansible/`.** The `ansible-lint` hook in
  `prek.toml` sets `files = "^ansible/.*\\.ya?ml$"` and excludes only `secrets.yml` and
  home-assistant's `files/`. Its comment says `.ansible-lint`'s `exclude_paths` are not
  honoured for files passed by name. ansible-lint reads a YAML file under a `vars/` directory
  as a vars file.
- **CI installs neither `sops` nor `age`.** `grep -rln -i 'sops\|age-keygen' .github/`
  matches only `.github/copilot-instructions.md`. The hosts' pin is
  `ansible/roles/setup/sops_setup/defaults/main.yml:sops_setup_version` (`v3.13.3`), with
  `sops_setup_binary_amd64_sha256` beside it.
- **peanut, the first role with a Secret.**
    - The role has one task, an include of `k8s/manifests` with `manifests_rollout: peanut`.
    - It is `RollingUpdate` with no PVC and `k8s_autodeploy: true`, behind Authelia at
      `one_factor`.
    - `templates/secret.yaml.j2` renders `peanut-secrets` with three secret references:
      `peanut_username`, `peanut_password` and `nut_ha_password`. The pod reads the first two
      through `secretKeyRef` and mounts `settings.yml`.
- **R2 hands D3 three documents.** Each is listed in a role's `render_sops_manifests` and
  holds one key from `SOPS_MANIFEST_KEYS`: `pi_peer_backup_ssh_key`,
  `authelia_oidc_rsa_key_content` and `google_assistant_service_account`. Placeholder mode
  emits no document for them, and stub mode still renders them. D3 supplies each as a
  SOPS-encrypted manifest. R2's round trip excludes these three.
- **The Ansible prune list names no Secret.**
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist` holds no
  `core/v1/Secret`, so dropping a Secret's template leaves the live Secret in place.
- **Plans D3 builds on**, not the tree:
    - D1's Alert `commit-status` sends every Kustomization event to the GitHub provider, with
      context `flux/<name>`, so any Kustomization in the bundle posts its own status.
    - The publish census admits the flux-owned tags plus the members of `SHARED_KINDS`, a
      closed literal set that ships empty with a reserved-name check. C3 and the slice index
      put that in D4.3a. D2's plan puts the same in D2.6b. D4.3 adds the rule for the
      documents a tag's file holds.
    - D2.6b writes `postBuild.substituteFrom` naming `homelab-cluster-vars` on each tag
      whose render holds a placeholder. D2 states that unit per tag, after C1, and D3.3b
      reads it per directory, as R2 does. D2.6c's source gate in `promote.py check-bundle`
      admits that Secret only when a Kustomization in the same bundle generates it. D2
      states that the gate does not check which keys the Secret holds, because R2's rules 1,
      2 and 4 and D3's `check` already cover it.
    - D2's publish step writes the root alone when no tag is flux-owned, because a `cluster`
      call without `--role` renders every tag.
    - D2.3's `land-wait` waits on the Kustomizations of the PR's flux-owned tags (C6).

## Design

### Where the values come from

```text
operator (holds an age key)                      CI (holds no key)
  cluster_vars.py write  ──writes──▶  ansible/vars/cluster-vars.sops.env    (dotenv, SOPS)
                                      ansible/vars/cluster-vars.source      (plaintext hashes)
                                      ansible/vars/cluster-manifests/*.sops.yaml
                                                │
                     cluster_vars.py check ◀────┘  key set and freshness, against secrets.yml
publish job ─▶ homelab_render.py cluster, placeholder mode (no --secret-stubs)
            ─▶ out/flux/cluster-vars.yaml
               out/cluster/cluster-vars/{kustomization.yaml, cluster-vars.sops.env}
cluster:  Kustomization cluster-vars (decryption: sops, secretRef sops-age)
            secretGenerator ─▶ Secret flux-system/homelab-cluster-vars
          Kustomization peanut, bento-pdf, ...  dependsOn cluster-vars
            postBuild.substituteFrom homelab-cluster-vars ─▶ Secret homelab/peanut-secrets,
                                                             IngressRoute Host rules
```

- **A cluster-only age key.** The operator generates a dedicated age keypair. The public half
  joins two new creation rules in `ansible/.sops.yaml`, for the cluster file and the
  cluster manifests only, so the cluster key never decrypts `secrets.yml`. The private half
  lives in the operator's password manager, beside the off-box recovery key, and in the
  Secret `flux-system/sops-age`. Both steps are **operator-only**.
- **Three creation rules list the host recipients.** Each new rule lists the cluster key and
  the three existing recipients, so the operator can regenerate the cluster files on any
  host that decrypts today. Adding or removing a host therefore edits all three rules. It
  runs `sops updatekeys`, or `sops rotate -i --rm-age <key>`, on `secrets.yml`, the cluster
  file and each cluster manifest, from `ansible/` or with `--config ansible/.sops.yaml`.
  D3.1 rewrites the `.sops.yaml` header and the *Notable* section of `sops_setup/CLAUDE.md`
  to say so, and `check` refuses drift between the rules.
- **The two cluster rules match from any working directory.** Their `path_regex` values are
  unanchored at the start, like the generic rule: `vars/cluster-vars\.sops\.env$` and
  `vars/cluster-manifests/[^/]+\.sops\.yaml$`. Each matches the path relative to `ansible/`,
  the path relative to the repo root, the absolute path, and the `--filename-override` path
  that `write` passes. Both sit ahead of the generic rule, because SOPS takes the first rule
  that matches and the generic rule also matches every `*.sops.yaml` manifest. It does not
  match the `.env` file.
- **Bootstrap material is `sops-age` alone.** No template ever renders it, and a rebuilt
  cluster gets it by hand (**operator-only**). `homelab-cluster-vars` is never bootstrap
  material. The `cluster-vars` Kustomization's `secretGenerator` produces it from the
  artifact on the first reconcile after `sops-age` exists. A hand-made copy would be a second
  writer that Flux overwrites, so no runbook creates one.
- **`cluster-vars.sops.env`** holds exactly the keys that the cluster render references as
  `${key}`, across all 56 tags and not only the flux-owned ones, less `SOPS_MANIFEST_KEYS`.
  That set includes `domain` and `email`. Each bulk-move PR in D5 therefore needs no
  regeneration.
- **`cluster-vars.source`** is the freshness evidence. It holds one line per key in the
  cluster file or the cluster manifests: the key name and the SHA-256 of that key's
  `ENC[...]` string in `secrets.yml`. The ciphertext is public, so the hash exposes nothing.
  The two files cannot be compared directly, because each has its own data key.
- **`cluster_vars.py check`** runs in CI and reads no plaintext. It asserts:
    - the cluster file exists;
    - the dotenv's key names, less SOPS's own `sops_*` metadata lines, equal the placeholder
      set that R2's checks compute, less `SOPS_MANIFEST_KEYS`;
    - every `.source` hash matches the key's ciphertext in `secrets.yml`;
    - `.source` covers every key in the dotenv and every manifest;
    - every value in the dotenv, less the plaintext `sops_*` metadata lines, and every
      `data` or `stringData` value in a manifest is an `ENC[` string;
    - each cluster file that differs from its base version outside its SOPS metadata also
      differs in its MAC line and its `lastmodified` line: `sops_mac` and
      `sops_lastmodified` in the dotenv, `mac` and `lastmodified` under `sops:` in a
      manifest. SOPS rewrites both on every write, and a hand edit leaves both unchanged.
      This stands in for "the file decrypts", which CI cannot test without a key. It misses
      a hand edit that also replaces both lines, so the decrypts in D3.3a's precondition
      and in `cluster-vars` stay the real test. The base is the merge base of `HEAD` with
      `origin/master`, the rule `scripts/dev/pr_changed_files.sh` applies in CI, and the
      pre-commit hook and `rotate` pass `--base HEAD`. A file absent from the base is exempt;
    - each file's recipient set in its `sops` metadata equals the full `age:` list of the
      rule that SOPS itself picks: the file's path relative to `ansible/`, matched against
      the rules in order, first match wins. The check reads the rules, never a constant, so
      a key rotation needs no code change;
    - one manifest exists per `render_sops_manifests` entry;
    - each manifest carries `kustomize.toolkit.fluxcd.io/substitute: disabled` in its
      plaintext metadata;
    - each manifest's plaintext structure equals the stub-mode render of its template:
      `apiVersion`, `kind`, `metadata` less that annotation, `type`, and the data-key names.
      Placeholder mode renders no document for these templates, and stub mode does (R2).

  Every failure message names the operator's `cluster_vars.py write` as the only fix, and
  says that a hand edit of a cluster file breaks its MAC. Two narrower modes share the code.
  `check --no-render` runs the assertions that need no render: `ENC[`, the MAC proxy, the
  `.source` hashes and coverage, and the recipients. `check --ciphertext-only <file>...` runs
  the `ENC[` assertion alone over the SOPS files it names. In the publish job those are the
  dotenv copy and each SOPS manifest copy under `out/cluster/`, the files the allowlist
  names. The rest of `out/cluster/` is plaintext by design, such as `kustomization.yaml` and
  the placeholder Secrets, so the mode never takes a whole directory.
- **`cluster_vars.py write`** is **operator-only**, because it decrypts. No plaintext value
  reaches the disk.
    - It decrypts `secrets.yml` and the existing cluster file into memory, from `sops
      decrypt` on stdout.
    - It refuses a referenced value that holds a newline, a double quote, a backslash, a
      control character or the two-character run `{{`, naming the key and never the value.
      The first four make today's implicit contract explicit. `{{` would open a second Go
      template action in Authelia's configuration once R2's PR 7 enables its template
      filter, which parses the whole file. R2's deploy-time count of `{{` stops running when
      D5 moves authelia to Flux, so `write` is its successor. A single `{` passes.
    - D3.2d adds two per-key refusals, each naming the key and never the value. Each is the
      successor of a deploy-time assert that stops running when D5 moves its role to Flux:
        - Each of R3's three `_argon2` keys that `write` places in the cluster file or a
          cluster manifest must match R3's PHC pattern,
          `^\$argon2id\$v=19\$m=65536,t=3,p=4\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$`, and
          differ from every other `_argon2` value it places. That is R3's "Fail if a
          stored Authelia digest is malformed" assert. authelia's `config-secret.yaml.j2`
          names the claude-ui and claude-agent digests only behind `authelia_k8s_manage_claude_user` and
          `authelia_k8s_manage_agent_user`, so `write` checks the users the assert checks.
          The three parameters live in one constant in `cluster_vars.py`, which joins
          `ansible/tests/services/test_authelia_argon2_parameters_agree.py` as a named
          member, compared with the rendered hashing block's `memory`, `iterations` and
          `parallelism`.
        - `mqtt_password_hash` must hold no `:`. That is R2's PR 4 colon-count assert, which
          stops a stored `mosquitto_passwd` line, `username:hash`, from rendering as
          `user:user:$7$...`.
    - Every refusal runs over every referenced value in memory before the first `sops set`,
      not only over changed values, so a refusal adds and changes no file. A value set in
      `secrets.yml` by any other route reaches a cluster file only through `write`, because
      `check` fails the stale `.source` hash until `write` runs. Open question 7 says what
      follows a refusal.
    - It runs `sops set --value-stdin` only for a key whose value changed, through
      `rotation_tools.py:sops_set`, which gains a path argument that defaults to
      `secrets.yml`. It runs `sops unset` for a key no longer referenced. Every other key
      keeps its ciphertext.
    - It regenerates a manifest when its key changed, or when its structure no longer
      matches the template's stub render. It builds the manifest in memory with
      `kustomize.toolkit.fluxcd.io/substitute: disabled` in its metadata, and pipes it to
      `sops encrypt --filename-override <target> --config <repo>/ansible/.sops.yaml`. On
      exit 0 it writes the ciphertext from stdout onto the target path. It never writes a
      temporary file beside the target, so a crash leaves no untracked file, and in the
      rotation cron every target is a tracked path that `revert()` restores. SOPS's MAC
      covers the unencrypted fields too, because this plan does not set
      `mac_only_encrypted`, so nothing may edit the manifest after encryption.
    - It encrypts the first cluster file, in D3.2c, the same way: from stdin, with
      `--filename-override` and `--config`.
    - It passes `--config` on every call that resolves a creation rule, so its output does
      not depend on the working directory. The rotation cron runs it from the repo root.
    - It rewrites `.source` and prints key names only.
- **The protected-edits hook learns the dotenv form.** D3.2a adds a second pattern,
  `(?m)^sops_mac=ENC\[AES256_GCM,`, to `is_sops_encrypted`, ahead of the first cluster file.
  The Bash arm loads the same `classify`, so one change covers both tool surfaces.
- **A pre-commit hook runs the checks that need no render.** D3.2a adds a local hook,
  `cluster-vars-ciphertext`, to `prek.toml`. It matches `^ansible/vars/cluster-` and runs
  `uv run python scripts/secrets_mgmt/cluster_vars.py check --no-render --base HEAD`. A
  plaintext value or a hand-edited cluster file then fails before the commit, not after a
  push. CI's full `check` stays the backstop for a commit made without hooks.
- **gitleaks.** D3.2a extends `.gitleaks.toml` in two ways, in the same PR as the guards
  above.
    - The path-scoped allowlist gains the cluster file, the cluster manifests and the publish
      job's copies of both under `out/cluster/`. The comment above it is rewritten to name,
      for each path, the guards that keep plaintext out of it:
        - `secrets.yml`: the protected-edits hook. A creation rule applies only when `sops`
          itself encrypts, so the rewrite drops the claim that `.sops.yaml` auto-encrypts
          everything in `vars/`;
        - the cluster file and manifests: the protected-edits hook with its dotenv pattern,
          the `cluster-vars-ciphertext` pre-commit hook, and `check` in CI;
        - the copies under `out/cluster/`: `check --ciphertext-only` on those copies in the
          publish job, which runs before its gitleaks step. That step must load the repo's
          `.gitleaks.toml`, or the allowlist does not apply to `out/`.
    - `ansible/vars/cluster-vars\.source` gets an allowlist scoped to `generic-api-key`, with
      a comment in the form of the `docs/facts.lock` entry. A `.source` line pairs a key name
      such as `grafana_admin_password` with a 64-hex digest, the shape that rule fired on.
- **Lint.** The cluster manifests sit under a `vars/` directory, where ansible-lint reads
  them as vars files and fails on `apiVersion`. They stay under `ansible/vars/`, because the
  creation rules in `ansible/.sops.yaml` and the runbooks point there. D3.2a adds
  `vars/cluster-manifests/` to the `ansible-lint` hook's `exclude` regex in `prek.toml` and
  to `.ansible-lint`'s `exclude_paths`, each with a comment giving that reason.
- **CI gets the binaries.** D3.2a adds a step to CI's `pytest` job that installs `sops` at
  `sops_setup_version`, verified against `sops_setup_binary_amd64_sha256`, and installs
  `age`. A test in `ansible/tests/repo/` holds the step's version and checksum equal to the
  `sops_setup` defaults. The `write` tests fail, not skip, when either binary is missing.
- **R2's manifests.** `write` produces each one under `ansible/vars/cluster-manifests/`,
  SOPS-encrypted as a whole with `encrypted_regex: ^(data|stringData)$`. The renderer copies
  it byte for byte into its role's `cluster/<tag>/`, refuses one that lacks the
  substitute-disabled annotation, and sets `decryption` on that role's Kustomization. C3
  lets a role vary three fields, and `decryption` is not one of them (Open question 6).
  Nothing uses this until pi-peer-backup, authelia and home-assistant move in D5.
- **The rotation cron keeps the file in step, from before the file exists.** D3.2b makes
  `secret_rotation.py:cmd_rotate` call `write` in process when a rotated key is
  cluster-referenced and the cluster file exists. It is a no-op while the file is absent.
    - The call sits after `save_registry` and before the deploy, and `check --no-render
      --base HEAD` runs in process right after it. A failure of either returns non-zero
      before anything is deployed, as a `sops_set` failure does. The cron's existing
      `revert` and alert path then runs while no rotated value is live.
    - The cron commits with `--no-verify`, which skips the pre-commit hook, so this
      in-process check is that path's guard. It runs before the deploy, not after `rotate`
      returns, because by then the value is live and the cron's commit guard forbids a
      revert.
    - The same PR adds the three cluster paths to `TRACKED` in `secret-rotate.sh.j2`, only
      when the cluster file exists, because `git add` fails on a pathspec that matches
      nothing. `revert()` then covers them. `write` creates no untracked file, so
      `revert()`, which restores tracked paths only, leaves a clean tree.
    - The cron is a host process that holds the host's key, not an agent. D3.2c commits
      the files and the CI gate only after D3.2b's setup-plane apply has settled.

### Where the `cluster-vars` Kustomization is published (D3.3a)

`cluster-vars` is the first shared kind, a Kustomization that is not a deploy tag. The rule
that admits it exists before D3: C3 and the slice index have D4.3a ship `SHARED_KINDS` empty,
with the allowlist census and the reserved-name check, and D2's plan puts the same in D2.6b.
D3.3a waits on that PR and adds one literal member, `cluster-vars`. It writes no census or
reserved-name logic of its own.

- **Layout.** The Flux object is the only document in `out/flux/cluster-vars.yaml`. Its
  directory, `out/cluster/cluster-vars/`, holds the dotenv file and this
  `kustomization.yaml`:

  ```yaml
  apiVersion: kustomize.config.k8s.io/v1beta1
  kind: Kustomization
  namespace: flux-system
  generatorOptions: {disableNameSuffixHash: true}
  secretGenerator:
    - name: homelab-cluster-vars
      envs: [cluster-vars.sops.env]
  ```

  Its Flux Kustomization sets `decryption: {provider: sops, secretRef: {name: sops-age}}`,
  `prune: true`, `wait: true`, `deletionPolicy: Orphan`, and the same `sourceRef`,
  `interval`, `retryInterval` and `timeout` as a tag's Kustomization. Orphan keeps the Secret
  if the Kustomization is ever deleted. D1's root Kustomization builds `./flux`, so it
  applies the file with no change.
- **Membership is unconditional.** `cluster-vars` is in `SHARED_KINDS` from D3.3a on, whether
  or not a file exists. The render fails when it cannot copy the cluster file, and `check`
  fails when the file is missing. Deleting the file therefore fails CI rather than narrowing
  the census while dependents keep their `cluster-vars` edges.
- **A shared kind's file holds one document.** D3.3a adds the clause that a `SHARED_KINDS`
  member's file holds exactly the Kustomization of its own name. D4.3's clause covers a tag's
  file, so the two clauses do not overlap.
- **The bundle carries `cluster-vars` before any tag is flux-owned.** Under C3's default,
  D3.3a lands before D2.7, while D2's publish step still writes the root alone. D3.3a changes
  that branch: with no flux-owned tag, the step runs `homelab_render.py cluster --out out
  --shared-only`, which writes every `SHARED_KINDS` member and a root that lists them, and no
  tag. With flux-owned tags, the usual call writes the shared kinds beside the tags. The first
  decryption then gets a revision of its own, before any tag depends on it. C1 names no
  `--shared-only` flag (Open question 6).
- **The expected statuses need no change.** `out/flux/cluster-vars.yaml` holds one object
  named `cluster-vars`. `verify` expects `flux/<name>` for each Kustomization document, so it
  adds `flux/cluster-vars`. D1's Alert `commit-status` posts that status, because it sends
  every Kustomization's events.

### Which Kustomizations substitute (D3.3b)

- **One predicate per directory, read by every consumer.** The unit is the Kustomization
  directory under `cluster/`, not the tag. A directory substitutes when its placeholder key
  set is non-empty: the keys whose R2 sentinel survives pass 1 in that directory's render.
  `<tag>` and `<tag>-deferred` each have their own set, so a tag can substitute in one and
  not the other. `cluster-vars`'s directory holds no rendered template, so its set is
  always empty and it never substitutes from itself. The set is a fact of the render, not
  of ownership, so it has the same value in R2's CI over all 56 tags and in the publish
  job.
    - **The source** is the key set that R2's `placeholders.py` computes in process from the
      render result. R2's checks run where no `DIR/flux/` file exists, so they cannot read
      one.
    - **The projection** is the presence of `spec.postBuild` in that directory's
      Kustomization document in `DIR/flux/`, which the renderer writes exactly when the set
      is non-empty. Only readers of a written bundle read it: the publish refusal that C1
      names and D2.6c's source gate.
    - **The in-process consumers:** R2's pass 2 escapes `$` only in a directory whose set
      is non-empty. R2's rule 2 and its round trip treat only those directories as
      substituted. D2.6b's writer gives exactly those Kustomizations
      `postBuild.substituteFrom` on `homelab-cluster-vars`. D3.3b adds a `dependsOn` entry
      for `cluster-vars`, and D4 merges that entry with the toposort edges.
    - C1 *Substitution and escaping* and *Only placeholder mode publishes* still name a
      per-tag unit with the rendered Kustomization as the source. R2's Open question 5
      requests the per-directory wording before R2's PR 1, and D3.3b waits on that
      amendment (Open question 6).
- **Every routed Kustomization depends on `cluster-vars`.** Each IngressRoute holds
  `${domain}`, so the set is non-empty for every directory that holds a route, secret or
  not. A directory with no route and no secret reference gets neither field, so a literal
  `${` in it reaches the cluster unescaped.
- **The source gate stays as D2 wrote it.** D2.6c's gate admits a `substituteFrom` Secret
  only when a Kustomization in the bundle generates it, and from D3.3a on `cluster-vars`
  does. The gate does not check which keys that Secret holds. R2's rules 1, 2 and 4 put
  every reference in a substituting tag in R2's placeholder set, and `check` holds the
  dotenv's key names equal to that set less `SOPS_MANIFEST_KEYS`. D3.3b adds no key set to
  the gate.
- **No flux-owned tag changes source.** Under C3's default, D2.7, the first flip, waits on
  D3.3b. bento-pdf therefore substitutes from `homelab-cluster-vars` from its first revision,
  and D3.3b changes no live object.
    - Only under C3's **operator-only** alternative, where D2.7 renders `domain` as a literal,
      does a flux-owned tag change here. D3.3b then renders `${domain}` again, and each routed
      flux-owned tag gains `postBuild` and the `cluster-vars` edge in the same revision. The
      substituted objects do not change.
- **The promote timeout covers the hop.** Until D4.3's chained timeout lands, D3.3b adds the
  `cluster-vars` timeout and one `requeue-dependency` interval to D2's derived timeout when
  the bundle holds a substituting tag, because each such tag waits on `cluster-vars`. D4.3's
  chained sum covers the same hop once it lands.

### Restart triggers (D3.3c)

Ansible's shared `rollout restart` does not exist under Flux. The renderer therefore stamps
`checksum/restart` on the pod template of each restart target of a flux-owned tag, and the
pod rolls when the value moves. This is D5's per-workload rule, adopted under D5's name, so
D5.1e widens the target set instead of adding a second annotation.

- **The targets in D3** are those the shared role restarts: the `manifests_rollout`
  workload when it is non-empty, and every `manifests_extra_rollouts` entry, whatever
  `manifests_rollout` holds. Both are read from the role's include `vars:`. cloudflare-ddns,
  with an empty `manifests_rollout` and two extra rollouts, is covered here.
- **The value is per workload.** It is the SHA-256 of these inputs:
    - the ConfigMaps and Secrets that the workload's own pod template names through
      `envFrom`, `env[].valueFrom` and `volumes`, each parsed and re-serialised in canonical
      form, so a YAML comment edit moves nothing (#3115);
    - the `.source` hash of every key those Secrets reference;
    - for a Secret shipped as a SOPS manifest, its key's `.source` hash, not its ciphertext.

  Secrets are hashed in placeholder form, so the value exposes no plaintext. A rotated value
  changes the annotation in the same revision that carries its new ciphertext, and the
  `dependsOn` on `cluster-vars` applies the value first.
- **Both render modes compute the same value.** The renderer hashes the placeholder-form
  documents and `.source` in stub mode too, never its own stub output. R2's round trip
  compares a stub render with a placeholder render byte for byte, so the annotation must be
  equal on both sides.
- **A target that names no ConfigMap or Secret gets no annotation.** bento-pdf and texbrain
  are such targets, so their pod templates do not change, and neither rolls.
- **A reference outside the role is refused.** The annotation sees only the role's own
  render (failure class 2). A test in `scripts/renderer/tests/` reads every tag's render and
  fails when a flux-owned pod template names a Secret or ConfigMap that its own role does not
  render, unless the reference sits on a named exemption list. Its named member is peanut:
  the test must find peanut's two `secretKeyRef`s and its `secretName` volume and resolve
  each to peanut's own render. Its red fixture is a flux-owned fixture role whose pod names
  another role's Secret.
- **Self-rolled workloads wait for D5.1e.** pihole and observability restart their own
  workloads through `manifests_self_rollouts`, and D5 shows that nothing would restart them
  under Flux. Until D5.1e lands, a census test in `scripts/renderer/tests/` fails when an
  `owner: flux` entry's role passes a non-empty `manifests_self_rollouts`. It must find
  pihole and observability in the tree as named members, and its red fixture marks pihole's
  entry `owner: flux`. D5.1e replaces it with D5's census.
- **The other seven roles keep Ansible's behaviour.** They name no restart target, so
  Ansible restarts nothing for them on a config change, and neither does Flux. A rendered
  `checksum/*` annotation of their own still rolls their pods, because it is part of the
  render.
- **The SOPS manifest copy lands with the restart wiring.** It is the renderer half of *R2's
  manifests* above, and the `.source` hash of its key feeds the annotation.

### Rotating the cluster key (D3.4)

The cluster keypair is the one secret in this slice that `secrets.yml` does not hold. Each
step is **operator-only**, and the order keeps a working key in `sops-age` throughout.

1. Generate a replacement keypair, store its private half in the password manager, and add
   it to `sops-age` as a second entry, `age-next.agekey`. `Decryptor.ImportKeys` imports
   both entries, so either key decrypts.
2. On a branch, replace the cluster recipient in both cluster creation rules, then run `sops
   updatekeys` and `sops rotate` on the cluster file and each cluster manifest, from
   `ansible/` or with `--config ansible/.sops.yaml`. The values'
   ciphertext in `secrets.yml` does not move, so no `.source` hash moves, and no pod
   restarts. `check` reads the recipients from the rules, so it passes.
3. Land the PR with `land.sh`. From D3.4 on, `land-wait` waits on `cluster-vars` for a PR
   that changes the cluster file. Confirm that `flux/cluster-vars` reads `success` on the
   promoted revision with the statuses read under *Verification*.
4. Remove the old entry from `sops-age` and the old private half from the password manager.

The registry in `ansible/secret_rotation.yml` does not track the key, because
`registry_drift` would report its row as stale. `docs/secret-rotation.md` carries the
procedure, and `docs/break-glass.md` lists the key in its recovery kit.

### Rotating a value read by both planes (D3.4)

- **`consumer_commands` stops printing a refused line.** After D3.6, a `deploy.sh` line that
  names peanut exits 5, and `deploy.sh` refuses the whole tag list when one tag is refused
  (D2). D3.4 drops flux-owned tags from the `deploy.sh` line and from the tag set that
  `consumer_tags` gives `rotate --deploy`. It prints the Flux path for them instead: the
  operator's `cluster_vars.py write`, then landing the PR.
- **`land-wait` waits on the Flux half of a cluster-secret PR.** A PR that changes
  `cluster-vars.sops.env` or `cluster-vars.source` touches no role. D3.4 makes `land-wait`
  treat it as touching `cluster-vars` and every flux-owned tag whose Kustomization depends
  on `cluster-vars`. It names no changed key, so it keeps to the `DECIDED: fire on ANY change
  to secrets.yml` marker in `land_tags.py`.

### Tree diff

```diff
  ansible/.sops.yaml                         + two creation rules, ahead of the generic one;
                                               the header's add and remove runbook covers all three
  ansible/roles/setup/sops_setup/CLAUDE.md   onboarding names all three rules
+ ansible/vars/cluster-vars.sops.env
+ ansible/vars/cluster-vars.source
+ ansible/vars/cluster-manifests/            three SOPS-encrypted Secret manifests (R2's)
+ scripts/secrets_mgmt/cluster_vars.py       check (CI), --no-render, --ciphertext-only,
                                             and write (operator-only)
  scripts/secrets_mgmt/rotation_tools.py     sops_set takes a path
  scripts/secrets_mgmt/secret_rotation.py    rotate --commit calls write and check
                                             before the deploy
  scripts/secrets_mgmt/consumers.py          flux-owned tags leave the deploy.sh line
  ansible/roles/setup/initial_setup/templates/secret-rotate.sh.j2   TRACKED gains three paths
  .claude/hooks/block-protected-edits.py     is_sops_encrypted matches the dotenv MAC line
  .gitleaks.toml                             allowlist gains the cluster files, copies, .source;
                                             the comment names each path's guards
  prek.toml                                  cluster-vars-ciphertext hook
  prek.toml, .ansible-lint                   ansible-lint skips vars/cluster-manifests/
  .github/workflows/ci.yml                   pytest job installs sops and age
  scripts/renderer/                          cluster-vars kind, --shared-only, the
                                             substituteFrom source, dependsOn,
                                             checksum/restart, SOPS manifest copy,
                                             decryption, self-rollouts census,
                                             outside-role reference test
  scripts/deploy_tools/promote.py            the timeout hop
  scripts/deploy_tools/land_lib/             land-wait covers cluster-vars
  .claude/skills/add-secret/SKILL.md         the operator's write step for a cluster key
  docs/secret-rotation.md                    cluster file, and cluster key rotation
  docs/break-glass.md                        recovery kit gains the cluster age key
+ docs/adr/<next free>-...md                 amends ADR-0003 (operator-only approval)
  ansible/inventory/host_vars/daniel-box.yml peanut: owner: flux   (PR D3.6 only)
```

## Steps, as PRs

0. **D3.0 — The cluster key (operator-only, no PR).** Generate the keypair with `age-keygen`.
   Store the private half in the password manager. Create `sops-age` with `sudo k3s kubectl
   -n flux-system create secret generic sops-age --from-file=age.agekey=<file>`, then shred
   the file. Hand the public half to the D3.1 author.
1. **D3.1 — Recipients, runbooks and the ADR** (about 60 lines, plus the ADR).
    - Add the two creation rules to `ansible/.sops.yaml`. Adding a recipient is
      **operator-only**, as design.md's ADR section says.
    - Rewrite the `.sops.yaml` header's add and remove runbook, and the *Notable* section of
      `ansible/roles/setup/sops_setup/CLAUDE.md`, for three rules and four kinds of file.
      Each `sops` command there runs from `ansible/` or with `--config ansible/.sops.yaml`,
      and the header says the cluster rules stay ahead of the generic one. H4 also edits
      `.sops.yaml`, so whichever lands second rebases onto the other's rules.
    - Draft the ADR-0003 amendment: the cluster decrypts the cluster file with its own key,
      and never `secrets.yml`. If H4's amendment has already landed, extend it instead.
      Approving it is **operator-only**.
2. **D3.2a — `cluster_vars.py` and its guards** (about 260 lines). Inert: no tree file and no
   tree gate. The guards land in the same PR as the allowlist that relies on them.
    - Add the script, with its tests in `scripts/secrets_mgmt/tests/`. The tests are
      `_is_clean` and `_is_flagged` pairs for each `check` assertion. They run `write`
      against a throwaway age key in `tmp_path`, never a real one, and assert that a
      no-change `write` leaves the file byte-identical. Three more `write` tests:
        - run from a working directory other than `ansible/`, it encrypts to the cluster
          rule's recipients;
        - afterwards, no file under the fixture tree holds a fixture plaintext value;
        - with the encrypt made to fail, it adds and changes no file.
    - `rotation_tools.py:sops_set` gains a path argument that defaults to `secrets.yml`.
    - Extend `is_sops_encrypted` to the dotenv form. An `_is_clean` and `_is_flagged` pair
      joins `.claude/hooks/tests/test_block_protected_edits.py`: a plaintext dotenv is clean,
      and a dotenv holding a `sops_mac=ENC[AES256_GCM,` line is flagged. One test joins
      `.claude/hooks/tests/test_block_protected_bash.py`: a shell write to the flagged
      fixture draws `ask`.
    - Add the `cluster-vars-ciphertext` pre-commit hook to `prek.toml`.
    - Add the CI step that installs `sops` and `age`, and the test in `ansible/tests/repo/`
      that holds it to the `sops_setup` pin.
    - Extend `.gitleaks.toml`, rewrite its comment, and add the two ansible-lint exclusions,
      as *Design* says.
3. **D3.2b — The rotation hook** (about 90 lines).
    - `secret_rotation.py rotate --commit` calls `write` for a rotated cluster-referenced
      key and then `check --no-render --base HEAD`, both before the deploy. It does nothing
      while the cluster file is absent. `secret-rotate.sh.j2` adds the three cluster paths
      to `TRACKED` once the file exists, and its PR body names them.
    - Tests in `scripts/secrets_mgmt/tests/`: a rotated cluster key reaches the `write`
      call, a host-only key does not, and an absent cluster file makes the hook a no-op. A
      failing `write` and a failing `check` each make `rotate` return non-zero without
      calling the deploy. A test in `ansible/tests/setup/` holds the rendered `TRACKED` to
      the three paths behind the existence check.
    - **Landing.** The template is a setup-plane change, which the deployer applies. The
      `land.sh` verdict must show the `initial_setup` apply settled before D3.2c merges.
4. **D3.2c — The first files and the tree gate** (generated files, plus about 30 lines).
    - **Preconditions**, quoted in the PR body: D3.2b's verdict was `settled`, no rotation
      PR is open (`gh pr list --search auto-rotate`), and the merge does not fall on a
      Sunday between the cron's 09:00 pull and its PR.
    - **Operator-only before merge:** run `cluster_vars.py write` on the branch, in a
      worktree and never in the primary checkout, and commit the output. The first cluster
      file is untracked until that commit. If `write` refuses a value, follow Open question 7 and list the refused key
      names in the PR body.
    - A test in `scripts/secrets_mgmt/tests/` runs `check` on the tree, so CI's `pytest` job
      gates every later PR.
    - The root `CLAUDE.md` *Secrets Management* section gains one sentence on the cluster
      file.
5. **D3.2d — The successor refusals** (about 60 lines). Waits on D3.2a only, and may land
   before or after D3.2c. D5's authelia and mosquitto PRs wait on it.
    - `write` gains the two per-key refusals under *Design*: R3's PHC pattern and
      distinctness for the `_argon2` keys, and no `:` in `mqtt_password_hash`. D3.2a already
      refuses `{{` with the other characters.
    - Tests in `scripts/secrets_mgmt/tests/`, each run through `write` on a fixture tree:
        - A well-formed set of distinct digests is clean.
        - Each of these is refused: a digest minted with `t=2`, one with a `Digest: `
          prefix, one with trailing whitespace, a pasted plaintext, and two equal digests.
        - With `authelia_k8s_manage_claude_user` false in the fixture, a malformed
          claude-ui digest is clean, because `write` does not place it.
        - For mosquitto, `$7$abc` is clean and `user:$7$abc` is refused.
        - Each refusal message names the key and not the value, and each refused run adds
          and changes no file.
    - `ansible/tests/services/test_authelia_argon2_parameters_agree.py` gains the
      constant as a named member. R3's PR 3 and PR 4 also edit that file, and whichever
      lands second rebases onto the other.
    - **Operator-only before merge, when D3.2c has landed:** run `write` on the branch, in a
      worktree and never in the primary checkout. A no-change `write` leaves every file
      byte-identical, so its exit status proves that each committed value passes the new
      refusals. A refusal follows Open question 7.
6. **D3.3a — The `cluster-vars` kind** (about 120 lines). Waits on D4.3a.
    - The renderer adds `cluster-vars` to `SHARED_KINDS`, emits `out/flux/cluster-vars.yaml`
      and `out/cluster/cluster-vars/`, and gains `--shared-only`. The publish step's no-tag
      branch calls it. The census gains the shared-kind file clause, and the publish job runs
      `check --ciphertext-only` on its dotenv and SOPS manifest copies. Tests in
      `scripts/renderer/tests/` and
      `scripts/deploy_tools/tests/` cover each rule with a red fixture, listed under
      *Verification*.
    - **Operator-only precondition**, quoted in the PR body, with the commands under
      *Verification*. Derive the public key from the private half in `sops-age` with
      `age-keygen -y`, compare it with the cluster rule's recipient, and print `match` or
      `mismatch`. Then decrypt the committed cluster file with that key as the only identity
      sops can load, printing only the exit status. sops also opens the default key file
      under `XDG_CONFIG_HOME`, and on daniel-box that key is a recipient, so the run points
      `XDG_CONFIG_HOME` at an empty directory. Before the real run counts, the same command
      with an unrelated, freshly generated key must exit non-zero.
    - **This PR is live.** The bundle gains `cluster-vars`, so Flux decrypts for the first
      time, and `promote` waits on `flux/cluster-vars`. No Kustomization reads
      `homelab-cluster-vars` yet. After it is promoted, the statuses read under
      *Verification* shows `flux/cluster-vars` at `success`.
7. **D3.3b — The `cluster-vars` edge** (about 80 lines).
    - Add the `cluster-vars` edge to every substituting Kustomization, and the timeout hop.
      D2.6b's writer already names `homelab-cluster-vars`, and D2.6c's source gate needs no
      change (*Which Kustomizations substitute*).
    - Tests in `scripts/renderer/tests/`: a fixture directory with a placeholder gets
      `substituteFrom` on `homelab-cluster-vars` and the `cluster-vars` edge, and one without
      gets neither and keeps a literal `${` unescaped. A fixture tag whose `<tag>` directory
      holds a placeholder and whose `<tag>-deferred` directory holds none gives both fields
      to the first Kustomization only, and the rendered `cluster-vars` Kustomization carries
      neither.
    - A pair in `scripts/deploy_tools/tests/`: the derived timeout grows by the
      `cluster-vars` timeout plus one `requeue-dependency` interval when the bundle holds a
      substituting tag, and does not grow when it holds none.
    - **Inert for the cluster** under C3's default, because no tag is flux-owned yet. After it
      is promoted, `flux/cluster-vars` still reads `success`. D2.7 waits on this PR, and exit
      criterion 5 is checked after D2.7 settles.
8. **D3.3c — Restart and SOPS-manifest wiring** (about 180 lines).
    - Add `checksum/restart` for the D3 targets, the outside-role reference test, the
      self-rollouts census, and the SOPS manifest copy with `decryption`. C3's per-role
      `decryption` field (Open question 6) is amended before this PR merges.
    - Tests in `scripts/renderer/tests/`:
        - the checksum moves when a `.source` hash moves and stays put for a comment edit;
        - cloudflare-ddns's two Deployments each get the annotation, and a change to one
          Secret moves only the workloads that name it;
        - bento-pdf's pod template gets no annotation;
        - the stub render and the placeholder render of a fixture flux-owned tag carry the
          same annotation value;
        - a fixture role with `render_sops_manifests` gets the manifest, byte for byte, and
          `decryption` on its Kustomization, and one without gets neither. A fixture manifest
          without the substitute-disabled annotation fails the render;
        - the outside-role reference test and the self-rollouts census, with the members and
          fixtures under *Restart triggers*.
    - R1's shadow comparison covers Ansible-owned tags only from D2.7 on, because a
      flux-owned tag has no Ansible render.
    - **Inert for the cluster:** bento-pdf and texbrain name no ConfigMap or Secret, and no
      role with a SOPS manifest is flux-owned.
9. **D3.4 — Rotation runbooks and the Flux-side wait** (about 150 lines).
    - Change `consumers.py` and `land-wait` as *Rotating a value read by both planes* says.
      Tests, each a pair:
        - in `scripts/secrets_mgmt/tests/`: a fixture key read by a flux-owned and an
          Ansible-owned tag prints `deploy.sh` for the second and the Flux path for the
          first, and a key read only by Ansible-owned tags prints the line it printed before;
        - in `scripts/deploy_tools/tests/`: a PR changing only `cluster-vars.source` waits on
          `cluster-vars` and a fixture dependent, and a PR changing neither cluster file adds
          no wait.
    - Edit `.claude/skills/add-secret/SKILL.md`. An agent adding a cluster-referenced key
      stops before the PR and hands `write` to the operator.
    - Edit `docs/secret-rotation.md`: the cluster file, and the cluster key rotation
      procedure under *Design*, marked **operator-only**.
    - Add the cluster age key to the recovery kit in `docs/break-glass.md`: where its private
      half lives, that the cluster needs it only as `sops-age`, and that a lost key is
      replaced by the rotation procedure from any host that decrypts `secrets.yml`.
10. **D3.5 — Evidence before the cutover (operator-only, no PR).** For `peanut-secrets` and
   two other Secrets, record the managers and whether `last-applied-configuration` is
   present, with the commands under *Verification*. Each manager must be one whose prefix
   Flux takes over (`kubectl-client-side-apply`, or `kubectl-patch` from
   `verify_secret_keys.yml`). Another manager means Flux will not remove that manager's keys.
   Record it in the D3.6 PR body before D3.6 merges.
11. **D3.6 — The flip** (about 20 lines).
    - Add `owner: flux` to peanut's entry, with the preconditions and the two-writer check
      from D2.7, adapted to peanut.
    - Three more preconditions: D3.3c and D3.4 have landed, `flux/cluster-vars` has been
      `success` on the last two promoted revisions, and no placeholder in peanut's render is
      missing from the cluster file, which `check` already asserts.
12. **D3.7 — Add the canary** (one line). Add `FLUX_CANARY: "d3"` to `peanut-secrets`. It is a
    literal, so it needs no secret.
13. **D3.8 — Drop the canary** (one line). Remove it. D3.7 and D3.8 are exit criterion 2.
14. **D3.9 — The rotation drill.**
    - **Operator-only:** change `peanut_password` with `sops set`, run `cluster_vars.py write`,
      and open the PR.
    - The agent lands it with `land.sh`, which waits on `cluster-vars` and peanut (D3.4), and
      confirms both statuses with the read under *Verification*.
    - The agent then runs the Ansible half that `secret_rotation.py consumers
      peanut_password` prints, `./scripts/deploy.sh --tags homepage`, because homepage's
      PeaNUT widget renders the same password. Exit criterion 3 is checked after both settle.

## Verification

```bash
# D3.3a precondition, operator-only. Prints a public key and two exit statuses, never a value
# or a private half. EMPTY holds no sops/age/keys.txt, so sops cannot fall back to the host's
# own key, which is a recipient of the cluster rules.
umask 077
KEYS=$(mktemp -d); EMPTY=$(mktemp -d)
sudo k3s kubectl -n flux-system get secret sops-age -o jsonpath='{.data.age\.agekey}' \
  | base64 -d > "$KEYS/cluster.agekey"
age-keygen -y "$KEYS/cluster.agekey"   # compare with the cluster rules' recipient
age-keygen -o "$KEYS/unrelated.agekey" 2>/dev/null
# Red proof first: an unrelated key must print a non-zero status, or the next line proves
# nothing.
env -u SOPS_AGE_KEY -u SOPS_AGE_KEY_CMD XDG_CONFIG_HOME="$EMPTY" \
  SOPS_AGE_KEY_FILE="$KEYS/unrelated.agekey" \
  sops decrypt ansible/vars/cluster-vars.sops.env >/dev/null 2>&1; echo "unrelated: $?"
env -u SOPS_AGE_KEY -u SOPS_AGE_KEY_CMD XDG_CONFIG_HOME="$EMPTY" \
  SOPS_AGE_KEY_FILE="$KEYS/cluster.agekey" \
  sops decrypt ansible/vars/cluster-vars.sops.env >/dev/null 2>&1; echo "cluster: $?"
shred -u "$KEYS"/*.agekey; rmdir "$KEYS" "$EMPTY"

# D3.3a and D3.3b live, and D3.4 step 3: the status on the promoted revision T.
gh api repos/DanielH2018/server/commits/<T>/statuses \
  --jq '.[] | select(.context == "flux/cluster-vars") | .state'      # success

# Exit 1: Flux owns peanut, and the page renders.
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io cluster-vars peanut
uv run python scripts/diagnostics/probe.py health peanut
# Then load peanut.local.<domain> with the homelab-ui MCP server.

# Operator-only: the substituted values match the source, printing match or mismatch only.
# The operator compares sha256 of each live value with sha256 of the sops-decrypted value
# on a host that decrypts, without printing either.

# Exits 1 and 2, operator-only: manager names, presence of last-applied, key names.
sudo k3s kubectl -n homelab get secret peanut-secrets \
  -o jsonpath='{.metadata.managedFields[*].manager}'
sudo k3s kubectl -n homelab get secret peanut-secrets -o go-template=\
'{{if index .metadata.annotations "kubectl.kubernetes.io/last-applied-configuration"}}present{{else}}absent{{end}}'
sudo k3s kubectl -n homelab get secret peanut-secrets \
  -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}'

# Exit 3: the restart annotation moved, and the pod is newer than the revision.
kubectl -n homelab get deploy peanut \
  -o jsonpath='{.spec.template.metadata.annotations.checksum/restart}'
kubectl -n homelab get pods -l app=peanut \
  -o jsonpath='{.items[*].metadata.creationTimestamp}'
gh api repos/DanielH2018/server/commits/<T>/statuses \
  --jq '.[] | select(.context == "flux/peanut") | .state'            # success

# Exit 4: CI's gate, run locally.
uv run python scripts/secrets_mgmt/cluster_vars.py check

# Exit 5, after D2.7 settles: bento-pdf substitutes from homelab-cluster-vars and routes.
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io bento-pdf \
  -o jsonpath='{.spec.postBuild.substituteFrom[*].name}{" "}{.spec.dependsOn[*].name}'
# Prints homelab-cluster-vars and cluster-vars. Then load bento-pdf.local.<domain> with
# the homelab-ui MCP server.
```

**Each new check can go red.**

- `check` has a flagged fixture for each assertion. The decisive one is a fixture
  `secrets.yml` whose ciphertext for one cluster key differs from `.source`. Others are a
  dotenv fixture with one plaintext value, which the `ENC[` assertion must flag, a cluster
  file whose recipients lack one host from its rule, a manifest with a renamed data key, a
  manifest without the substitute-disabled annotation, and a tree with no cluster file.
- The MAC proxy is flagged by a dotenv fixture with one value line removed and `sops_mac` and
  `sops_lastmodified` unchanged from its base. Its clean half removes the same key with
  `write`, which moves both lines. A test asserts that the failure message names
  `cluster_vars.py write`.
- The recipient assertion is flagged by a manifest encrypted to the generic rule's
  recipients alone. A check that ignored rule order would accept it, because the generic
  rule matches the manifest too.
- `write` refuses a fixture value holding each forbidden character or `{{`, and passes one
  holding a single `{`. A test asserts that the refusal message names the key and not the
  value.
- D3.2d's refusals go red on the fixtures listed under that step: a digest minted with
  `t=2`, two equal digests and `user:$7$abc` each fail `write`. Their clean halves, a
  distinct well-formed set and `$7$abc`, pass. The parameter-agreement test fails when the
  constant's `t=` differs from the rendered hashing block's `iterations`. With `sops` or `age` off `PATH`, the
  `write` tests fail rather than skip. The working-directory test fails when `write` runs
  without `--config`, the no-plaintext test fails when a fixture value lands in any file,
  and the failed-encrypt test fails when a file is added or changed.
- The hook pair fails when `is_sops_encrypted` misses a dotenv `sops_mac=` line, and the
  Bash arm's test fails when a shell write to that fixture draws no `ask`.
- The `rotate` pair fails when a failing `write` or `check` still reaches the deploy.
- The pin test fails when the CI step's sops version or checksum differs from the
  `sops_setup` defaults.
- The publish census fails on three fixtures: a renderer patched to drop
  `cluster-vars.yaml`, an unknown extra file in `out/flux/`, and a second document in
  `cluster-vars.yaml`. A fixture tag named `cluster-vars` fails the reserved-name check. The
  expected-status set of a fixture bundle must name `flux/cluster-vars`, or the test fails. A
  `--shared-only` render of a fixture tree holds the root and `cluster-vars`, and no tag.
- The predicate test fails when a fixture directory without a placeholder gets `postBuild`,
  when its literal `${` is escaped, or when a `<tag>-deferred` directory with no placeholder
  inherits `postBuild` from its tag, as a per-tag flag would give it.
- A placeholder missing from the dotenv fails `check`'s names assertion, and a reference to a
  key outside `secrets.yml` fails R2's `known-key` rule. D3 adds no source-gate key set.
- The timeout pair fails when a bundle with a substituting tag derives D2's timeout unchanged.
- The SOPS manifest pair fails when a role without `render_sops_manifests` gets `decryption`,
  and when a manifest without the annotation renders.
- The outside-role reference test fails on its fixture role and must resolve peanut's three
  references. The self-rollouts census fails on pihole's entry marked `owner: flux`, and
  when either pihole or observability is missing from the members it finds in the tree.
- The `consumers.py` pair fails when a flux-owned tag stays on the `deploy.sh` line, and the
  `land-wait` pair fails when a cluster-file PR adds no wait.
- Strict substitution is the live red proof that a key is missing. Before D3.6, a draft branch
  of peanut's template that names a key absent from the cluster file fails R2's `known-key`
  rule in CI. That draft is never merged.
- The canary pair (D3.7, D3.8) is the red proof for key removal. Its first read must show the
  key, or the second read proves nothing.
- Exit 5's read fails with `NotFound` before D2.7, because bento-pdf has no Kustomization,
  so the check tells the two states apart.

## Rollback

- **D3.0.** Deleting `sops-age` is a write verb (**operator-only**).
- **D3.1.** Revert. It changes no cluster object.
- **D3.2a.** Revert it before D3.2c lands, or together with D3.2c, whose tree gate calls
  `check`. Revert D3.2d first, because its refusals live in `write`. Revert it whole: the allowlist must not outlive the hook pattern and the
  pre-commit hook that its comment names.
- **D3.2b.** Revert it only with D3.2c, or before D3.2c lands. Without the hook, an auto-tier
  rotation leaves `.source` stale, and the rotation PR fails the tree gate.
- **D3.2c.** Revert it only before D3.3a, because from D3.3a on the render needs the cluster
  file.
- **D3.2d.** Revert it only while authelia and mosquitto are both Ansible-owned. Once D5 has
  moved either, its refusal is the only guard left on that role's value, so a revert waits
  for D5's handback of that role. The revert changes no cluster object and no file `write`
  produced.
- **D3.3c.** Revert D3.6 first if peanut is flux-owned, because without the annotation a
  rotated value would not restart it. Otherwise the revert changes no live object.
- **D3.3b.** Revert D3.6 first if peanut is flux-owned, then D2.7 (D2's handback), because
  D2.7's preconditions name D3.3b. Without the edge, a substituting tag can reconcile before
  `cluster-vars` applies a rotated value. On a rebuilt cluster its build fails until
  `homelab-cluster-vars` exists.
- **D3.3a.** Revert D3.3b first, with what that needs, because its edges name
  `cluster-vars`. The root then prunes the `cluster-vars` Kustomization, and
  `deletionPolicy: Orphan` leaves `homelab-cluster-vars` in place, unused.
- **D3.4.** Revert. `consumers.py` prints the old line, and `land-wait` stops waiting on a
  cluster-file PR. A key rotation in flight is undone by putting the old recipient back, and
  `sops-age` still holds the old key until step 4 of the procedure.
- **D3.6, the handback.** As in D2. Once peanut is Ansible-owned again,
  `verify_secret_keys.yml` reconciles its Secret on the next deploy.
- **What partial failures leave:**
    - A missing or wrong `sops-age` fails `cluster-vars`. `flux/cluster-vars` turns
      `failure` and `promote` holds. Every dependent stays at its last revision
      (`DependencyNotReady`), so live Secrets and routes keep their last values.
    - A cluster key edited without regeneration fails `check`, and the PR cannot merge.
    - A cluster file edited by hand fails the pre-commit hook and `check`'s MAC proxy. One
      that reaches master anyway fails to decrypt: `flux/cluster-vars` turns `failure`,
      `promote` holds, and dependents keep their last revision until the operator reruns
      `write` (**operator-only**).
    - If the rotation cron's `write` or its in-process `check` fails, `rotate` returns before
      the deploy. The cron's `revert()` restores every `TRACKED` path, the three cluster
      paths included, while no rotated value is live, and `write` has left no untracked
      file.

## Risks

- **A compromised cluster key** exposes the cluster-referenced values, but never the rest of
  `secrets.yml`. Mitigation: the key is dedicated, and `.sops.yaml` gives it to two path
  rules only. The D3.4 procedure protects future revisions only, because every past revision
  in git and every published `sha-<sha>` artifact holds a copy of the cluster file. A
  suspected compromise also needs every key in the cluster file rotated (**operator-only**)
  and its consumers redeployed. `docs/secret-rotation.md` and `docs/break-glass.md` state
  that distinction in D3.4.
- **Every routed tag depends on decryption.** `${domain}` puts each IngressRoute behind
  `cluster-vars`, so a broken `sops-age` stops every routed flux-owned tag from reconciling.
  Mitigation: they stay at their last revision and keep serving, and `promote` holds on
  `flux/cluster-vars`. D3.3a's precondition proves the key pair matches before the first
  decryption, and D3.3a promotes that decryption before any tag depends on it.
- **A rotation lands between the files and the hook** (failure class 5, a sequence treated as
  a transaction). The cron deploys a rotated value before its PR exists. If the cluster files
  and their CI gate landed before the hook, the PR would fail `check` with the value already
  live, and the cron would stop rotating while that PR stayed open. Mitigation: D3.2b lands
  and applies the hook and `TRACKED` before D3.2c commits the files and the gate, and D3.2c
  requires that no rotation PR is open.
- **A value read by both planes rotates in two steps** (failure class 5). The cron's
  `--deploy` applies the Ansible half at once, and the Flux half arrives only after the PR
  merges and `promote` moves. Latent until D5 flips a consumer of an auto-tier token, such
  as cloudflare-ddns or autofix-bridge. Mitigation: D3.4 keeps flux-owned tags off the
  `deploy.sh` line, so the cron never fails on exit 5. The ordering of the two halves is
  D5's, and a cross-slice note hands it over.
- **A data-key rotation restarts every reader.** `sops rotate` on `secrets.yml` re-encrypts
  every value, so every `.source` hash moves and each flux-owned pod that reads a secret
  restarts in one revision. That happened once in the last 20 commits. Mitigation:
  `docs/secret-rotation.md` schedules a data-key rotation in the weekly maintenance window,
  and the `.source` diff shows the count of affected keys in review.
- **A hand-edited cluster file passes every name and hash assertion** (failure class 2).
  `check` reads names and hashes, which stand in for "the file decrypts". Deleting a value
  line from the dotenv and `.source`, the obvious response to a dropped reference, keeps
  those green and breaks the SOPS MAC. `cluster-vars` then fails and `promote` holds.
  Mitigation: the protected-edits hook denies an Edit or Write to the dotenv form, the
  pre-commit hook and `check` compare the MAC and `lastmodified` lines with the base, and
  every `check` message names `write` as the only fix. The proxy misses an edit that also
  rewrites both lines, so the decrypt in D3.3a's precondition and in `cluster-vars` stays
  the real test.
- **sops resolves its config from the working directory** (failure class 7). A `write` or a
  runbook command run from the repo root, as the rotation cron runs, finds no creation rule.
  Mitigation: `write` passes `--config` on every call that resolves a rule, a test runs it
  from outside `ansible/`, and the runbooks say to run from `ansible/` or pass `--config`.
- **A precondition can pass on the wrong key** (failure class 7). sops opens the host's
  default key file whatever `SOPS_AGE_KEY_FILE` names, and daniel-box's key is a recipient.
  Mitigation: the D3.3a decrypt runs with `XDG_CONFIG_HOME` on an empty directory, and an
  unrelated key must fail first.
- **A deploy-time assert stops running when its role moves to Flux** (failure class 4, a
  guard's selector drifts from the hazard's extent). Three asserts guard values that CI
  cannot read: R3's digest assert, R2's PR 4 colon count and R2's PR 7 `{{` count. Each
  runs only in an Ansible play, so each stops guarding when D5 moves its role.
  Mitigation: `write` refuses the same shapes, and every value reaches a cluster file only
  through `write`. D5's authelia and mosquitto preconditions require D3.2d, and R4 names
  `write`'s refusals as the asserts' successors (cross-slice notes). The refusals run only
  where `write` decrypts, on the operator's host or in the rotation cron. A cluster file
  that bypassed `write` still fails the MAC proxy in `check`, but no CI check reads a
  value's shape.
- **The operator becomes a step in every cluster-secret change.** Mitigation: `check` tells
  the author exactly which keys need `write`, and the rotation cron runs `write` itself.
- **The checksum misses a mount it cannot see** (failure class 2). A pod that reads a Secret
  outside its own role is not stamped. Mitigation: D3.3c's outside-role reference test, with
  peanut as its named member and a red fixture.
- **A self-rolled workload loses its restart** (failure class 2). Mitigation: the
  self-rollouts census refuses the flip until D5.1e covers the role.
- **Substitution differs from the CLI.** R2 proves the round trip with `flux envsubst`. Exit
  criteria 1 and 3 are the in-cluster proof for peanut. D5 repeats exit 1 for each role it
  moves. A SOPS manifest is decrypted before substitution, so its annotation keeps a
  `${` in a key out of substitution.
- **Host recipients drift between the three rules** (failure class 4). Mitigation: `check`
  compares each cluster file's recipients with its own rule's full list, and the runbooks
  name all three rules.
- **Sequence, not transaction** (failure class 5). The value and its restart arrive in one
  revision, and the order between them comes from `dependsOn`. If `cluster-vars` fails,
  dependents keep the old value and the old pods together, which is consistent.

## Tests and docs that change in the same PR

- D3.1: the ADR-0003 amendment, and its row in `docs/adr/index.md` and the `mkdocs.yml` nav
  (`ansible/tests/repo/test_adr_links.py` checks both). The `.sops.yaml` header and
  `ansible/roles/setup/sops_setup/CLAUDE.md`.
- D3.2a: tests in `scripts/secrets_mgmt/tests/`, the pin test in `ansible/tests/repo/`, and
  the hook tests in `.claude/hooks/tests/test_block_protected_edits.py` and
  `.claude/hooks/tests/test_block_protected_bash.py`. The docstring of
  `block-protected-edits.py` names the dotenv form beside the `mac:` line.
  `.github/workflows/ci.yml` gains the install step. The `.gitleaks.toml` comment names each
  allowlisted path's guards and the `.source` scope. `prek.toml` gains the
  `cluster-vars-ciphertext` hook, and `prek.toml` and `.ansible-lint` each gain the
  exclusion with its reason.
- D3.2b: tests in `scripts/secrets_mgmt/tests/` and `ansible/tests/setup/`.
  `secret-rotate.sh.j2`'s header comment and its PR body, which says the diff is the secrets
  file and the registry, name the three cluster paths. A search of
  `docs/host-baseline-record.md`, `docs/secret-rotation.md` and the `initial_setup` role's
  `CLAUDE.md` for `TRACKED` finds no other record of the cron's file list.
- D3.2c: the test that runs `check` on the tree, in `scripts/secrets_mgmt/tests/`. The
  `CLAUDE.md` *Secrets Management* section gains one sentence on the cluster file. Until G2
  retires `docs/facts.lock`, run `fact_status.py verify` on that section if it cites an
  atom.
- D3.2d: tests in `scripts/secrets_mgmt/tests/`, and the named member in
  `ansible/tests/services/test_authelia_argon2_parameters_agree.py`.
  `docs/secret-rotation.md` gains one sentence: `write` refuses a malformed or duplicated
  `_argon2` digest and a `:` in `mqtt_password_hash`, and names the key only.
- D3.3a: tests in `scripts/renderer/tests/` and `scripts/deploy_tools/tests/`.
  `docs/flux-deploy-plane.md` gains the `cluster-vars` kind and `--shared-only`.
- D3.3b: tests in `scripts/renderer/tests/` and `scripts/deploy_tools/tests/`.
  `docs/flux-deploy-plane.md` gains the substitution predicate and the `cluster-vars` edge.
- D3.3c: tests in `scripts/renderer/tests/`. `docs/flux-deploy-plane.md` gains
  `checksum/restart` and the SOPS manifest path.
- D3.4: `docs/secret-rotation.md`, `docs/break-glass.md`,
  `.claude/skills/add-secret/SKILL.md`, and tests in `scripts/secrets_mgmt/tests/` and
  `scripts/deploy_tools/tests/`.
- D3.6: `ansible/roles/k8s/peanut/CLAUDE.md`.

## Complexity delta

- **Added:** one age key and its bootstrap Secret, two creation rules, three generated files
  and a directory of manifests, `cluster_vars.py` (about 260 lines with the MAC proxy and
  two narrower `check` modes, plus their tests), one shared Kustomization kind and a
  `--shared-only` render, about 300 renderer lines, one CI install step, one hook pattern,
  one pre-commit hook, two lint exclusions, and one operator-only key rotation procedure.
  D3.2d adds two per-key refusals to `write` (about 60 lines with their tests) and one
  member to the argon2 parameter-agreement test. `{{` joins D3.2a's character set.
- **Not added:** a key set in D2.6c's source gate. D2 declines it, and `check` with R2's
  rules covers it.
- **Removed:** for each flux-owned role, `verify_secret_keys.yml` no longer runs. R4 deletes
  it once no role is Ansible-owned. Under C3's default, no interim `domain` source exists, so
  D3 has none to retire.

## Open questions

1. **Who owns the ADR-0003 amendment, D3 or H4?** design.md assigns it to both. Resolution:
   whichever lands first writes it, and the other extends it. This is listed as a contract
   gap.
2. **Should the cluster file hold every cluster-referenced key from the start, or only the
   flux-owned roles' keys?** This plan holds every key, so that D5 needs no regeneration.
   That places values for roles that Ansible still deploys inside the cluster Secret.
   Resolution: the operator confirms in the D3.2c review (**operator-only**).
3. **Does `sops set` work on a dotenv file with the pinned sops version?** Resolution:
   D3.2a's tests run `write` against a throwaway key with the pinned binary, which its CI
   step installs.
4. **The source of `domain`, and whether it should need decryption.** D3 follows C3's
   default: D2.7 waits on D3.3b, and `domain` comes from `homelab-cluster-vars`.
    - D2 has withdrawn D2.6a and its Secret `homelab-identifiers`. Nothing remains to
      retire.
    - C3's alternative, a literal `domain`, is **operator-only**. Under it, D3.3b replaces
      the literal, as *Which Kustomizations substitute* says.
    - Keeping `domain` and `email` on a source that needs no decryption would keep routed
      tags without secrets off the `cluster-vars` edge, at the cost of a second source beside
      C3's one Secret. C3 does not allow it, and this plan does not propose it.

   Resolution: settled for the default. The operator chooses the alternative before D2.7 if
   at all.
5. **What is `SHARED_KINDS`'s origin, D4.3a or D2.6b?** C3 and the slice index name D4.3a.
   D2's plan ships the set in D2.6b, and D4's step list has no D4.3a. Resolution: D3.3a waits
   on whichever PR ships the empty set, census and reserved-name check. The contracts.md
   maintainer and D4 reconcile the name (cross-slice notes).
6. **Contract amendments this slice still needs.** C1's shared-kind output, its escape rule
   and C3's shared kinds are already in contracts.md. Proposed wording for the rest:
    - C3 *Per-role fields* gains a fourth field: "`decryption: {provider: sops, secretRef:
      {name: sops-age}}`, derived from a non-empty `render_sops_manifests`, never set by
      hand". The shared kind `cluster-vars` also sets it. Gate: before D3.3c merges.
    - C3's second secret path gains: "Each SOPS manifest carries
      `kustomize.toolkit.fluxcd.io/substitute: disabled` in its plaintext metadata, written
      before encryption, and the renderer copies it byte for byte." Gate: before D3.3c
      merges.
    - C3 *Secrets* gains: "`sops-age` is the only Secret made by hand." Gate: before D3.3a
      merges.
    - C1 *Modes* gains `cluster --out DIR --shared-only`, which writes the shared kinds and
      the root and no tag, for a publish with no flux-owned tag. Gate: before D3.3a merges.
    - The slice index names D3.3a for the D4.3a edge and D3.3b for D2.7's edge, and C3's
      *Shared kinds* names D3.3a.
    - C3's value rule gains `{{`: "A value holds no newline, double quote, backslash,
      control character or `{{`". Authelia's template filter parses its whole
      configuration file, and `write` already refuses the run. Gate: before D5's authelia
      PR merges.
    - C1 *Substitution and escaping* and *Only placeholder mode publishes* move to the
      per-directory unit, with the renderer's in-process key set as the source and
      `spec.postBuild` as its projection. R2's Open question 5 requests it, gated before
      R2's PR 1. D3.3b reads the predicate that way already.

   Resolution: the contracts.md maintainer applies each before its gate.
7. **What follows a value that `write` refuses?** One refused value blocks D3.2c, or D3.2d
   when D3.2c has landed. Resolution, each step **operator-only**, then a rerun of `write`.
   The PR body lists the refused key names, never values.
    - A refused character or `{{`: rotate the value to one that complies, and redeploy its
      consumers through Ansible.
    - A `:` in `mqtt_password_hash`: store only the part after the first `:` of the
      `mosquitto_passwd` line, R2's rule in `docs/secret-rotation.md`. The rendered
      password file keeps the same `user:hash` line, before R2's PR 4 through the
      template's split and after it through the stored value.
    - A malformed or duplicated `_argon2` digest: mint it again with R3's procedure, which
      validates the digest against the plaintext before `sops set`.

## Review log

### Round integration-2 (cross-slice integration review)

Two findings were raised against D3: one high and one medium. Both are fixed for D3's share,
and neither was refuted. Each claim was re-measured at `70591c307` first. The parts that
belong to D5, R2, R3, R4, H1 and contracts.md went to them as cross-slice notes.

- **X2-03 (high). `cluster_vars.py write` lacked the successor refusals that R2 and R3 rely
  on once Flux renders authelia and mosquitto.** Fixed. A search of this file for `argon2`
  and `mqtt_password_hash` found nothing, and `write` refused only four characters. The tree
  confirms each subject. `config-secret.yaml.j2` in authelia sets `iterations: 3`,
  `parallelism: 4`, `memory: 65536`, `salt_length: 16` and `key_length: 32`, the parameters
  of R3's PHC pattern. It names the claude-ui and claude-agent digests only behind
  `authelia_k8s_manage_claude_user` and `authelia_k8s_manage_agent_user`.
  `ansible/tests/services/test_authelia_argon2_parameters_agree.py` exists, with
  `HASH_TASK_PODS` and `FLAG_TO_CONFIG_KEY`. mosquitto's `config-secret.yaml.j2` splits
  `mqtt_password_hash` on its first `:`, and `grep -c '^mqtt_password_hash:'` on
  `secrets.yml` prints 1. A search of authelia's role for `X_AUTHELIA_CONFIG_FILTERS` finds
  nothing, so the `{{` hazard begins with R2's PR 7.
    - D3.2a's character set gains `{{`, with a red fixture and a clean single `{`.
    - A new PR, D3.2d, adds R3's PHC-shape and distinctness refusal for the `_argon2` keys
      that `write` places, and a refusal of `:` in `mqtt_password_hash`. Each names the key
      only. The pattern's parameters join the agreement test as a named member. Each
      refusal has red and clean fixtures, and a malformed digest of a disabled user is
      clean, because `write` does not place it.
    - Every refusal runs over every referenced value before the first write, so a refused
      run changes no file. When D3.2c has landed first, the operator runs `write` on
      D3.2d's branch to prove the committed values pass.
    - *Rollback*, *Risks* (failure class 4), *Tests and docs*, *Complexity* and Open
      questions 6 and 7 follow. Open question 6 carries C3's `{{` wording. D5's
      preconditions and R4's successor rows are cross-slice notes.
- **X2-09 (medium). Substitution was decided per tag in C1 and H1 but per directory in R2
  and D3.** Fixed for D3's share. R2's design names the per-directory key set as the source
  and the Flux file as its projection, and C1 *Substitution and escaping* still says "A tag
  *substitutes*" and "The rendered Kustomization is the one source". D3.3b already read the
  predicate per directory, but it cited C1 as its source. It now names the unit, the
  in-process key set as the source and `spec.postBuild` as the projection. It states that
  `<tag>-deferred` is its own unit and that `cluster-vars` never substitutes. A new D3.3b
  fixture gives a `<tag>` directory with a placeholder and a `<tag>-deferred` directory
  without one, and only the first gets `postBuild`. The C1 rewrite, H1's flag, R2's
  amendment rows and X-12's reopening belong to contracts.md and H1 (cross-slice notes).

### Round integration (cross-slice integration review)

Five findings were raised: two high and three medium. D3's share of each is fixed here, and
none was refuted. Each claim was re-measured at `70591c307` before it was fixed. The parts
that belong to D2, D4, D5, R2, H1, H7, R4 and contracts.md went to them as cross-slice notes.

- **X-01 (high). D2's publish job cannot render bento-pdf: no stub file, and no source for
  `domain` before D3.** Fixed for D3's share. `grep -c '^domain: ENC\['` on `secrets.yml`
  prints 1, and the `ingressroute` macro in `ingressroute.yml.j2` builds every Host rule from
  `domain`. bento-pdf therefore substitutes before D3, and D3.3's "No role substitutes yet"
  was false. D3.3b now switches the routed leaves to `homelab-cluster-vars`, exit criterion 5
  loads bento-pdf after it, and D3.10 retires the pre-D3 source last so that D3.3b stays
  revertible. The publish line names placeholder mode. The D2 → R2 edge, the C3 source and
  D2's red proof are handed off.
- **X-04 (high). D3.3's non-tag `cluster-vars` Kustomization collides with D2's and D4's
  publish census and verify set.** Fixed. D3.3a owns the shared-kind layout:
  `out/flux/cluster-vars.yaml` with one object, `SHARED_KINDS` computed from the tree, the
  reserved name and red fixtures. `flux/cluster-vars` follows from D2's per-file rule and
  from D4's per-name rule alike. D1's Alert `commit-status` already posts every
  Kustomization's status. The timeout gains the hop. D5.1a1 extends the set, and the C1 and
  C3 amendments are Open question 6.
- **X-12 (medium). The `$${` escape rule is split between C1, H1 and R2.** Fixed for D3's
  share. One predicate per Kustomization directory, computed from the render, decides the
  escape, R2's checks and `postBuild`, and the renderer writes it once as `spec.postBuild`.
- **X-15 (medium). No slice plans rotation of the cluster age keypair, and two rebuild
  runbooks create `homelab-cluster-vars` by hand.** Fixed for D3's share. D3.4 adds the
  operator-only rotation procedure. It rests on `Decryptor.ImportKeys`, which imports every
  `.agekey` entry, read through `gh` at kustomize-controller v1.9.6. `check` reads the
  recipient from the creation rule, so rotation needs no code change. `docs/break-glass.md`
  gains a recovery-kit item. The registry does not track the key, because
  `secret_registry.py:registry_drift` reports a row with no `secrets.yml` key as stale.
  *Bootstrap material* says that only `sops-age` is made by hand.
- **X-16 (medium). D5 refutes D3's restart-trigger premise, and D3 is not updated.** Fixed.
  The premise is gone. D3 adopts D5's per-workload value under D5's name,
  `checksum/restart`. It covers every `manifests_extra_rollouts` entry, cloudflare-ddns's
  included, and a census refuses `owner: flux` for pihole and observability until D5.1e. The
  exit 3 command reads `checksum/restart`.

Found while fixing X-04: `.gitleaks.toml` allowlists only `secrets.yml`, so the cluster files
and the publish job's copies would meet the default rules that its comment says
false-positive on ciphertext. D3.2 extends the allowlist, and `check` asserts that every
value in those files is an `ENC[` string.

### Round 10 (per-slice review)

Nineteen findings were raised: six high and thirteen medium. All nineteen are fixed, and none
is refuted. Each claim was re-measured at `70591c307`, or read through `gh` at the pinned Flux
versions, before it was fixed. The integration entry above describes D3.3b's source switch
and D3.10. This round replaces both under C3's default.

- **D3-R10-01 (high). D3 hedged on the source of `domain` that C3 settles, and its switch,
  rollback, D3.10 and exit 5 rested on D2's `homelab-identifiers`.** Fixed. D3 adopts C3's
  default: D2.7 waits on D3.3b, *Unblocks* names D2.7, and D3.3b changes no live object.
  The source switch survives only under the operator-only literal alternative, where D3.3b
  replaces the literal. D3.10 is gone, because the default leaves nothing to retire. Exit 5
  is checked after D2.7 settles. D3.3b's rollback reverts D2.7 first. Open question 4 raises
  `homelab-identifiers` as a contract gap. That Secret would be rendered by the Ansible-owned
  flux role, and `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist`
  holds no `core/v1/Secret`, so retiring it is an operator-only delete.
- **D3-R10-02 (high). D3 claimed to introduce `SHARED_KINDS` in either order with D4.**
  Fixed. D3.3a waits on D4.3a, as the index says, and adds one literal member to the
  existing set, unconditionally. `check` and the render fail when the cluster file is
  missing. "Either order with D4.3" is gone, *Depends on* carries the D4.3a sub-edge, and the
  baseline describes the census that admits shared kinds. D3.3b changes the Secret that
  D2.6b's writer names rather than adding a second writer. The two D4 sentences the finding
  quotes are absent from D4 at this round: D4's step list has no D4.3a, and its header says
  that no PR of D3 waits on D4. The fix follows C3 and the index, and Open question 5 and
  two cross-slice notes ask D4 and the contracts maintainer to reconcile.
- **D3-R10-03 (high). D2.6c's source gate knew only `IDENTIFIER_SOURCE_KEYS`.** Fixed.
  D3.3b gives the gate the dotenv's key names less `sops_*`, read without decrypting, with a
  pair in `scripts/deploy_tools/tests/` whose named member is peanut's placeholder render.
- **D3-R10-04 (high). ansible-lint fails on manifests under `ansible/vars/`.** Fixed by
  exclusion. The `ansible-lint` hook in `prek.toml` matches `^ansible/.*\.ya?ml$`, so a move
  to another directory under `ansible/` would still be linted, and SOPS needs the files under
  `ansible/` to find `ansible/.sops.yaml`. D3.2a adds the path to the hook's `exclude` regex
  and to `.ansible-lint`'s `exclude_paths`, each with its reason.
- **D3-R10-05 (high). A per-role `decryption` field breaks C3's "No other field varies by
  role".** Fixed. Open question 6 requests the fourth field, derived from
  `render_sops_manifests`, gated before D3.3c, which emits it. D3.3c carries the fixture
  pair. kustomize-controller v1.9.6 `KustomizationReconciler.build` decrypts only when
  `spec.decryption` is set, which makes the field necessary.
- **D3-R10-06 (high). D3.2's CI gate landed before D3.4's rotation hook.** Fixed. The secret
  rotation hook and `TRACKED` move to D3.2b, which lands and applies before D3.2c commits the
  files and the gate. D3.2c requires that no rotation PR is open. `TRACKED` gains the paths
  only once the file exists, because `git add` fails on a pathspec that matches nothing.
  *Risks* names the window as failure class 5.
- **D3-R10-07 (medium). No tool waited on the Flux half of a cluster-secret PR.** Fixed.
  D3.4 makes `land-wait` wait on `cluster-vars` and its dependents for such a PR, with a
  pair, and names no key, in keeping with `land_tags.py`'s `DECIDED: fire on ANY change`
  marker. *Verification* gives the statuses read for D3.3a, D3.3b, D3.4 step 3 and D3.9.
- **D3-R10-08 (medium). Decrypted SOPS manifests went through substitution.** Fixed. `write`
  adds `kustomize.toolkit.fluxcd.io/substitute: disabled` before encrypting, because the SOPS
  MAC covers the plaintext fields. The renderer copies the file byte for byte and refuses one
  without the annotation. `check` asserts it. Confirmed in fluxcd/pkg
  `kustomize/kustomize_varsub.go` (`DisabledValue`) at `kustomize/v1.35.7`, the version
  kustomize-controller v1.9.6's `go.mod` pins. Open question 6 names the C3 rule.
- **D3-R10-09 (medium). Host recipient changes had to cover three rules.** Fixed. D3.1
  rewrites the `.sops.yaml` header and `sops_setup/CLAUDE.md`, and `check` compares each
  file's recipients with its rule's full list, with a flagged fixture. D3.1 notes the
  overlap with H4.
- **D3-R10-10 (medium). Rotation missed consumers outside Flux.** Fixed. Every peanut key has
  a second reader, homepage or nut, so no single-consumer drill exists. D3.9 also redeploys
  homepage. D3.4 drops flux-owned tags from `consumer_commands`' `deploy.sh` line and from
  `rotate --deploy`, with a pair. *Risks* hands the cross-plane rotation gap to D5.
- **D3-R10-11 (medium). D5's half of the X-16 fix was not updated.** Fixed by a cross-slice
  note to D5. D5.1e narrows to the `manifests_self_rollouts` entries, drops
  `checksum/role-config`, and takes cloudflare-ddns off its named members, because D3.3c's
  test owns that case.
- **D3-R10-12 (medium). `.source` was not on the gitleaks allowlist.** Fixed with an
  allowlist scoped to `generic-api-key`, in the form of the `docs/facts.lock` entry.
- **D3-R10-13 (medium). CI had no `sops` or `age`.** Fixed. D3.2a installs both in the
  `pytest` job, `sops` checksum-verified at the `sops_setup` pin. A pin test holds the two
  equal, and the `write` tests fail rather than skip without the binaries.
- **D3-R10-14 (medium). Stub mode's `checksum/restart` was undefined.** Fixed. Both modes
  hash the placeholder-form documents and `.source`, and D3.3c tests that the two renders
  carry the same value.
- **D3-R10-15 (medium). D3.3a went live without checking `sops-age`.** Fixed with an
  operator-only precondition: the derived public key matches the cluster rule, and the
  committed file decrypts with that key alone.
- **D3-R10-16 (medium). A template edit never reached the shipped manifest.** Fixed. `check`
  compares each manifest's plaintext structure with the stub-mode render of its template,
  because placeholder mode renders none. `write` regenerates on a mismatch. A renamed data
  key is the flagged fixture.
- **D3-R10-17 (medium). The compromised-key mitigation overstated the rotation.** Fixed.
  *Risks* says that a key rotation protects future revisions only, and that a compromise
  also needs every key in the file rotated (operator-only).
- **D3-R10-18 (medium). New D3.3b behaviours lacked red proofs.** Fixed. The restart and
  manifest half moves to D3.3c with pairs for the manifest copy and `decryption`, and the
  outside-role reference test with peanut as its named member. D3.3b carries the timeout
  hop pair.
- **D3-R10-19 (medium). A refused value had no resolution path.** Fixed as Open question 7
  and in D3.2c: the operator rotates the value, redeploys through Ansible and reruns `write`.

PR identifiers that other slices cite keep their meaning: D3.4 still owns the cluster key
rotation, which H7 cites, and D3.5 and D3.6 are unchanged. D3.2 is now D3.2a to D3.2c, and
the `CLAUDE.md` sentence that G2 and G5 cite under D3.2 sits in D3.2c.

### Round c1 (per-slice review)

Five findings were raised: one high and four medium. All five are fixed, one of them by a
different route than the finding proposed, and none is refuted. Each claim was re-measured
at `70591c307`, or read through `gh` at getsops/sops v3.13.3, before it was fixed. This round
supersedes the rationale of D3-R10-03 and D3-R10-04 above.

- **D3-C1-01 (high). The dotenv cluster file escaped the protected-edits hook, and the
  widened gitleaks allowlist left only a post-push check.** Fixed.
  `.claude/hooks/block-protected-edits.py:is_sops_encrypted` matches only `_SOPS_MAC_RE`, the
  YAML and JSON `mac:` form, and sops v3.13.3 writes dotenv metadata as `sops_`-prefixed
  `key=value` lines (`SopsPrefix` in `stores/metadata.go`, `EmitPlainFile` in
  `stores/dotenv/store.go`). D3.2a adds the dotenv pattern with an `_is_clean` and
  `_is_flagged` pair. `block-protected-bash.py` loads `classify` through `_load_edits_hook`,
  so it needs no second pattern, and one Bash-arm test confirms the `ask`. D3.2a also adds
  the `cluster-vars-ciphertext` pre-commit hook and rewrites the `.gitleaks.toml` comment to
  name each path's guards. `check` gains the MAC proxy, with the finding's red fixture, and
  every failure message names `write` as the only fix. The proxy compares lines outside the
  SOPS metadata, so a `sops updatekeys` that changes only recipient lines does not trip it.
  `EncryptTree` in `cmd/sops/common/common.go` moves `LastModified` and the MAC on every
  write. *Risks* names the proxy's blind spot.
- **D3-C1-02 (medium). SOPS finds `.sops.yaml` from the working directory.** Fixed.
  `findConfigFile` in `cmd/sops/main.go` calls `config.LookupConfigFile(".")`, and
  `parseCreationRuleForFile` in `config/config.go` matches the path relative to the config
  directory, first rule wins. `write` passes `--config`, with a test run outside `ansible/`.
  The cluster rules are unanchored and sit ahead of the generic rule. `check` picks the rule
  as SOPS does, with a fixture that the generic rule would also match. The D3.1 runbook and
  D3.4 step 2 say to run from `ansible/` or with `--config`, and the *Lint* rationale is
  corrected.
- **D3-C1-03 (medium). D3.3a's "that key alone" decrypt passed on any recipient host.**
  Fixed. `loadIdentities` in `age/keysource.go` opens `<UserConfigDir>/sops/age/keys.txt`
  whenever it exists, and `getUserConfigDir` defers to `os.UserConfigDir` on Linux, which
  honours `XDG_CONFIG_HOME`. *Verification* holds both operator-only commands: an empty
  `XDG_CONFIG_HOME`, the two other key variables unset, and an unrelated key that must fail
  first.
- **D3-C1-04 (medium). D3.3b and the Baseline described a D2 interface that D2 had
  withdrawn.** Fixed. `d2-first-leaf.md` withdraws `homelab-identifiers`, names
  `homelab-cluster-vars` from D2.6b on, and says the gate does not check keys "a third
  time". D3.3b drops the no-op Secret change, the source-gate key set and its pair, and
  keeps the `cluster-vars` edge and the timeout hop. The Baseline, Open question 4, the tree
  diff, *Verification*, *Rollback*, the docs line and *Complexity* follow.
- **D3-C1-05 (medium). The rotation cron ran `write` in the primary checkout with no plaintext
  rule and no check before the commit.** Fixed, with the check placed differently.
  `write` sets values through `rotation_tools.py:sops_set` (`--value-stdin`), encrypts a
  manifest from stdin with `--filename-override` and `--config`, and writes only
  ciphertext. It writes onto the target rather than renaming a temporary file into place,
  because a temporary file left by a crash would be an untracked file in the primary
  checkout, which `revert()` does not remove. Tests cover the no-plaintext and
  failed-encrypt cases. The finding placed `check` after `rotate` and before `git add`.
  `secret_rotation.py:cmd_rotate` deploys inside `rotate --commit --deploy`, and the cron's
  commit-guard comment says a live value must not be reverted. `check --no-render` therefore
  runs in process after `write` and before the deploy, where a failure reverts with nothing
  live.
