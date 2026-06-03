# beszel — Lightweight server monitoring (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Images:** `henrygd/beszel:latest` (hub) + `henrygd/beszel-agent:latest` (agent, `network_mode: host`)
- **Intended:** port 8090 · monitoring net · Authelia: no (uses its own OIDC)
- **Notable:** Authelia config still provisions a Beszel OIDC client/secret (per recent
  hardening), so partial wiring exists even though the container is parked. The agent
  runs on the host network to read system stats.
