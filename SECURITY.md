# Security policy

This is a personal homelab, run by one person. There is no security team, no bounty, and no
response-time commitment. What follows is what actually happens, not a process promise.

## Reporting a vulnerability

Report privately through GitHub's [private vulnerability
reporting](https://github.com/DanielH2018/server/security/advisories/new) — the **Security** tab,
then **Report a vulnerability**. It is enabled on this repository.

Do not open a public issue for anything that exposes a credential, a host, or a way into the
network.

Include what you found, where in the tree, and how you reached it. A file path and a reproduction
beat a scanner name.

Expect a reply when I next sit down with the repo, typically within a week. A fix lands as a PR
like any other change, gated on CI.

## Scope

The interesting parts are the Ansible roles, the rendered Kubernetes manifests, and the Python
under `scripts/`. Anything under `ansible/roles/containers/archive/` deploys nothing — it is kept
for git history and is out of scope.

The homelab itself is not a target. Nothing here authorizes probing, scanning, or reaching the
live hosts or their public hostnames; findings must come from reading this repository.

## Secrets

Secrets live SOPS-encrypted in `ansible/vars/secrets.yml`, with the age private keys never leaving
the hosts. `.gitattributes` sets `diff=sops`, so `git diff` on that file prints **plaintext** —
that is a local review hazard, not a defect in the committed blob.

If you find a credential committed in plaintext anywhere in the tree or in history, report it
privately and treat it as live. Every value exposed that way gets rotated rather than merely
removed, because removal from a public repository is not containment.
