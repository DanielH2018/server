# guacamole — Clientless remote desktop gateway (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Images:** `guacamole/guacamole` (web) + `guacamole/guacd:latest` (proxy daemon)
  + `postgres:15.2-alpine` (auth/connection DB)
- **Intended:** apps net · Authelia: yes (no commented `containers_list` entry survives, so
  confirm port/networks on reactivation — Guacamole's webapp is typically `:8080/guacamole`)
- **Notable:** Three-container stack — needs the Postgres DB initialized (schema SQL) before
  first run.
