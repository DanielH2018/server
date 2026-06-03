# wallabag — Read-it-later / article saver (ARCHIVED)

**Not deployed.** Parked in `archive/`; see `../CLAUDE.md` for how to reactivate.

- **Images:** `wallabag/wallabag` + `mariadb` (DB) + `redis:alpine` (cache/queue)
- **Intended:** port 80 · apps net · Authelia: no
- **Notable:** Three-container stack. FreshRSS still ships a "wallabag-button" extension
  (`freshrss/files/`) from when this was active. Needs MariaDB creds + a `SYMFONY_ENV`
  config on reactivation.
