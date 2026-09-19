# ical-proxy — iCal feed aggregation proxy

Small Flask app that merges several ICS calendars into one feed for the Homepage
calendar widget. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry. -->
- **Deploy tag:** `--tags "ical-proxy"`
- **Image:** `<k8s_registry_pull_host>/ical-proxy` (`ical_proxy_k8s_image`)
- **Route:** `ical-proxy.local.<domain>` (LAN only), no Authelia
- **Claims:** none (no PVC)
- **Auto-deploy:** eligible (`k8s_autodeploy: true`)
<!-- /generated_from -->

- **Built in-cluster** from `templates/Dockerfile.j2` (Flask app in `files/app.py`)
- **Host:** daniel-box (k8s), since 2026-08-10 — slice-7 Phase C
- **The route guards by ClientIP** so only Homepage reads the unauthenticated private feeds

## Notable
- Aggregates Google + Obsidian ICS URLs (`calendar_1/2/4` from secrets), refreshing every
  15 min. Not exposed publicly — Homepage consumes it over the private network.
- Image is built in-cluster by `k8s/image-builder` from this role's `templates/Dockerfile.j2`
  — update via redeploy.

## Editing
- App: `files/app.py` (tests in `tests/test_app.py`) · Image: `templates/Dockerfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "ical-proxy"`
