---
id: "0003"
title: Secrets are encrypted at rest with SOPS and age, in the repo
status: Accepted
date: 2026-08-01
governs: []
---

# ADR-0003: Secrets are encrypted at rest with SOPS and age, in the repo

## Status

Accepted.

## Context

The homelab is deployed from a git repository by Ansible, and the deploy needs credentials:
API tokens, database passwords, the SSO signing material. Keeping them out of the repo means
a second distribution channel that has to be as available as the repo and as reproducible on
a rebuilt host, which is a second system to get right.

Keeping them in the repo means encrypting them, and that requires a scheme where a host can
decrypt without a human present — the deploy runs from a timer.

## Decision

Secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS to age recipients.
`ansible/.sops.yaml` lists the recipients and auto-encrypts anything under a `vars/` or
`secrets/` directory. At deploy time the `community.sops.sops_decrypt` lookup decrypts values
in place.

Multi-recipient is OR: any listed key decrypts the whole file, which is what lets a new host
be onboarded by adding its public key rather than by re-keying anything.

Rotation is tracked separately in `ansible/secret_rotation.yml` — a **plaintext** registry of
names, tiers and dates, never values — with a daily cron pushing a monitor.

## Consequences

**A host that cannot decrypt fails at the secret-load pre-task**, so onboarding is its own
procedure: run `bootstrap.yml` on the new host to generate its key, add the public key to
`.sops.yaml`, `sops updatekeys` from a host that can already decrypt, commit, pull.

**`git diff ansible/vars/secrets.yml` prints plaintext credentials.** `.gitattributes` sets
`diff=sops`, so git decrypts before diffing. The committed blob stays encrypted — this is a
review-hygiene trap, not a repo defect, and the driver is worth keeping because an encrypted
diff is unreadable. But the plaintext reaches the terminal, the scrollback and any agent
transcript that captured the command, so a value exposed that way needs rotating. A reviewer
did exactly this on 2026-08-24. To see which keys changed without their values, diff the
names.

**`kubectl apply` leaves stale Secret keys behind.** Removing a key from a manifest does not
remove it from the live Secret once ownership breaks.

**A direct edit to `secrets.yml` is denied by a hook**, because an editor that writes
plaintext over an encrypted file destroys it silently.

**`ansible/inventory/` is neither auto-encrypted nor hook-guarded.** A secret pasted into
`host_vars` or `group_vars` commits in plaintext, and nothing stops it.

**Keys with no references are not necessarily orphaned.** Five of eight apparently unused
keys turned out to be hand-typed dashboard logins. A grep produces candidates, not a
deletion list.

## Governs

No single line. `governs:` is empty; the decision is expressed by `ansible/.sops.yaml`.
