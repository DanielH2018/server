---
paths:
  - "ansible/roles/k8s/**/templates/**"
  - "ansible/roles/k8s/**/files/**"
---

# Where a k8s role's non-manifest config goes

`roles/k8s/<name>/templates/` is for **manifests only**. App config a manifest embeds via
`lookup('template')` goes in **`templates/config/`** (CouchDB's `local.ini`, HA's
`secrets.yaml.j2`); a file it carries verbatim with `lookup('file')` goes in `files/` (HA's
`configuration.yaml`). `scripts/validate/k8s_manifests.py` enforces both directions — it fails
a `lookup('template')` naming a `templates/` path outside `config/`, and a top-level template
that renders a document with no `kind` — and `is_manifest_template` in
`scripts/lib/k8s_roles.py` names the two shapes it exempts (`Dockerfile*`, `*.sh.j2`).

The shared render → apply → queue contract every k8s role includes is
`ansible/roles/k8s/manifests/CLAUDE.md`.
