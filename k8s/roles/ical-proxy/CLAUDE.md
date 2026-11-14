# ical-proxy — iCal feed aggregation proxy

Small Flask app that merges several ICS calendars into one feed for the Homepage
calendar widget. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=ical-proxy` gates the rollout
- **Image:** `ghcr.io/danielh2018/ical-proxy` (`ical_proxy_k8s_image`)
- **Route:** `ical-proxy.local.<domain>` (LAN only), no Authelia
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux's render pulls `ghcr.io/danielh2018/ical-proxy:<content tag>`**, with
  `imagePullPolicy: IfNotPresent` (D5.4b), and the *Image* line above names that repository.
  The role's defaults still name the in-cluster registry. The image is built
  from `templates/Dockerfile.j2`, with the Flask app in `files/ical_proxy.py`, shipped into the
  image as `app.py`.
- **Host:** daniel-box (k8s), since 2026-08-10 — slice-7 Phase C
- **The route guards by ClientIP** so only Homepage reads the unauthenticated private feeds
- **Flux applies it.** Its `containers_list` entry is
  in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster when
  `promote` publishes and verifies the next bundle. traefik is flux-owned too, so the
  Kustomization lists it in `dependsOn`, and Flux applies this tag only after its Kustomization is
  Ready. The Deployment is `RollingUpdate` with one replica. The default surge starts the new pod
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
  `secrets/cluster-vars.sops.env`. The Kustomization depends on `cluster-vars`, so a
  rotated value is live before ical-proxy reconciles. A rotation reaches the cluster only
  through `cluster_vars.py write`, which the operator runs after the hand rotation: all three
  keys are `assisted`-tier in `secrets/secret_rotation.yml`. The rotation rolls the pod through
  the restart checksum.
- **Its image build and rollout.** Promote's `images` job builds the image and pushes it to
  GHCR. The role's `manifests_rollout: ical-proxy` names the Deployment that the
  Kustomization's `wait` holds and that the renderer stamps the restart checksum on.
  `k8s_autodeploy: true` gates no apply: Flux applies every merged
  change. It still decides whether Renovate automerges the image bump.

## Notable
- Aggregates Google + Obsidian ICS URLs (`calendar_1/2/4` from secrets), refreshing every
  15 min. Not exposed publicly — Homepage consumes it over the private network.
- To change the image, edit `templates/Dockerfile.j2` or the app and merge. The next
  promotion builds the new content tag and rolls the pod.

## Editing
- App: `files/ical_proxy.py` (tests in `tests/test_app.py`) · Image: `templates/Dockerfile.j2`
