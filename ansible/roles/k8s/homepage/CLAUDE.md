# homepage — Application dashboard

The landing dashboard (gethomepage) with service tiles, widgets and bookmarks. Live on
daniel-box since E3 (2026-08-12). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/gethomepage/homepage:latest`
- **URL:** `homepage.<domain>` · **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-box.yml` → `containers_list`
- **Deploy:** `uv run ansible-playbook ansible/deploy.yml --tags "homepage"`

## Where the config lives
All of it renders into one Secret (`config-secret.yaml.j2`), which mounts read-only:

- `templates/config/{settings,bookmarks,widgets}.yaml.j2` + `custom.css.j2` — moved here
  from the retired Docker role, which used to own them. They sit one level down because
  `validate_k8s_manifests.py` parses every `templates/*.j2` as a manifest, and `custom.css`
  is not YAML.
- `templates/services.yaml.j2`, `docker.yaml.j2`, `kubernetes.yaml.j2` — always this role's
  own. `services.yaml` is the tile list; edit it here and nowhere else.
- `templates/icons-configmap.yaml.j2` — base64s the PNGs in `files/` into a ConfigMap.

Edit the `.j2` files, never the live config: homepage seeds any missing file into
`/app/config` at startup, which EROFSes on the read-only mount and crash-loops the pod.

## Notable
- Pulls calendar data from the internal `ical-proxy`.
- The `docker.yaml` status dots have no k8s equivalent yet, so `docker.yaml.j2` renders empty
  and `services.yaml.j2` drops the matching `server:`/`container:` keys — tiles render
  dot-less rather than erroring on a `my-docker` host that does not exist here.
