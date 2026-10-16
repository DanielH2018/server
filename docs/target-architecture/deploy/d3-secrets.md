# D3 — Secrets through Flux

Status: DONE

All five exit criteria held on 2026-10-16:

1. D3.6's value-hash comparison printed `match` for all three of peanut's keys.
2. The operator's second key-name read, after D3.8, printed `WEB_PASSWORD WEB_USERNAME
   settings.yml`, without `FLUX_CANARY`.
3. D3.9's rotation moved peanut's restart checksum, and peanut rolled in the same revision.
4. `test_a_secret_changed_since_the_cluster_file_was_written_is_flagged` and the
   `cluster_vars.py check` prek hook refuse a stale cluster file.
5. bento-pdf's Kustomization carries no `postBuild` and no `dependsOn`, so it takes nothing
   from `homelab-cluster-vars`.

D3.0, the cluster key: the operator generated it on 2026-10-15, stored the private half in the
password manager, and created `flux-system/sops-age` from it. Deriving the public key from the
stored Secret printed `age1wf2js7pakyty3qgn3mxwuz8gc277jdgd3guy283cmphgxdj62qeqk6tmnc`.

D3.1, recipients, runbooks and the ADR: [#5121](https://github.com/DanielH2018/server/pull/5121).
The operator accepted [ADR-0022](../../adr/0022-the-cluster-decrypts-its-own-file-with-its-own-key.md)
and the cluster recipient in its review on 2026-10-15.

D3.2a, `cluster_vars.py` and its guards: [#5133](https://github.com/DanielH2018/server/pull/5133).
It adds no cluster file and no tree gate. Its fix round pinned the `sops_` metadata to an exact
name set with value shapes, and refuses repeated keys by line number.

D3.2b, the rotation hook: [#5138](https://github.com/DanielH2018/server/pull/5138). Its
landing read `settled` at `9ca696963`, and the deployer rewrote `/usr/local/bin/secret-rotate.sh`
with the three cluster paths at 19:59 UTC on 2026-10-15. To break the import cycle the hook
would have made, `sops_set` moved to the leaf `sops_io.py`, and `rotation_tools` re-exports it.

D3.2d, the successor refusals: [#5137](https://github.com/DanielH2018/server/pull/5137). It
landed before D3.2c, so its operator-only `write` step did not apply.

D3.2c, the first files and the tree gate: [#5140](https://github.com/DanielH2018/server/pull/5140).
The operator ran `cluster_vars.py write` on its branch at 20:11 UTC on 2026-10-15. It refused
no value and wrote the dotenv, `cluster-vars.source` and four SOPS manifests, then handed the
landing to the session. The file holds every cluster-referenced key from the start, and the
operator confirmed that answer to Open question 2 the same day.

D3.3a, the `cluster-vars` kind: [#5145](https://github.com/DanielH2018/server/pull/5145). The
operator's precondition printed `public key: match`, `unrelated: 128` and `cluster: 0`. Its
first promotion, `9ae59f789`, read `flux/cluster-vars` `success` at 22:07:24 UTC, and the
Kustomization read `True ReconciliationSucceeded`. Flux decrypts the cluster file with
`sops-age`.

D3.3b, the `cluster-vars` edge: [#5156](https://github.com/DanielH2018/server/pull/5156). Inert,
as planned: after its promotion, `c452fffda`, `flux/cluster-vars` still read `success`.

D3.3c, restart and SOPS-manifest wiring: [#5166](https://github.com/DanielH2018/server/pull/5166).
It applied Open question 6's two C3 amendments: the per-role `decryption` field and the SOPS
manifest's `substitute: disabled` annotation. Inert, as planned: after its promotion,
`9f49b5541`, bento-pdf still ran ReplicaSet `865c4b8dd4`, its pod template carried no
annotation, and all three Flux statuses read `success`.

D3.3d, the in-cluster type proof: [#5171](https://github.com/DanielH2018/server/pull/5171). The
`sops-manifest-type-proof` job passed on its first run. In a kind cluster, `0123` and
`9007199254740993` in quoted `stringData` each decoded to exactly its digits. The
int-encrypted copy failed to apply with `.stringData.long_digits: expected string`, and the
API server had already read its 16-digit value as `...992`. The `data:` remedy is not needed.

D3.4, rotation runbooks and the wait on `cluster-vars`: [#5176](https://github.com/DanielH2018/server/pull/5176).
`land.sh` waits on promotion for a PR that changes a cluster file, and `land-wait` adds
`cluster-vars` and its live dependents. `secret_rotation.py consumers` prints the Flux path
for an `owner: flux` consumer, which no live role exercises yet, because bento-pdf reads no
secret. Review found that the compromise guidance named only the cluster file. A follow-up
added the cluster manifests to it, here and in both docs.

D3.5, evidence before the cutover (operator-only). On 2026-10-16 the operator read
`peanut-secrets`, `ical-proxy-env` and `qbittorrent-mullvad-account`. Each had one manager,
`kubectl-client-side-apply`, and carried `last-applied-configuration`, so Flux takes over
every key. The reads are in D3.6's PR body.

D3.6, the flip: [#5193](https://github.com/DanielH2018/server/pull/5193). The operator ran
the two-writer check at the PR's head, because the classifier refuses `deploy.sh` from an
agent: `--tags peanut` exited 5 with `DEPLOY-VERDICT: flux-owned`. The red draft #5189 failed
CI on `check`'s names assertion. After the merge, `fea20962d`, the deployer dropped peanut as
flux-owned and applied nothing. The `peanut` Kustomization read Ready, `probe.py health
peanut` passed, and the PeaNUT login page rendered. The operator's value-hash comparison
printed `match` for all three keys, which is exit criterion 1.

D3.7 and D3.8, the canary: [#5194](https://github.com/DanielH2018/server/pull/5194) and
[#5195](https://github.com/DanielH2018/server/pull/5195). After D3.7 settled, the operator's
key-name read listed `FLUX_CANARY` beside the three real keys. D3.8 settled as `2dd6d4f92`,
and its render matched D3.6's, restart checksum included. The operator's second read, on
2026-10-16, printed `WEB_PASSWORD WEB_USERNAME settings.yml` without `FLUX_CANARY`, which
closes exit criterion 2.

D3.9, the rotation drill: [#5218](https://github.com/DanielH2018/server/pull/5218). The
operator ran `sops set` on `peanut_password` and `cluster_vars.py write`, and neither printed
a value. `secret_rotation.py record` moved the key's `last_rotated` to 2026-10-16. The first
landing went red on a shard-weight gate that depends on the runner, filed as #5219. After a
re-run it ended `needs-manual-apply`, because a `secrets.yml` change maps to no tag, and
promotion applied `b07b33061`. peanut's `checksum/restart` moved from `2a78e014…` to
`cae737ec…`, and peanut rolled to ReplicaSet `6cff78674` at 12:46:25 UTC.
`flux/cluster-vars` read `success` at 12:45:55 and `flux/peanut` at 12:46:40. The Ansible
half, `./scripts/deploy.sh --tags homepage`, printed `DEPLOY-VERDICT: deployed`, and
homepage's PeaNUT widget logged in with the new value. That closes exit criterion 3.

Depends on: [D2](d2-first-leaf.md), [R2](../render/r2-secret-placeholders.md). D3.0 to D3.3c
need only D2.1 to D2.6c, which include D2.6b's empty `SHARED_KINDS` and census, as the slice
index says. D3.4 to D3.9 need all of D2.

Unblocks: [D5](d5-bulk-move.md). PR D2.7 of [D2](d2-first-leaf.md) does not wait on D3,
because the operator ruled on 2026-10-14 that `domain` is published (C3).

## Goal and exit criterion

After D3, Flux renders a flux-owned role's Secrets by substituting values from one Secret,
`homelab-cluster-vars`. kustomize-controller builds that Secret by decrypting
`ansible/vars/cluster-vars.sops.env` with an age key that only the cluster holds. CI holds no
key, yet proves the file is in step with `ansible/vars/secrets.yml`. A changed secret value
restarts the pods that read it, through a checksum of ciphertext, never of plaintext. `peanut`
is the first role with a Secret that Flux owns. Every flux-owned tag whose render holds a
secret placeholder substitutes it from the same Secret. `domain` is published and is not one
of those placeholders, so D2.7 flips bento-pdf with no part of D3, and no tag ever
substitutes from another source. R2's four SOPS-encrypted manifests get their
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
5. **D2's leaf takes nothing from this Secret.** After D2.7 settles, bento-pdf's
   Kustomization has no `postBuild` and no `cluster-vars` edge, because `domain` is
   published, and the bento-pdf page loads through Traefik.

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
- **Every route carries `domain`, which is published.** The operator ruled on 2026-10-14
  that `domain` is not secret. At the baseline `grep -c '^domain: ENC\['` and
  `grep -c '^email: ENC\['` on `secrets.yml` each print 1. The `ingressroute` macro in
  `ansible/templates/ingressroute.yml.j2` builds each Host rule as `hostname ~ '.local.' ~
  domain`. bento-pdf and texbrain take their route from
  `ansible/templates/ingressroute-default.yaml.j2`, and peanut's own
  `templates/ingressroute.yaml.j2` imports the same macro. Until domain PR 2b of R2's
  *Publish `domain`* item, R2's placeholder mode renders `${domain}`, which R2's
  `PLACEHOLDER_ANYWHERE` allows outside a Secret. From domain PR 2b on, every route renders
  the literal. `homelab-cluster-vars` never holds `domain`, and D2.7 waits on no PR of D3. D2
  has withdrawn the Secret `homelab-identifiers` that its D2.6a once added, so no other
  source of `domain` exists to retire.
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
  consumer. `land_tags.py` carries the marker `DECIDED fire on ANY change to secrets.yml,
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
- **R2 hands D3 four documents.** Each is listed in a role's `render_sops_manifests` and
  holds one key from `SOPS_MANIFEST_KEYS`: the three multi-line keys
  `pi_peer_backup_ssh_key`, `authelia_oidc_rsa_key_content` and
  `google_assistant_service_account`, and, from R2's PR 7b, `mullvad_account`, whose
  all-digit value cannot pass through a placeholder (R2 Open question 13). Placeholder mode
  emits no document for them, and stub mode still renders them. D3 supplies each as a
  SOPS-encrypted manifest. R2's round trip excludes all four.
- **The Ansible prune list names no Secret.**
  `ansible/roles/k8s/manifests/defaults/main.yml:manifests_prune_allowlist` holds no
  `core/v1/Secret`, so dropping a Secret's template leaves the live Secret in place.
- **Plans D3 builds on**, not the tree:
    - D1's Alert `commit-status` sends every Kustomization event to the GitHub provider, with
      context `flux/<name>`, so any Kustomization in the bundle posts its own status.
    - The publish census admits the flux-owned tags plus the members of `SHARED_KINDS`, a
      closed literal set that ships empty with a reserved-name check. C3 and D2's plan put
      that in D2.6b. D4.3 adds the rule for the documents a tag's file holds.
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
          Kustomization peanut, ...  dependsOn cluster-vars
            postBuild.substituteFrom homelab-cluster-vars ─▶ Secret homelab/peanut-secrets
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
  That set includes `email` and leaves out `domain`, which is published. Each bulk-move PR
  in D5 therefore needs no regeneration.
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
      Placeholder mode renders no document for these templates, and stub mode does (R2);
    - every `data` or `stringData` value in a manifest is `ENC[...]` with `type:str`. SOPS
      records each value's type in plaintext inside that string, and a value encrypted from
      an int decrypts as an int. `mullvad_account` is `type:int` in `secrets.yml`, so
      `write` must encrypt its manifest value as a string, and this assertion fails a
      manifest where it did not.

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
    - It refuses the value of a key with a standalone use, a placeholder that is the whole
      scalar of a field, when R2's resolver in `scripts/lib/plain_scalar.py` types it as
      anything but a string. That is C3's standalone value rule. Flux types such a value by
      its text, whatever quotes the template wrote (R2 *The standalone-value guard*).
      `write` reads R2's per-directory standalone key sets in process, as it reads the key
      sets, and imports the same resolver as R2's deploy-path guard, so the two producers
      cannot disagree. Like the other refusals, it names the key and never the value. D3.2a
      adds it.
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
  Nothing uses this until pi-peer-backup, authelia, home-assistant and qbittorrent move in
  D5.
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

### What one secret costs

A secret lives in up to five stores (F-09). What reads the secret decides which stores it
touches, and each store has its own writers:

- **`ansible/vars/secrets.yml`** holds every secret. The add-secret author writes a value
  there with `sops set`, and the rotation cron rewrites a rotated one. The two cluster
  files derive from it. H4 states how the per-host files relate to it.
- **`cluster-vars.sops.env`** holds each value that a cluster placeholder substitutes. The
  `cluster-vars` Kustomization turns it into the Secret `homelab-cluster-vars`. Only
  `cluster_vars.py write` writes the file: the operator (**operator-only**), or the rotation
  cron for a rotated value (D3.2b).
- **A SOPS manifest under `ansible/vars/cluster-manifests/`** holds the values in
  `SOPS_MANIFEST_KEYS`, one manifest per `render_sops_manifests` entry. `write` writes it,
  with the same two writers.
- **A per-host SOPS file** holds the values its host reads, and the host package carries it
  (C5). H4 owns its writer.
- **`sops-age`** holds the cluster's private age key and no application value, so adding a
  secret never touches it. Creating or replacing it is **operator-only** (D3.0, D3.4).

`cluster-vars.source` is evidence, not a store. `write` rewrites it with the cluster files.

The cost of one secret follows from the list:

- A secret that only Ansible reads touches `secrets.yml` alone. Its author needs no operator
  step.
- A secret that the cluster reads touches `secrets.yml` and one cluster file. The cluster
  file needs the operator's `write`, so an agent that adds such a secret stops before the PR
  and hands `write` to the operator (D3.4).
- A secret that a host and the cluster both read touches three stores, and each store
  changes through its own writer.

Open question 8 asks whether a cluster secret that no file in the tree holds could skip the
operator's step.

### Where the `cluster-vars` Kustomization is published (D3.3a)

`cluster-vars` is the first shared kind, a Kustomization that is not a deploy tag. The rule
that admits it exists before D3: C3 and D2's plan have D2.6b ship `SHARED_KINDS` empty, with
the allowlist census and the reserved-name check. D3.3a waits on that PR, as part of D2.1 to
D2.6c, and adds one literal member, `cluster-vars`. It writes no census or reserved-name
logic of its own.

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
- **The bundle can carry `cluster-vars` before any tag is flux-owned.** D3.3a may land
  before D2.7, because D2.7 no longer waits on it. Then D2's publish step still writes the
  root alone, and D3.3a changes that branch: with no flux-owned tag, the step runs `homelab_render.py cluster --out out
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
- **Every substituting Kustomization depends on `cluster-vars`.** A route does not make a
  directory substitute, because `domain` is published and renders as a literal. The set is
  non-empty for a directory with a secret reference. A directory with no secret reference
  gets neither field, so a literal `${` in it reaches the cluster unescaped.
- **The source gate stays as D2 wrote it.** D2.6c's gate admits a `substituteFrom` Secret
  only when a Kustomization in the bundle generates it, and from D3.3a on `cluster-vars`
  does. The gate does not check which keys that Secret holds. R2's rules 1, 2 and 4 put
  every reference in a substituting tag in R2's placeholder set, and `check` holds the
  dotenv's key names equal to that set less `SOPS_MANIFEST_KEYS`. D3.3b adds no key set to
  the gate.
- **No flux-owned tag changes source.** D2.7, the first flip, renders `domain` as a literal
  and waits on no PR of D3. bento-pdf holds no placeholder, so it gains neither `postBuild`
  nor the `cluster-vars` edge here. D3.3b changes no live object, because the only flux-owned
  tag at D2.7 is bento-pdf, which holds none.
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
  on `cluster-vars`. It names no changed key, so it keeps to the `DECIDED fire on ANY change
  to secrets.yml` marker in `land_tags.py`. This rule is interim. C6 has `land.sh` read its
  changed set from `digests.json` and derive no tags itself (D6), and C1 counts the
  `cluster-vars` files in every substituting tag's `footprint`. The rule retires in the PR
  that makes that switch.

### Tree diff

```diff
  ansible/.sops.yaml                         + two creation rules, ahead of the generic one;
                                               the header's add and remove runbook covers all three
  ansible/roles/setup/sops_setup/CLAUDE.md   onboarding names all three rules
+ ansible/vars/cluster-vars.sops.env
+ ansible/vars/cluster-vars.source
+ ansible/vars/cluster-manifests/            four SOPS-encrypted Secret manifests (R2's)
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
2. **D3.2a — `cluster_vars.py` and its guards** (about 300 lines, over the guide because the
   guards must land in the same PR as the allowlist that relies on them). Inert: no tree
   file and no tree gate. The 40 lines beyond the earlier estimate are C3's standalone value
   refusal in `write`, which imports R2's resolver, and `check`'s `type:str` assertion on
   manifest values, each with its pair under *Verification*.
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
6. **D3.3a — The `cluster-vars` kind** (about 120 lines). Waits on D2.6b, which D3's header already covers.
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
    - **Inert for the cluster**, because bento-pdf, the only flux-owned tag at D2.7, holds
      no secret placeholder. After it is promoted, `flux/cluster-vars` still reads
      `success`. D2.7 does not wait on this PR, and exit criterion 5 is checked after D2.7
      settles.
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
    - **D3.3d — The in-cluster proof that an all-digit value stays a string** (about 100
      lines: the CI job, its fixture tree and its red half), after D3.3c, whose manifest
      copy and `decryption` field it exercises. It is R2's cross-slice note. R2 PR 7b moves
      `mullvad_account`, whose real value is all digits, into a dedicated Secret that this
      path delivers. Nobody has measured what kustomize-controller does with a decrypted
      all-digit value in a quoted `stringData` field. D3.3d adds a CI job,
      `sops-manifest-type-proof`, that measures it in C8's throwaway kind cluster.
        - The job creates the cluster, installs the pinned Flux controllers, and creates
          `sops-age` from an age key it generates. `write`'s manifest code, run on a fixture
          tree encrypted to that key, builds one SOPS-encrypted Secret whose quoted
          `stringData` fields hold `0123` and a 16-digit value.
        - A Kustomization with `decryption` applies it. The Kustomization must reach Ready,
          and each value in the live Secret's `data` must decode to exactly its digits.
        - The red half applies the same Secret with its values encrypted from an int, so
          SOPS records `type:int`. It must fail to apply or come back changed. If it passes,
          the job cannot see the failure it exists to catch, and it fails.
        - *Why kind suffices.* The question belongs to kustomize-controller's decryption and
          to the API server's validation of `stringData`, not to k3s or Longhorn. The job
          runs the same pinned controller images as the cluster.
        - The job runs on a change to the flux pin, the sops pin, `cluster_vars.py` or the
          job itself.

      D5's flip of qbittorrent waits on a green run. If the proof fails, `write` emits that
      value under `data:`, base64-encoded, and `check`'s structure assertion compares
      data-key names across `data` and `stringData`. R2 *`mullvad_account` becomes a
      dedicated Secret* gives the reason the remedy holds.
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
      stops before the PR and hands `write` to the operator. The skill lists the stores
      from *What one secret costs*, so the author knows before starting which ones the
      secret touches.
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

# Exit 5, after D2.7 settles: bento-pdf substitutes nothing and routes.
kubectl -n flux-system get kustomizations.kustomize.toolkit.fluxcd.io bento-pdf \
  -o jsonpath='{.spec.postBuild.substituteFrom[*].name}{" "}{.spec.dependsOn[*].name}'
# Prints one space and nothing else. Then load bento-pdf.local.<domain> with
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
- `write` refuses `0123`, `yes` and `1e3` for a fixture key with a standalone use, and
  passes the same values for a fixture key whose placeholder sits inside other text.
  `check` flags a fixture manifest whose value carries `type:int`.
- The `sops-manifest-type-proof` job's red half must fail to apply a Secret encrypted from
  an int (D3.3d).
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
- A placeholder missing from the dotenv fails `check`'s names assertion. R2's `known-key`
  rule backstops a renderer bug only: no template can produce a `${NAME}` outside
  `secrets.yml`, because the renderer writes `${K}` only for a key `secret_keys()` reads and
  escapes every other `${`. D3 adds no source-gate key set.
- The timeout pair fails when a bundle with a substituting tag derives D2's timeout unchanged.
- The SOPS manifest pair fails when a role without `render_sops_manifests` gets `decryption`,
  and when a manifest without the annotation renders.
- The outside-role reference test fails on its fixture role and must resolve peanut's three
  references. The self-rollouts census fails on pihole's entry marked `owner: flux`, and
  when either pihole or observability is missing from the members it finds in the tree.
- The `consumers.py` pair fails when a flux-owned tag stays on the `deploy.sh` line, and the
  `land-wait` pair fails when a cluster-file PR adds no wait.
- Strict substitution is the live red proof that a key is missing. Before D3.6, a draft branch
  of peanut's template that names a key `secrets.yml` holds and the cluster file lacks fails
  `check`'s names assertion in CI
  (`test_cluster_vars_tree.py::test_the_committed_cluster_files_pass_check`). That draft is
  never merged. D3.6's draft, #5189, failed exactly there.
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
- **D3.3d** adds only a CI job, whose kind cluster and age key are throwaway and die with
  the run. A revert changes no live object. It also removes the proof that D5's qbittorrent
  flip waits on, so that flip cannot merge until the job is restored.
- **D3.3b.** Revert D3.6 first if peanut is flux-owned. Without the edge, a substituting tag
  can reconcile before `cluster-vars` applies a rotated value. On a rebuilt cluster its
  build fails until `homelab-cluster-vars` exists. D2.7 does not depend on D3.3b, so its
  handback is not a step here.
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
  in git and every published `sha-<sha>` artifact holds a copy of the cluster file and of
  each cluster manifest. A suspected compromise also needs every key that
  `cluster-vars.source` lists rotated (**operator-only**), the manifest-held keys included,
  and its consumers redeployed. `docs/secret-rotation.md` and `docs/break-glass.md` state
  that distinction in D3.4.
- **Every substituting tag depends on decryption.** A secret placeholder puts a tag behind
  `cluster-vars`, so a broken `sops-age` stops every substituting flux-owned tag from
  reconciling. A route alone does not, because `domain` is published.
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
  *What one secret costs* says which secrets need the step. Open question 8 proposes
  `seal`, which would drop the step for a secret that no file in the tree holds.
- **The checksum misses a mount it cannot see** (failure class 2). A pod that reads a Secret
  outside its own role is not stamped. Mitigation: D3.3c's outside-role reference test, with
  peanut as its named member and a red fixture.
- **A self-rolled workload loses its restart** (failure class 2). Mitigation: the
  self-rollouts census refuses the flip until D5.1e covers the role.
- **Substitution differs from the CLI.** R2 proves the round trip with `flux envsubst`. Exit
  criteria 1 and 3 are the in-cluster proof for peanut. D5 repeats exit 1 for each role it
  moves. A SOPS manifest is decrypted before substitution, so its annotation keeps a
  `${` in a key out of substitution.
- **A value's YAML type changes on the way in** (failure class 2). On the pinned CLI, a
  value substituted alone into a field loses its string type whatever quotes the template
  uses (R2 *Baseline*). A SOPS manifest value encrypted from an int decrypts as an int.
  Mitigation: `write` refuses a value of a key with a standalone use that R2's resolver
  types, as C3's standalone value rule requires. `check` requires `type:str` on every
  manifest value. D3.3d's `sops-manifest-type-proof` job shows in a kind cluster that an
  all-digit value in a quoted field of a SOPS-encrypted Secret applies as a string.
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
- D3.3d: `.github/workflows/ci.yml` gains the `sops-manifest-type-proof` job, with its
  fixture tree beside the `cluster_vars.py` tests in `scripts/secrets_mgmt/tests/`.
  `docs/flux-deploy-plane.md` records the measured result on the SOPS manifest path, and
  which remedy applies if the job ever goes red.
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
- **Added for R2's standalone value rule (R2 Open question 13):** about 40 lines in D3.2a
  for the standalone value refusal in `write` and `check`'s `type:str` assertion, with their
  pairs. The resolver itself is R2's, imported, not counted here. D3.3d adds one CI job,
  `sops-manifest-type-proof`, at about 100 lines with its fixture and red half. It costs
  one kind cluster per run, and it runs only when the flux pin, the sops pin,
  `cluster_vars.py` or the job changes.
- **Not added:** a key set in D2.6c's source gate. D2 declines it, and `check` with R2's
  rules covers it. D3 also adds no second writer of cluster manifests. Open question 8
  proposes one, `seal`, at about 120 lines with its tests.
- **Removed:** for each flux-owned role, `verify_secret_keys.yml` no longer runs. R4 deletes
  it once no role is Ansible-owned. `domain` is published and has no interim source, so D3
  has none to retire.

## Open questions

1. **Who owns the ADR-0003 amendment, D3 or H4?** design.md assigns it to both. Resolution:
   whichever lands first writes it, and the other extends it. This is listed as a contract
   gap.
2. **Should the cluster file hold every cluster-referenced key from the start, or only the
   flux-owned roles' keys?** This plan holds every key, so that D5 needs no regeneration.
   That places values for roles that Ansible still deploys inside the cluster Secret.
   Resolution: the operator confirms in the D3.2c review (**operator-only**). The operator
   confirmed it on 2026-10-15: the cluster file holds every cluster-referenced key.
3. **Does `sops set` work on a dotenv file with the pinned sops version?** Resolution:
   D3.2a's tests run `write` against a throwaway key with the pinned binary, which its CI
   step installs.
4. **Resolved: the source of `domain`, and whether it should need decryption.** The
   operator ruled on 2026-10-14 that `domain` is published: "domain is not secret, it's a
   var for flexibility just in case of future changes." The renderer writes it as a
   literal, and `homelab-cluster-vars` does not hold it.
    - D2 has withdrawn D2.6a and its Secret `homelab-identifiers`. Nothing remains to
      retire.
    - D2.7 waits on domain PR 2b of R2's *Publish `domain`* item and on no PR of D3.
    - Routed tags without a secret stay off the `cluster-vars` edge. `email`, the other
      key the question named, stays a secret placeholder.

   Resolution: settled by the ruling.
5. **Resolved: what is `SHARED_KINDS`'s origin?** D2.6b ships the empty set, the census and
   the reserved-name check. D4 has no D4.3a (D4-R10-01), and C3 credits D2.6b. D3.3a waits on
   D2.6b through D2.1 to D2.6c and on no PR of D4.
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
    - **Applied.** C3's *Shared kinds* names D3.3a. The slice index has no D4.3a edge, and
      D2.7 no longer waits on D3.
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
8. **Could a cluster secret that no file in the tree holds skip the operator's `write`?**
   (F-09) SOPS encrypts with the public recipients in `ansible/.sops.yaml` alone, and
   `write` already encrypts each manifest from stdin with `--filename-override` and
   `--config`. Only a change to a file that is already encrypted needs that file's data key.
   Such a secret could therefore ship as its own SOPS manifest, through C3's second path.
   Only a value that a placeholder substitutes would then need the operator's `write`. This
   plan does not adopt it, for two reasons:
    - C3 sends only `SOPS_MANIFEST_KEYS` down the second path. R2's `sops-manifest-entries`
      check fails an entry whose template names any other secret. A single-line value in a
      manifest changes both.
    - CI cannot compare a manifest's plaintext with `secrets.yml`, so the two values would
      agree only by construction.

   Recommendation: adopt it as a `cluster_vars.py seal` mode, one PR of about 120 lines with
   its tests, after D3.3c.
    - `seal` reads the value from stdin once. It passes the value to the same `sops set` that
      the add-secret skill runs, through `rotation_tools.py:sops_set`. It then encrypts the
      manifest from the same bytes, and writes the `.source` line from the public ciphertext.
    - It refuses a key that `secrets.yml` already holds, a key that any placeholder
      references, and a manifest path that already exists. A value added to an encrypted
      manifest needs that manifest's data key, so that case stays with `write`.
    - It applies `write`'s value refusals in memory. A refused run adds and changes no file.
    - `check`'s failure message names `seal` beside `write` for a manifest that is absent
      from the base.
    - Red proof: a test runs the manifest encrypt with no age identity reachable, with
      `SOPS_AGE_KEY_FILE` unset and `XDG_CONFIG_HOME` on an empty directory, and the encrypt
      succeeds. Its control runs `sops decrypt` on the output under the same environment,
      which must exit non-zero, as D3.3a's unrelated-key check does. A third test decrypts
      the fixture manifest with the throwaway key and finds the input value.
    - The next `write`, by the operator or the rotation cron, decrypts both files and
      regenerates a manifest that differs from `secrets.yml`. Drift between the two lasts
      at most until then.

   `cluster_vars.py write` stays **operator-only**. Resolution, before the PR opens: the
   contracts.md maintainer decides a request on C3's second path, R2's owner rules on
   widening `SOPS_MANIFEST_KEYS` or adding a second set beside it, and the operator accepts
   equality by construction (**operator-only**).

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
  pair, and names no key, in keeping with `land_tags.py`'s `DECIDED fire on ANY change`
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

### Pillar review fold (2026-10-12)

Folded from the 2026-10-12 pillar review; not adversarially reviewed. The pre-start review round covers it.

- **F-09, secrets in up to five stores.** *Design* gains *What one secret costs*: the five
  stores, the writers of each, and which stores a secret touches by what reads it. D3.4's
  add-secret edit lists those stores, and the *Risks* entry on the operator step points at
  them. The encrypt-only path went to Open question 8 rather than into the design. C3 and
  R2's `sops-manifest-entries` check limit the second path to `SOPS_MANIFEST_KEYS`, so a
  single-line secret in a manifest needs a contract change. The question recommends a
  `seal` mode of about 120 lines and gives its red proof. *Complexity delta* names `seal` as
  not added. `cluster_vars.py write` stays operator-only.
- **Contract sync, C6.** D3.4's `land-wait` rule for the cluster files is marked interim.
  C6 has `land.sh` derive no tags itself, and C1's `footprint` counts the cluster files for
  every substituting tag.

### Cross-slice note from R2 (2026-10-13)

R2's *Risks* said that D3 carries a cross-slice note on an all-digit value, but D3 did not.
The operator ruled on 2026-10-13 that R2 guards standalone values where they are decrypted
and moves `mullvad_account` to a dedicated SOPS Secret (R2 Open question 13). Applied here:

- `write` refuses a value of a key with a standalone use that R2's shared resolver types,
  C3's standalone value rule. It lands in D3.2a, with a fixture pair.
- `check` requires `type:str` on every SOPS manifest value, so a manifest encrypted from
  `mullvad_account`'s `type:int` value fails in CI without a key.
- D3.3d, after D3.3c, adds the `sops-manifest-type-proof` job: in a kind cluster, an
  all-digit value in a quoted `stringData` field of a SOPS-encrypted Secret applies as a
  string, and a value encrypted from an int does not. D5's flip of qbittorrent waits on it,
  and the remedy if it fails is a base64 `data:` value.
- The SOPS manifests count four, with qbittorrent's, in the summary, the *Baseline* and the
  tree diff.
- D3.2a's estimate grows to about 300 lines. D3.3d has its own size, rollback entry, tests
  and docs entry and *Complexity delta* line, so the work R2 hands over is counted here.
