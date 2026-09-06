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
- **A widget dialling a ClusterIP needs the target's NetworkPolicy to name `app: homepage`.**
  The namespace baseline admits traefik and prometheus, so pod-to-pod from homepage is denied by
  default and the tile fails as a widget-proxy error while homepage stays 1/1. ENFORCED by
  `ansible/tests/services/test_homepage_widget_netpol_edges.py`, which resolves each widget URL's
  Service to the pod label it selects and checks the rendered policies.
- **The longhorn widget dials `longhorn-frontend`, not `longhorn-backend`.** Longhorn's own
  chart-owned `longhorn-manager` policy admits six same-namespace components and nothing else, and
  editing it means editing an object the Longhorn deploy would revert. `longhorn-frontend` is
  selected by no policy at all and proxies `/v1` through as `app: longhorn-ui`, which that policy
  does admit. The node-local-manager trap does not apply here: the caller is a pod and the target
  is the frontend Service.
- **Four widgets are deliberately absent**, each blocked on a credential rather than on plumbing:
  traefik (#1383 — `api.insecure: false` and the `DECIDED:` marker in
  `roles/k8s/traefik/templates/dashboard-ingressroute.yaml.j2` mean `/api` is served nowhere the
  widget can reach), grafana (#1384 — needs a service-account token; only the admin password
  exists), crowdsec (#1385 — a LAPI machine credential is read/write on decisions) and
  healthchecks (#1386 — needs a project API key created in the UI). Read the issue before adding
  one; each records why it was not simply plumbed in.
- The `docker.yaml` status dots have no k8s equivalent yet, so `docker.yaml.j2` renders empty
  and `services.yaml.j2` drops the matching `server:`/`container:` keys — tiles render
  dot-less rather than erroring on a `my-docker` host that does not exist here.
