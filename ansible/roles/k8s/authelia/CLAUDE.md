# authelia — SSO / forward-auth for the cluster

Authelia guards most public routes as a Traefik forward-auth middleware. See repo-root
`CLAUDE.md` for shared conventions.

**Deploy tag:** `--tags "authelia"`. Denylisted from GitOps auto-deploy (platform — SSO/OIDC
gate; a failed deploy locks out access to everything behind it, including the tools to fix it).

## Traps

### The one-time code lives in the pod's notification.txt
Authelia uses the file notifier, so identity-verification one-time codes land in
`/config/notification.txt` inside the Authelia pod (namespace `homelab`). No email is sent.
The user runs the retrieval in a real terminal, because `sudo` needs a TTY and is ask-listed:

```
sudo k3s kubectl -n homelab exec deploy/authelia -c authelia -- cat /config/notification.txt
```

Three traps hit on 2026-08-09:

- k3s runs on **daniel-box**, not daniel-server — `ssh daniel-server sudo k3s …` fails with
  "command not found". Run it locally.
- The readonly SA (`homelab-readonly`) cannot `exec`. Only `sudo k3s kubectl`, which uses the
  root kubeconfig, can.
- Codes expire in ~5 min, the Authelia elevated-session default. Resend in the browser, then
  re-read the file — it holds only the latest notification.

### `authelia_session*` is a cookie name, not a secret name

The session secret is **`authelia_secret`**. That one SOPS key fills `session.secret` on both
portals — the retired Docker one (the `configuration.yml` template under
`ansible/roles/containers/archive/authelia/templates/`, at its line 92) and this one
(`templates/config-secret.yaml.j2:110`). Every `authelia_session*` string in the tree is a
**cookie** name instead: `authelia_session` on the Docker portal, `authelia_session_k8s` here
(`defaults/main.yml:50`). Searching the rotation registry for `authelia_session*` therefore
finds nothing, which reads as an untracked credential.

That is what the open item standing here until 2026-09-05 had found. It said the Docker
portal's session keys were never rotated after this portal took over `auth.<domain>`, and that
the key the note meant was recorded nowhere. Both halves are settled:

- `authelia_secret` is in `ansible/secret_rotation.yml` at tier `assisted`, and
  `secret_rotation.py audit` reads it `ok`. `sync` adds nothing.
- The tier is right. Rotating this key re-signs session cookies, so every user is logged out
  and no data is lost. It needs none of the `pinned` care `authelia_storage` takes
  (`docs/secret-rotation.md`).
- Its ciphertext last changed on 2026-08-30, two weeks after this portal took over
  `auth.<domain>`.

**The registry records `last_rotated: '2025-12-15'` for this key, and that is not drift.**
`audit` advances the date in memory to the day git shows the ciphertext last changed, then
writes nothing back — git is the source of truth, which is why `sync` leaves an existing date
alone. Read the audit line before concluding that a registry date means a key is stale.

### The crowdsec-agent sidecar seeds /etc/crowdsec from an init container, not its entrypoint

The CrowdSec image entrypoint opens with a "Populating configuration directory" step — an
`rsync -a --ignore-existing /staging/etc/crowdsec/* /etc/crowdsec` — that runs under `set -e`
and only while `/etc/crowdsec/config.yaml` is absent. About twenty staged files are root-only
(the LAPI and online credentials, the bundled hub tree), so the non-root sidecar exits 23 on
it and the kubelet restarts the container. The restart finds `config.yaml` present, skips the
block, and the pod settles at 2/2 Running with one restart on the clock.

That one restart failed every authelia deploy's health gate. `probe.py health` fails closed on
any container restart inside its 180s window, so `land.sh` read `VERDICT: unhealthy` while
nothing was actually wrong (#1173). Traefik hit the same thing first (#976).

`crowdsec-config-install` therefore runs that rsync itself, before its own `install` steps, so
the seeds win over the staged copies of the same names and the sidecar's entrypoint finds
`config.yaml` already there. **Exit 23 is the only status tolerated.** Authelia rolls under
`Recreate`, so a failed init container means the old pod is already gone and SSO is down — but
a blanket `|| true` would trade that for an agent started on a half-populated config with
nothing saying so. `ansible/tests/services/test_crowdsec_config_install_seeds_staged_tree.py`
holds both pods to this.

`crowdsec-data-install` is the second init container, and it fixes a different half of the
same image's staging behaviour. The image ships its datafiles at
`/staging/var/lib/crowdsec/data` mode 0600 root:root and the entrypoint SYMLINKS them into the
data volume rather than copying, so the non-root agent cannot read through the link. GeoIP then
never initialises — `unable to open GeoLite2-City.mmdb: permission denied` — and the
`geoip-enrich` parser is dead behind a pod that reads 2/2 Running (#1177; traefik hit the same
thing first as #990). Copying the files in world-readable defeats the symlink, because the
entrypoint's `[ ! -e ]` guard skips a name that already exists. It runs as root with
`DAC_READ_SEARCH` — the read-only half of root's permission-bit override, which is what reaches
the 0600 sources — and ends in `exit 0`, so an unreadable file leaves that one name on the
symlink path instead of taking SSO down under `Recreate`.
`ansible/tests/services/test_crowdsec_optional.py` holds both pods to this.

Verify a deploy of this role by the sidecar's restart count, not just by pod readiness:

```
kubectl get pod -n homelab -l app=authelia -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}'
```
