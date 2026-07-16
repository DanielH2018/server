# Anime profile baseline snapshot (reference only — NOT applied)

Read-only snapshot of Sonarr's manually-managed **"Anime"** quality profile, captured
2026-07-16 when the `configarr` role was introduced. Configarr does **not** apply these files;
they document the bespoke scheme so it is reviewable/trackable in git without Configarr taking
ownership of it. The live definitions remain in Sonarr's DB (Kopia-backed).

- `anime-profile.json` — the full profile as returned by `GET /api/v3/qualityprofile`
  (allowed qualities, `minFormatScore`/`cutoff`, and every custom-format score).
- `anime-cf-scores.json` — just the 52 non-zero custom-format scores, high→low. This is the
  bespoke `Anime Profile N_N_N` tier scheme that selects releases.

Refresh with the command in the role `CLAUDE.md`. The only scores Configarr actively enforces
are the two release-group CFs in `templates/config.yml.j2` (`Fake/Mislabeled Remux Groups`
= -10000, `Trusted Anime Groups` = +200).
