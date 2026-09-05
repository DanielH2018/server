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
