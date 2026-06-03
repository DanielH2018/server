# home-assistant — Home automation platform (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Image:** `lscr.io/linuxserver/homeassistant:latest`
- **Intended:** port 8123 · apps net · Authelia: yes
- **Notable:** Ships a `templates/configuration.yaml.j2`. HA usually wants host networking
  / mDNS for device discovery and a `trusted_proxies` entry for Traefik — revisit those on
  reactivation.
