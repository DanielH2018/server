---
id: "0022"
title: The cluster decrypts its own file with its own key, and never secrets.yml
status: Accepted
date: 2026-10-15
governs: []
---

# ADR-0022: The cluster decrypts its own file with its own key, and never secrets.yml

Amends [ADR-0003](0003-sops-with-age-for-secrets-at-rest.md). ADR-0003's decision still
stands: secrets live in the repo, encrypted with SOPS to age recipients, and
`ansible/vars/secrets.yml` stays their source. What changes is who decrypts. ADR-0003 has one
decrypting party, a host that runs Ansible. This record adds a second, the cluster, and
limits it to files written for it.

## Status

Accepted. The operator accepted it on 2026-10-15 in PR D3.1's review. The answer was "Yes" to "accept ADR-0022 and the cluster recipient?" The same answer accepted the second operator-only decision in that PR: adding the cluster recipient to the two cluster creation rules in `ansible/.sops.yaml`. Slice H4 extends this record with per-host files
(design.md, *Amend ADR-0003*); the *Amendment* section at the end of this record carries that extension.

## Context

ADR-0003 decrypts at deploy time. `community.sops.sops_decrypt` runs on the Ansible
controller, with the age key of the host that runs the deploy. The cluster holds no key and
receives rendered Secrets that already carry plaintext.

[ADR-0020](0020-flux-applies-the-cluster-from-a-promoted-artifact.md) proposes that Flux apply
the cluster from a promoted artifact. Flux then renders a role's Secrets itself, so the
cluster must decrypt something. Flux's `kustomize-controller` decrypts SOPS natively, with an
age key held in a Secret in `flux-system`. Slice D3 has the plan, in
[d3-secrets.md](../target-architecture/deploy/d3-secrets.md).

Three options existed:

- **Give the cluster a host key.** It would decrypt `secrets.yml` whole, including the
  credentials of every host-plane service and the off-box recovery material. A key held in a
  cluster Secret is readable by anything that can read Secrets in `flux-system`.
- **Add a cluster key to `secrets.yml`'s rule.** Multi-recipient is OR, so this is the same
  exposure as the first option.
- **Give the cluster a dedicated key and its own files.** The cluster decrypts only a file
  that holds the values its render references. Each value is copied from `secrets.yml` by an
  operator tool and checked in CI against `secrets.yml`'s ciphertext.

## Decision

The cluster has its own age key. Its private half lives in the Secret `flux-system/sops-age`
and in the operator's password manager, and nowhere else. `ansible/.sops.yaml` gives the key
to two creation rules only: the cluster file, `vars/cluster-vars.sops.env`, and the cluster
manifests, `vars/cluster-manifests/*.sops.yaml`. Both rules sit ahead of the generic rule,
because SOPS takes the first rule that matches. The generic rule, which covers `secrets.yml`,
never lists the cluster key. The cluster therefore decrypts the cluster file with its own key,
and never `secrets.yml`.

Both cluster rules also list every host key that `secrets.yml`'s rule lists, so any host that
decrypts `secrets.yml` can regenerate the cluster files. `secrets.yml` stays the only source
of a value. The cluster files are derived from it.

## Consequences

**Adding or removing a host edits three rules, not one.** It runs `sops updatekeys` or `sops
rotate -i --rm-age` on `secrets.yml`, the cluster file and each cluster manifest. The header
of `ansible/.sops.yaml` and the *Notable* section of
`ansible/roles/setup/sops_setup/CLAUDE.md` carry the runbook. A rule that drifts from the
others leaves one host unable to regenerate a cluster file. Slice D3's `cluster_vars.py
check` compares each file's recipients with its rule.

**The rule order is load-bearing.** The generic rule's `path_regex` also matches every
cluster manifest. A cluster rule moved below it stops applying, and a manifest written then
is encrypted without the cluster key, so the cluster cannot decrypt it.

**Every `sops` command runs from `ansible/`, or with `--config ansible/.sops.yaml`.** SOPS
resolves `.sops.yaml` from the working directory. Run from the repo root without `--config`,
it finds no creation rule. This held under ADR-0003 too, and three rules raise the cost of
forgetting it.

**A value is encrypted twice, once per file.** A rotated value reaches the cluster only after
the operator's `cluster_vars.py write` regenerates the cluster file. CI holds no key, so it
proves freshness by comparing hashes of `secrets.yml`'s ciphertext, never by decrypting.

**The cluster key is one more key to keep.** It is not in `secrets.yml`, so the rotation
registry does not track it. Rotating it is an operator-only procedure in slice D3 (*Rotating
the cluster key*). Losing both copies means generating a new key and re-encrypting the
cluster files from a host, which needs no value from the cluster.

**ADR-0003's "auto-encrypts" wording is narrower than it reads.** A creation rule applies
only when `sops` itself encrypts a file. It chooses recipients and options. It does not
encrypt a file that an editor writes under `vars/`.

## Amendment: each host decrypts its own file, with its own key (slice H4)

### Amendment status

Accepted. The operator accepted it on 2026-10-23, in the session that orchestrates slice H4,
before PR H4.1d merged. The sections above stay as accepted on 2026-10-15. This section
extends them from the cluster to the hosts and changes nothing the cluster rules decide.

### Amendment context

Slice H4 ships a host's secret-bearing configuration inside the host's package, and a unit on
the host, `homelab-secrets.service`, decrypts it at boot. The host needs a key. Multi-recipient
SOPS is OR, so a key that decrypts `secrets.yml` decrypts every value in it, including the
credentials of every other host.

Three options existed:

- **Give the host a recipient key for `secrets.yml`.** daniel-pi left that list on 2026-10-03,
  and `docs/break-glass.md` records the removal. Adding it back would give a Raspberry Pi on an
  SD card the whole store.
- **Ship a byte copy of `secrets.yml` in each package.** The copy changes with every commit to
  that file. The builder publishes a package whenever its rendered tree differs, so every
  `/add-secret` PR and every rotation would publish a package for every host.
- **Give each host its own file.** The host decrypts a file that holds only the keys its tree
  names. An operator tool copies each value from `secrets.yml`, and CI checks the copy against
  `secrets.yml`'s ciphertext. This is the option ADR-0022 already chose for the cluster.

### Amendment decision

A host that runs from a package has its own age key, `/etc/homelab/age.key`, and its own SOPS
file, `ansible/vars/hosts/<host>.sops.yaml`. The package ships the file as
`/usr/lib/homelab/secrets/<host>.sops.yaml`. The file holds only the keys that the host's
rendered tree names, as credentials or as placeholders in a declared output.

The host decrypts its own file and never `secrets.yml`. Each file is encrypted to three keys:
the host's, daniel-box's and the off-box recovery key. `ansible/.sops.yaml` gets one creation
rule per host file, ahead of the generic rule, because the generic rule's pattern also matches
`ansible/vars/hosts/`. The generic rule and the two cluster rules do not change. The host key
appears in no rule except its own file's.

`secrets.yml` stays the only source of a value. `scripts/secrets_mgmt/host_secrets.py sync`
copies the values and is operator-only, because it decrypts `secrets.yml`. `host_secrets.py
check` runs in CI without a key. It compares, for each key, the commit that last changed the
host file with the commit that last changed `secrets.yml`, and fails when the host file is
older.

### Amendment consequences

**A value is stored once more per host that carries it.** A rotated value reaches a host only
after the operator runs `sync` and the package ships. `check` fails the PR that rotates a key
without the `sync`, and the unattended `rotate --commit` skips every key a host file carries,
so a cron commit cannot leave a host file stale.

**Losing a host's key exposes that host's file, not `secrets.yml`.** The rotation list for a
lost or exposed host key is derived, not remembered: every value in every SOPS file whose
creation rule lists that host's public key. `docs/break-glass.md` carries the procedure from
PR H4.2.

**The rule order is load-bearing here too.** A host rule moved below the generic rule stops
applying, and `sync` then encrypts the file without the host's key, so the host cannot
decrypt it.

**`check` needs full git history.** A shallow clone dates every key at its one visible commit
and would pass a stale copy, so `check` fails in one. The pytest job checks out with
`fetch-depth: 0`.

**Reversal costs one PR.** Stop shipping the file, delete it and its creation rule. Nothing
else holds a copy that the cluster or the other hosts read.

## Governs

No single line. `governs:` is empty; the decision is expressed by the creation rules in
`ansible/.sops.yaml` and by `scripts/secrets_mgmt/host_secrets.py`.
