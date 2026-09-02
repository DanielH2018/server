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
  `validate/k8s_manifests.py` parses every `templates/*.j2` as a manifest, and `custom.css`
  is not YAML.
- `templates/services.yaml.j2`, `docker.yaml.j2`, `kubernetes.yaml.j2` — always this role's
  own. `services.yaml` is the tile list; edit it here and nowhere else.
- `templates/icons-configmap.yaml.j2` — base64s the PNGs in `files/` into a ConfigMap.

Edit the `.j2` files, never the live config: homepage seeds any missing file into
`/app/config` at startup, which EROFSes on the read-only mount and crash-loops the pod.

## Notable
- Pulls calendar data from the internal `ical-proxy`.
- **The Headlamp tile's widget reads Prometheus, not Headlamp.** Headlamp exposes no service
  API — its backend only proxies the Kubernetes API — and dialling its ClusterIP is fenced off
  on purpose (`roles/k8s/headlamp/templates/networkpolicy.yaml.j2`, re-asserted every deploy by
  that role's netpol-probe Job). So the tile shows cluster-state counts from a `customapi`
  widget against `prometheus.<observability-ns>:9090`, with the PromQL in `defaults/main.yml`
  as `homepage_k8s_headlamp_cluster_query`. That call is cross-namespace, so
  `prometheus-callers` (`roles/k8s/netpol-baseline/templates/networkpolicy-prometheus.yaml.j2`)
  names `homepage` — a widget-proxy error on that tile is the symptom of it being dropped.
- The `docker.yaml` status dots have no k8s equivalent yet, so `docker.yaml.j2` renders empty
  and `services.yaml.j2` drops the matching `server:`/`container:` keys — tiles render
  dot-less rather than erroring on a `my-docker` host that does not exist here.
