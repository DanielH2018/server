# Claude tooling in this repo — reference

The long form of `CLAUDE.md` → *Claude Tooling in This Repo*. That section keeps what has to
be in context whether or not anyone opens this page: what each tool is, and the gotchas that
would otherwise produce a wrong verdict. Everything here is detail you read when you are
working on one of these tools, or when one of them has just surprised you.

## `scripts/diagnostics/probe.py`

Read-only homelab diagnostics, allow-listed (no prompt). It resolves the live container IP via
`docker inspect`, so prefer it over curling bridge IPs, which change on recreate:

```
uv run python scripts/diagnostics/probe.py <targets | metric '<promql>' | loki-query '<logql>' |
  alerts | monitors | kuma-drift | scrutiny | pi <path> | cert <host> | health <svc> |
  ha <state|automation|get> …>
```

### `alerts [--days N --check X]`

Reconstructs DOWN alert history from Loki, because Kuma keeps only current state — one row per
firing episode. The same view is the "Alert History" Grafana board (Infrastructure folder).

It reads **two** streams: monitor-bridge's container log, and the `{job="syslog"}` `status=down`
lines the host crons emit, which push Kuma directly and so have no other durable record. Until
2026-08-22 it read only the first, and the whole backup/drift plane left no episode anywhere.

### `monitors` vs `kuma-drift`

`monitors` answers "what is down." **`kuma-drift` answers "what is missing,"** which `monitors`
structurally cannot — it counts the exporter's own set, so a monitor that is gone rather than
down leaves the ratio at N/N up (a fenced-off push tile read green for a day on 2026-08-20).

`kuma-drift` diffs that set against `static-monitors.yaml.j2` and treats a push monitor inside
its own interval after a Kuma restart as pending, since Kuma exports a monitor only once it has
beaten.

### `health <svc>`

A k8s post-deploy gate. It exits 0 only when the Deployment **or DaemonSet** is fully rolled out
(observed generation caught up, every replica updated + ready + available) **and** no container
restarted in the last 180s. An unreadable restart time counts as recent, so it fails closed.

Both halves matter — readiness flips a Deployment to Available before a bad liveness probe starts
killing it, so a rollout check alone reports green on a crashlooping pod.

`--docker` inspects the Pi's container over ssh instead. That was the only mode until 2026-08-16,
which is why it died with `FileNotFoundError: 'docker'` on both cluster nodes for the two days
after the Docker retirement.

### `ha …`

Reads live Home Assistant state, authed with the SOPS `claude_ha_token`. `ha automation
<id-or-alias>` resolves the alias-slug≠id trap. See the home-assistant role's `CLAUDE.md`.

## `homelab-ui` MCP server

A headless Chromium Claude drives against the LAN routes, so it can *see* a service's UI
(navigate, click, type, accessibility snapshot, screenshot) rather than infer it from a status
code. This is the half `probe.py health` structurally cannot cover: readiness flips a Deployment
to Available while the UI behind it is broken, which is how 19 dead Grafana panels sat behind a
1/1 pod. Registered user-scope, so it is per-operator config rather than a repo file, and
launched by `scripts/diagnostics/ui_mcp.sh`.

### The three things a browser needs here

The wrapper supplies all three.

- **DNS.** This host's resolver bypasses the LAN DNS, so `.local.<domain>` does not resolve to
  the cluster edge from a shell. The wrapper passes Chromium `--host-resolver-rules` pinned to
  the MetalLB ingress VIP — the browser equivalent of the `curl --resolve` pin
  `probe_core.k8s_endpoint` documents.
- **Auth.** Every `*.local.<domain>` route is Authelia `one_factor`, so the context loads a
  session cookie minted by `uv run python scripts/diagnostics/ui_login.py`. That login sets
  `keepMeLoggedIn`, which is load-bearing — the session config's `inactivity: '5m'` would
  otherwise expire the cookie between two idle minutes, where `remember_me: '1M'` applies only
  when the login asks for it.
- **Secrecy.** `domain` is SOPS-encrypted, so the config is generated into a 0600 file at launch
  instead of being written into `~/.claude.json`.

`ui_login.py --verify <svc>` proves the cookie reaches the backend without involving the browser,
and it reads a portal 302 as a failure rather than as a reachable service.

### `--check` asks Authelia, never the clock

The expiry stamped in the state file is a claim, and the two come apart in exactly the cases
that matter: restarting Authelia or rotating `authelia_secret` invalidates every live session
while the local timestamp reads valid for weeks.

So `--check` calls `/api/state` and requires `authentication_level >= 1` — Authelia answers HTTP
200 with level 0 to a cookie it no longer honours, so neither the status code nor the timestamp
can stand as the verdict. An unreachable portal counts as invalid: minting needs the same network
the browsing does, so there is nothing useful to do with a session that cannot be confirmed.

### Going through Traefik is not a shortcut, it is the only path

Hitting a ClusterIP directly reaches only pods on the node you run from: the baseline
NetworkPolicy admits the two cni0 gateways alone (`netpol-baseline/defaults/main.yml:41`), and
host-to-remote-node traffic SNATs to flannel.1, which is not listed. `kubectl port-forward` does
not route around it either — the read-only ServiceAccount is denied `create pods/portforward`.

### The regression suite

`uv run pytest -m ui` (`scripts/diagnostics/test_ui_smoke.py`) drives this same MCP server over
stdio, so a break in the wrapper's DNS pin, session minting or launch config fails a test rather
than silently degrading a Claude session. The `ui` marker is deselected by `addopts`, because
these tests need the host's age key, LAN reachability and a browser — none of which a GitHub
runner has.

Pin the **exact** page title when adding a service. Several apps carry their own login behind
Authelia (`FreshRSS` lands on `/i/`, uptime-kuma on `/dashboard`, karakeep on `/signin`), so a
substring like `FreshRSS` also matches `Login · FreshRSS` and scores a broken app green.

### The `two_factor` services

**code-server, n8n and longhorn are `two_factor`**, so the ordinary session bounces off them at
the portal. Reach them by minting a second, short-lived session with a code from your
authenticator — `uv run python scripts/diagnostics/ui_login.py --totp <code>` — then launching
`ui_mcp.sh --two-factor`. The `-m ui` tests for those three skip when no such session is live.

**The TOTP secret is deliberately NOT in SOPS.** Storing it would put both factors under one age
key, and unlike a password its rotation costs a phone re-enrollment; nothing here runs unattended
anyway, since the `ui` marker is deselected in CI.

The two_factor session also gets its own state file and is never a fallback for the default one:
`ui_mcp.sh` loads a jar unconditionally, so promoting it would put a shell as the repo user
(code-server) and volume deletion (longhorn) behind every page load.

## What an edit costs, by file type

Six hooks match `Edit|Write`, and each is a ~7 ms no-op except on the paths it owns. Measured
directly 2026-08-23, one payload per hook: `ansible-lint` takes **1,642 ms** on
`roles/*/tasks/main.yml` and 7 ms on a `.j2` manifest, a Compose template or a Markdown file;
`validate-compose` takes **177 ms** on `docker-compose.yml.j2` and 7 ms on everything else.

Over the same 24h the OTEL telemetry put `PostToolUse:Edit` at a 559 ms average and
`PostToolUse:Write` at 234 ms — the two populations differ in which file types they touch, not in
which hooks run. So editing a tasks file is the slow case by an order of magnitude, and that is
the price of the coverage rather than overhead to trim. Recorded so a slow-feeling edit isn't
mistaken for a stuck hook.

## `auto-mode-bridge` internals

The two places auto mode and this repo have to talk (`PermissionDenied` + `PostToolUseFailure`,
both Bash).

On a **denial**, it retries `gitops_tick.sh` and nothing else: the tick is allow-listed and still
denied about 1 run in 7 on identical text, which is classifier variance rather than a rule, so
`retry: true` reissues the call and the classifier judges it again. Two retries per session, and
a compound command that merely contains the tick gets none — the classifier judged the whole line.

On a **failure**, it decodes `deploy.sh` exits 75/4/3/2 into what each one means, because all four
mean *nothing was deployed* and they reach Claude as a bare `Exit code N` that reads like a
playbook failure.

It does **not** use `classifierContext`: that field is PostToolUse-only, so a failed deploy can't
carry one, and the standing facts (public repo, read-only kubectl SA) already live in
`autoMode.environment` and `autoMode.allow`, where the classifier reads them as configuration
rather than as unverified application context.

## Permission auditing

No longer lives here. A `log-permission` hook used to count tool calls and prompts into
`.claude/logs/permissions.json` for `audit-permissions.py` to read; Claude Code's own OTEL
`tool_decision` events carry that now, and name the deciding authority (`config` rule, `hook`,
`user`) instead of leaving it inferred.

Both hosts' Claude Code exports OTLP to their local node's hostPort (127.0.0.1:4317); since
Phase F (2026-08-13) the cluster claude-otel collector is a DaemonSet with a loopback hostPort on
every node, so both hosts reach their own node's collector directly — the Docker forwarder is
dissolved/archived. The reader is the `claude-permission-audit` plugin (`/audit-permissions`),
installed globally rather than vendored per-repo.

### `/audit-permissions` breaks whenever Loki is not on the node you run it from

And the fix is not in this repo. Its `loki-source.js` hardcodes `LOKI_URL ||
http://127.0.0.1:3100` with no ClusterIP fallback and no retry, reporting "could not read Loki …
Set $LOKI_URL"; `$LOKI_URL` is unset in `settings.json`, the chezmoi base template, the shell rc
files and the plugin's own frontmatter, so the loopback default is what runs.

Loki, Prometheus and Tempo are Deployments with **no `nodeSelector`**, bound to `hostIP:
127.0.0.1` hostPorts — all three sit on daniel-box by scheduler luck, and a reboot can move them.
The 2026-08-23 ClusterIP pin (`c0d8731e`) gave `otelq` and `otel-sweep` a stable second address;
the plugin never got it.

Workaround: `LOKI_URL=http://10.43.99.158:3100`. Durable fix: apply the ClusterIP-fallback pattern
in the `daniel-tools` marketplace repo, which is where the plugin lives — an operator searching
under `ansible/` will not find it (2026-08-23b review M11).

Do **not** pin the workloads to a node; `roles/setup/k3s/defaults/main.yml:893-896` pre-rejects
that — fix the firewall, not the placement. In-cluster consumers (Grafana datasources,
monitor-bridge, autofix-bridge) use Service DNS and are unaffected.
