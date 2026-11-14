# navidrome — Subsonic-compatible music server

Navidrome serves the music library at `media/music` on the shared media volume to its web UI
and to Subsonic clients. It was stood up 2026-09-02 for issue #803, parked at zero replicas
until 2026-11-12, and unparked with an empty library for the music pipeline to fill.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=navidrome` gates the rollout
- **Image:** `ghcr.io/navidrome/navidrome` (`navidrome_k8s_image`)
- **Route:** `navidrome.<domain>` · `navidrome.local.<domain>`, Authelia one_factor
- **Claims:** `navidrome-data` (weekly -> B2 (default target)), `media-data` (not Longhorn
  (media-local))
<!-- /generated_from -->

- **Pinned `tag@sha256`.**
- **Host:** daniel-box, by way of the `media-data` local PV's node pin. The template sets no
  affinity of its own (the `DECIDED:` note in `templates/deployment.yaml.j2`).
- **The library** is `media-data` at `subPath: media/music`, mounted read-only at `/music`.
  daniel-box's host package creates the directory
  (`hosts/daniel-box/tmpfiles.d/homelab-media-volume.conf.j2`). Nothing backs the library up,
  like the rest of the media volume.
- **`navidrome-data`** (`navidrome_k8s_claim`), Longhorn, 2Gi — SQLite index and transcoding
  cache, sized as headroom rather than a measurement.
- **The `navidrome-snapshot` Job snapshots `navidrome-data` before each revision applies**,
  so `k8s_autodeploy: true` lets Renovate automerge an image bump that migrates the
  index. A failed revision writes the stateful hold, and the operator reverts the claim
  through `docs/volume-snapshot-drills.md`.
- **Flux applies it, not Ansible** (D5.8, Wave A'). Its `containers_list` entry is
  in `cluster/services.yml`, so nothing applies it by hand, and a merged change reaches the cluster when
  `promote` publishes and verifies the next bundle. The Kustomization lists its dependencies
  (authelia, traefik, media-volume) in `dependsOn`, and its `wait` replaces the rollout wait.

## Notable
- **The first account created in the web UI becomes the admin.** Authelia gates the UI, so
  only a signed-in user can reach that form, but create the admin right after a fresh
  `navidrome-data`.
- **`/rest/` skips Authelia on the public name only.** Subsonic clients cannot pass
  forward-auth, so `bridge_bypass_prefixes` renders a `navidrome-public-rest` route, and
  navidrome checks each request's username and token itself. Point an app at
  `navidrome.<domain>`; the `.local.` name still sends `/rest/` through Authelia. Each user
  needs a password set in navidrome for the app to log in with.
- **Never enable navidrome's reverse-proxy auth** (`ND_REVERSEPROXYWHITELIST`,
  `ND_REVERSEPROXYUSERHEADER`). Navidrome honours the user header on `/rest/` too, so on the
  unauthenticated route anyone could name any user.
- **A wrong password on `/rest/` answers HTTP 200.** The Subsonic protocol reports the failure
  in the body, so CrowdSec's status-based scenarios never see a failed login there. The edge
  rate limit and each user's password strength are what stand in front of it. No CrowdSec
  whitelist row covers `/rest/`: one keyed on a 2xx would also exempt those failed logins, and
  `/rest/` has no unguessable id to key on as Jellyfin's `/Videos/<GUID>/` row does. If a
  first library sync gets a phone banned, unban it as `k8s/roles/crowdsec/CLAUDE.md`
  describes before reaching for a whitelist.
- **External services are on for ListenBrainz and Deezer only.** `ND_ENABLEEXTERNALSERVICES`
  also turns on the anonymous insights collector and the Last.fm agent, so the deployment sets
  both off. Last.fm needs an API account and two secrets that this setup does not have.
- **Each user turns on scrobbling in the UI:** Personal settings, ListenBrainz, then paste the
  user token from listenbrainz.org/settings. Nothing about it is in git.
- **It stays in `BORN_FENCED_ROLES`** (`tests/k8s/test_netpol_baseline_labels.py`).
  Traefik and Prometheus are its only callers, and the baseline admits both. Its calls to
  ListenBrainz and Deezer are egress, which kube-router does not enforce on this cluster
  (`docs/networkpolicy-default-deny.md`).
- **`/metrics` is on, with no password.** `ND_PROMETHEUS_ENABLED` serves it on the UI port,
  and the `navidrome` scrape job reads it. Homepage's Navidrome tile shows the artist, album
  and song counts from `db_model_totals`, through Prometheus. The route keeps `/metrics` behind
  Authelia, because only `/rest/` bypasses it.
- **The route is gated on the replica count.** `defaults/main.yml` lists `ingressroute.yaml` in
  `manifests_files` only while `navidrome_k8s_replicas` is above 0. Traefik re-reads every
  IngressRoute on each config refresh and logs `no servers found for homelab/navidrome`
  whenever the EndpointSlice behind one is empty — every ~20s, forever, burying every other
  router error (issue #1323). Flux prunes by its inventory (`prune: true`), so a render that
  drops the route deletes the live IngressRoute at that promotion. The gate is in the list
  rather than the template because the prune's labelling wrapper refuses a template that
  renders no document.
- **`Recreate` strategy**, not rolling: the data PVC is RWO on a single Longhorn replica, and
  SQLite wants one writer. A change that rolls it takes the service down briefly.

## Parking it again
Set `navidrome_k8s_replicas: 0`, drop `k8s_autodeploy_snapshot_pvcs` and turn
`k8s_autodeploy` off (a snapshot-protected role must have a pod), and add `probe: false` to
the `containers_list` entry so the derived ingress probe does not fire forever. Drop the
entry's `metrics` item too, or the `navidrome` scrape job reads `up == 0` and pages. The route
leaves with the replica count.
