# `hosts/components/sops` — the `sops` command on every host

The component ships `/usr/bin/sops`, which execs the pinned binary
`hosts/packages/homelab-tools/package.yml` installs at `/usr/lib/homelab/bin/sops`. It makes the
package depend on `age`. All three host packages list it, and each lists the old
`/usr/local/bin/sops` as a legacy entry, because `/usr/local/bin` comes first on `PATH`.
`hosts/bootstrap-host.sh` creates the host's own age key, `/etc/homelab/age.key`, which
`homelab-secrets` decrypts the host's SOPS file with.

## Rules the files do not state

- **DR:** the user-home age keys are in no automated backup, because nothing outside a
  Longhorn volume is. The operator backed them up out-of-band on 2026-06-06. Since 2026-06-11
  `secrets/.sops.yaml` also lists an off-box recovery recipient, whose private half is only in
  the operator's password manager, so the secrets survive losing every host key.
  `docs/break-glass.md` records which hosts keep a user-home key.
- **Three creation rules, four kinds of file.** `secrets/.sops.yaml` holds two cluster rules
  ahead of the generic rule, because SOPS takes the first rule that matches and the generic
  rule also matches every cluster manifest. The cluster file (the `cluster-vars` dotenv under
  `secrets/`) and the cluster manifests (under `secrets/cluster-manifests/`) go to the cluster key
  plus the host keys. `secrets.yml`, and any other YAML directly under `secrets/`, go to the
  host keys only, so the cluster never decrypts `secrets.yml` (ADR-0022). The file's header
  holds the full runbook.
- **Every `sops` command runs from `secrets/`, or with `--config secrets/.sops.yaml`.** SOPS
  resolves `.sops.yaml` from the working directory, so from the repo root it finds no
  creation rule. `--config` is a global flag and goes before the subcommand (`sops --config
  secrets/.sops.yaml updatekeys <file>`).
- **Onboarding an Nth recipient:** append its public key to the `age:` string of all three
  rules, then from `secrets/` run `sops updatekeys` on `secrets.yml`, the cluster file and each
  cluster manifest that exists, and commit. Removing one deletes its key from all three rules
  and runs `sops rotate -i --rm-age <key>` on the same files, not `updatekeys`; the
  `.sops.yaml` header says why.
