---
id: "0022"
title: The cluster decrypts its own file with its own key, and never secrets.yml
status: Proposed
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

Proposed. Slice D3 of the [target architecture](../target-architecture/design.md) drafts it
in PR D3.1. Acceptance is **operator-only**, and the operator moves the status to `Accepted`
in that PR's review. Adding the cluster recipient to `ansible/.sops.yaml` is a second
operator-only decision in the same review. Slice H4 extends this record with per-host files
(design.md, *Amend ADR-0003*).

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

## Governs

No single line. `governs:` is empty; the decision is expressed by the creation rules in
`ansible/.sops.yaml`.
