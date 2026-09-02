# ical-proxy — iCal feed aggregation proxy

Small Flask app that merges several ICS calendars into one feed for the Homepage
calendar widget. See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** built from `templates/Dockerfile.j2` (Flask app in `files/app.py`)
- **Host:** daniel-box (k8s), since 2026-08-10 — slice-7 Phase C
- **Internal only, no public route** — the cluster route guards it by ClientIP so only
  Homepage reads the unauthenticated private feeds
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`

## Notable
- Aggregates Google + Obsidian ICS URLs (`calendar_1/2/4` from secrets), refreshing every
  15 min. Not exposed publicly — Homepage consumes it over the private network.
- Image is built in-cluster by `k8s/image-builder` from this role's `templates/Dockerfile.j2`
  — update via redeploy.

## Editing
- App: `files/app.py` (tests in `tests/test_app.py`) · Image: `templates/Dockerfile.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "ical-proxy"`
