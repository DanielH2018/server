# bento-pdf — browser-based PDF toolkit

BentoPDF, a stateless client-side PDF editor. See repo-root `CLAUDE.md` for shared
conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "bento-pdf"`
- **Image:** `ghcr.io/alam00000/bentopdf` (`bento_pdf_k8s_image`)
- **Route:** `bento-pdf.<domain>` · `bento-pdf.local.<domain>`, Authelia one_factor
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Digest-pinned image**, with the tag kept alongside the digest so Renovate's k8s-defaults
  manager still tracks it for digest-bump PRs.
- **Port:** 8080. Stateless RollingUpdate Deployment with a readinessProbe, which is what
  makes it auto-deploy-eligible.
- **Hostname is single-label** (`bento-pdf`, not `bento-pdf.k8s`) deliberately: the
  `*.local.<domain>` wildcard cert covers it; a nested label would not.

## Notable
- `bento_pdf_k8s_uid: 101` matches the upstream nginx-unprivileged image's own UID, and is
  also used as `fsGroup` for the container's tmpfs scratch dirs — `emptyDir` mounts
  `0755` root-owned with no equivalent of the Compose template's `tmpfs mode=1777`, so
  without the matching `fsGroup` the container hits `EPERM` on write.
- Pins the same image tag as the retired `roles/containers/archive/bento-pdf` Docker role,
  kept in sync deliberately so a behavior difference between the two is never the image.

## Editing
- Manifests: `templates/deployment.yaml.j2`, `templates/ingressroute.yaml.j2`,
  `templates/service.yaml.j2`.
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "bento-pdf"`.
