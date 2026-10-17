# ical-proxy — iCal feed aggregation proxy

Small Flask app that merges several ICS calendars into one feed for the Homepage
calendar widget. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=ical-proxy` with exit 5
- **Image:** `<k8s_registry_pull_host>/ical-proxy` (`ical_proxy_k8s_image`)
- **Route:** `ical-proxy.local.<domain>` (LAN only), no Authelia
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux's render pulls `ghcr.io/danielh2018/ical-proxy:<content tag>`**, with
  `imagePullPolicy: IfNotPresent` (D5.4b). The *Image* line above prints the Ansible default,
  which names the in-cluster registry. The image is built from `templates/Dockerfile.j2`, with
  the Flask app in `files/ical_proxy.py`, shipped into the image as `app.py`.
- **Host:** daniel-box (k8s), since 2026-08-10 — slice-7 Phase C
- **The route guards by ClientIP** so only Homepage reads the unauthenticated private feeds
- **Flux applies it, not Ansible** (D5.14, the first Wave C PR). Its `containers_list` entry
  carries `owner: flux`, so `deploy.sh`, the play and the GitOps deployer all skip it, and a
  merged change reaches the cluster when `promote` publishes and verifies the next bundle. To
  give it back to Ansible, follow the two-PR handback in `docs/flux-deploy-plane.md`; a plain
  revert of the flag leaves two writers on the objects. traefik stays Ansible-owned, so the
  Kustomization records it in its `homelab/ansible-owned-deps` annotation, not in `dependsOn`.
  The Deployment is `RollingUpdate` with one replica. The default surge starts the new pod
  before the old one stops, so a roll keeps one pod serving.
- **`promote` builds the image, not `k8s/image-builder`.** The renderer writes the build
  context to `DIR/images/ical-proxy/` and its content tag to `DIR/images/tags.json`, and the
  Deployment's ref reads the same tag. promote's `images` job pushes that tag to GHCR when GHCR
  does not serve it yet (`scripts/deploy_tools/ci_images.py`). So a merged edit to
  `files/ical_proxy.py` or `templates/Dockerfile.j2` moves the content tag, and the next
  promotion builds the image and rolls the pod. A Renovate bump of the `FROM` digest moves it
  the same way.
- **The calendar-feed Secret substitutes from `homelab-cluster-vars`.** `calendar_1`,
  `calendar_2` and `calendar_4` are `${<key>}` placeholders in the published render. Flux
  substitutes the values from the `homelab-cluster-vars` Secret, which it decrypts from
  `ansible/vars/cluster-vars.sops.env`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before ical-proxy reconciles. A rotation reaches the cluster only
  through `cluster_vars.py write`, which the operator runs after the hand rotation: all three
  keys are `assisted`-tier in `ansible/secret_rotation.yml`. The rotation rolls the pod through
  the restart checksum.
- **Where the tasks went.** `tasks/main.yml` holds two includes, and no Flux path runs either.
  They stay for the handback. The `k8s/image-builder` include became promote's `images` job
  (D5.4a2) and the GHCR ref (D5.4b). The `k8s/manifests` include's `manifests_rollout:
  ical-proxy` named the Deployment for the stabilisation gate and the shared restart. The
  Kustomization's `wait` replaces the gate, and the restart checksum the renderer stamps
  replaces the restart. `k8s_autodeploy: true` no longer gates an apply, because the deployer
  skips the tag.

## Notable
- Aggregates Google + Obsidian ICS URLs (`calendar_1/2/4` from secrets), refreshing every
  15 min. Not exposed publicly — Homepage consumes it over the private network.
- To change the image, edit `templates/Dockerfile.j2` or the app and merge. The next
  promotion builds the new content tag and rolls the pod.

## Editing
- App: `files/ical_proxy.py` (tests in `tests/test_app.py`) · Image: `templates/Dockerfile.j2`
