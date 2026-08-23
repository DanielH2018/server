# authelia — SSO / forward-auth for the cluster

Authelia guards most public routes as a Traefik forward-auth middleware. See repo-root
`CLAUDE.md` for shared conventions.

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

## Open item

The Docker portal's session keys were never rotated after this portal took over
`auth.<domain>`. The note lived in daniel-server's host_vars ledger, which retired with the
rest of the migration record; every `authelia_*` key is tracked in
`ansible/secret_rotation.yml`, but none is named `authelia_session*`, so which key that note
meant is not recorded anywhere. Establish that before treating it as done.
