# Security Review — Detailed Guide

This guide defines the security standards for this homelab. The `/security-review` skill references this file to calibrate findings against project-specific expectations.

---

## Severity Ratings

| Level | Meaning | Examples |
|-------|---------|---------|
| **Critical** | Immediate risk of credential exposure or full compromise | Plaintext secret in committed file, privileged container with host network |
| **High** | Significant exposure if exploited | Admin panel reachable without Authelia, HTTP-only service, SOPS bypass |
| **Medium** | Increases attack surface or violates defence-in-depth | Unnecessary port exposed on host, missing resource limits, overly broad volume mount |
| **Low** | Best-practice gap with limited direct impact | Missing healthcheck, unpinned image tag, missing `no_log` on non-secret task |

---

## 1. Exposed Credentials / Secrets

### What to look for

- **Plaintext values** in any committed `.yml`, `.env`, `.j2`, or script file where a secret is expected
- **Hardcoded credentials** in `docker-compose.yml.j2` templates (passwords, API keys, tokens)
- **Unencrypted files** inside `ansible/vars/` or `ansible/inventory/` — these must be SOPS-encrypted
- **`.env` files** checked into git that contain real values instead of placeholders
- **Git history** exposure — check if a secret was ever committed even if later removed

### Standards for this project

- All secrets live in `ansible/vars/secrets.yml`, encrypted with SOPS + age
- Templates reference secrets as `{{ variable_name }}` — never inline values
- The `.sops.yaml` rule auto-encrypts any `.yml`/`.yaml` in `vars/` or `secrets/` directories
- Edit secrets only via `sops ansible/vars/secrets.yml`
- Tasks that handle credentials must set `no_log: true`

### Remediation pattern

```yaml
# BAD — hardcoded in template
environment:
  - PASSWORD=mysecretpassword

# GOOD — referenced from SOPS-encrypted vars
environment:
  - PASSWORD={{ my_service_password }}
```

---

## 2. Insecure Configurations

### Docker / Docker Compose

- **`privileged: true`** — flag as High unless there is a documented reason (e.g. WireGuard requires it); confirm it is intentional
- **`network_mode: host`** — flag as Medium; most services should use the `proxy` network
- **Volume mounts** — overly broad mounts (e.g. `- /:/host`) are Critical; mounting `/var/run/docker.sock` is High (grants full Docker control) — verify only `docker-proxy` uses it intentionally
- **`cap_add`** — only `NET_ADMIN` (WireGuard) and `SYS_TIME` (NTP) are expected; anything else is suspicious
- **Missing `restart: unless-stopped`** — Low; all services should restart automatically

### Traefik / Reverse Proxy

- **HTTP entrypoint without redirect** — all external services must redirect HTTP → HTTPS
- **Exposed dashboard** without authentication — Traefik dashboard must be behind Authelia or disabled
- **Missing TLS** on `traefik.http.routers.*.tls: true` label — flag as High
- **`insecureSkipVerify`** in Traefik config — flag as Medium

### Authelia

- Services reachable externally must have the Authelia forward-auth middleware applied:
  ```yaml
  traefik.http.routers.<name>.middlewares: authelia@docker
  ```
- Services that intentionally bypass Authelia (e.g. public-facing ones like Jellyfin) should have a comment explaining why
- Check `authelia/config/configuration.yml` access control rules for overly permissive `bypass` entries

### CrowdSec

- Confirm the CrowdSec bouncer middleware is applied on the Traefik entrypoint
- Flag any service that routes traffic externally without going through Traefik

---

## 3. Authentication & Authorization Gaps

### What to check

- **Every web-facing service** behind Traefik must either have Authelia middleware or a documented reason it does not (e.g. has its own auth, is public by design)
- **Exposed host ports** — services should not bind to `0.0.0.0:<port>` unless there is a specific reason (e.g. game servers, WireGuard UDP). Prefer letting Traefik handle ingress
- **Default credentials** — check if any service (Grafana, Portainer, etc.) is deployed without changing default admin passwords via vars

### Expected public/no-auth services

These intentionally skip Authelia:

| Service | Reason |
|---------|--------|
| Jellyfin | Has its own user auth |
| Healthchecks | Ping endpoints must be unauthenticated |
| WireGuard | UDP protocol, not HTTP |
| Game servers (Minecraft, Terraria, Valheim) | Game protocol, not HTTP |

Flag anything not on this list that lacks Authelia middleware.

### Remediation pattern

```yaml
# Add Authelia middleware to a Traefik-routed service
labels:
  - "traefik.http.routers.myservice.middlewares=authelia@docker"
```

---

## 4. SQL Injection & XSS

These risks are minimal in a pure Ansible/Docker infrastructure repo but apply to:

- **Python scripts** in `scripts/` — check for shell injection via `subprocess` with unsanitised input, or `os.system()` calls
- **N8N workflows** — if any workflow constructs SQL queries or HTTP requests from user-supplied data
- **Ansible `shell`/`command` tasks** — check for unquoted variables that could allow injection:
  ```yaml
  # BAD
  - shell: echo {{ user_input }}
  # GOOD — use a module, or quote and validate
  ```

---

## File Locations to Always Check

| Path | What to look for |
|------|-----------------|
| `ansible/vars/secrets.yml` | Must be SOPS-encrypted (first line should be `sops:`) |
| `ansible/roles/containers/*/templates/*.j2` | No hardcoded secrets |
| `containers/*/docker-compose.yml` | Read-only reference — flag if it contains plaintext secrets |
| `ansible/inventory/group_vars/all.yml` | No secrets, only non-sensitive vars |
| `ansible/inventory/host_vars/` | No secrets |
| `scripts/*.py` | Input sanitisation, no hardcoded credentials |
| `.env` files (if any) | Must not contain real values |
| `.gitignore` | Ensure `*.env`, `secrets/`, and runtime data are excluded |

---

## Reporting Format

Each finding should include:

```
[SEVERITY] Short title
File: path/to/file.yml (line N)
Issue: What the problem is.
Risk: What an attacker could do if exploited.
Fix: Specific remediation step.
```

Group findings by severity (Critical → High → Medium → Low). End the report with a summary count per severity level.
