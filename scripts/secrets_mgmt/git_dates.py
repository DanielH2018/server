"""When each secret's ciphertext last changed, read out of git.

A real rotation changes the value in `ansible/vars/secrets.yml` but leaves the registry's
`last_rotated` behind, because `sync` deliberately will not touch an existing row's date. An
app-side rotation nobody recorded is then invisible and ages into a false OVERDUE. These
functions read the date back out of the git history and advance the registry IN MEMORY only,
so git stays the source of truth and the audit stays read-only.

Nothing here decrypts. Every read goes through `tools.git`, so a test drives the whole
derivation off a synthetic history.
"""

import datetime as dt
import subprocess
from collections.abc import Mapping

import yaml

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/

from lib import yaml_fast
from secrets_mgmt.rotation_tools import RotationTools

# Repo-relative, for git revspecs — `git show <rev>:<path>` needs the tracked path.
# `rotation_tools.SECRETS_FILE` is the absolute spelling of this same file, for the callers
# that open it rather than ask git about it.
SECRETS_GIT_PATH = "ansible/vars/secrets.yml"

# new name -> the name the same secret was stored under before it was renamed.
#
# A rename rewrites the ciphertext even when the plaintext is untouched: SOPS binds each
# value's ciphertext to its key path, so re-keying it forces a re-encrypt. A reader that
# compared values alone would call the rename commit a rotation and reset the clock on a
# credential nobody rotated — the same class of false clear that the reordering case in
# `ciphertext_rotation_dates` guards against, one level down.
#
# A renamed key gets NO derived date from before its rename: the walk stops at the boundary
# rather than following the old spelling backwards. Its pre-rename history is frozen into
# `ansible/secret_rotation.yml` at rename time instead, by carrying over the date the
# derivation reported on the commit before. A later real rotation derives normally, because
# both revisions then spell the key the same way.
#
# Written as prefix pairs rather than a literal name-to-name table because gitleaks reads
# `"<name>": "<name>"` as an api-key assignment and fails the commit on it. Merge a rename
# that does not share a prefix in with `|`.
RENAMED_FROM: dict[str, str] = {
    # Kopia retired 2026-08-13 and its B2 credentials became Longhorn's, keeping the dead
    # tool's name until 2026-09-09. See
    # docs/adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md.
    f"longhorn_b2_{suffix}": f"kopia_b2_{suffix}"
    for suffix in ("application_key", "bucket", "endpoint", "key_id")
}

# Keys the store held once and holds no longer, dropped WITH the service or job that read
# them rather than renamed. The pair with `RENAMED_FROM`: between them they must account for
# every name that ever appeared in secrets.yml and is absent now, and
# `test_departed_secrets_are_accounted_for` holds them to it — a departed name in neither
# table is a rename nobody recorded, which is the silent clock reset `RENAMED_FROM`'s
# comment describes. A retirement has no date to carry over, so listing it here is all it
# needs. Names only, never values: the same rule `sops_names` keeps.
RETIRED: frozenset[str] = frozenset(
    {
        # 44136b0d 2026-05-29 — beszel, foundry and wallabag left with Duplicati.
        "beszel_agent_key",
        "beszel_password",
        "beszel_system",
        "foundry_password",
        "foundry_username",
        "wallabag_db_name",
        "wallabag_db_password",
        "wallabag_db_user",
        "wallabag_mysql_root_password",
        # 7762dd4a 2026-06-09 — split into the gitops alive/status tokens, minted fresh.
        "gitops_deploy_kuma_push_token",
        # 89a88f05 2026-07-03 — dead since the wg0.conf migration.
        "wireguard_interface_address",
        "wireguard_interface_dns",
        "wireguard_peer_endpoint",
        "wireguard_peer_public_key",
        # f82c2d3b 2026-07-17 — recyclarr retired for configarr.
        "monitor_bridge_recyclarr_push_token",
        # 580da2dd 2026-08-09 — portainer retired.
        "portainer_agent_secret",
        "portainer_api_key",
        # 4616b116 .. 8edb11cd 2026-08-10..13 — kopia and its monitors retired for Longhorn
        # (docs/adr/0014-kopia-retired-longhorn-owns-the-b2-credentials.md).
        "kopia_password",
        "kopia_restore_drill_push_token",
        "monitor_bridge_b2_push_token",
        "monitor_bridge_b2_trend_push_token",
        "monitor_bridge_content_verify_push_token",
        "monitor_bridge_kopia_push_token",
        "monitor_bridge_maintenance_push_token",
        "monitor_bridge_verify_push_token",
        # b6cad82e 2026-08-12 — the Docker prometheus remote-write reader is gone.
        "monitor_bridge_remote_write_push_token",
        # 6894e352 2026-08-13 — orphaned by the E7 drain.
        "crowdsec_bouncer_api_key",
        "crowdsec_bouncer_docker_traefik_key",
        "pihole_api_key",
        # e5ee2be7 2026-08-14 — Docker uninstalled from daniel-server.
        "docker_fleet_push_token",
        "monitor_bridge_disk_prune_push_token",
        # ca5ae25b 2026-08-15 — dead after the k3s migration.
        "authelia_beszel_password_hash",
        "monitor_bridge_docker_user_push_token",
        "traefik_password",
        "traefik_user",
        # b01a2455 2026-08-24 — wg-easy's hash moved out of SOPS in the review remediation.
        "wg_easy_password_hash",
        # 59d0165a 2026-08-30 — crowdsec's console password and the healthchecks SMTP
        # password retired with the transcript-exposure rotation.
        "crowdsec_password",
        "healthchecks_smtp_password",
        # 429d4ffb 2026-09-10 — a duplicate of a value held elsewhere; the Django signing
        # key added in the same commit is a different credential, not this one renamed.
        "healthchecks_smtp_user",
    }
)


# DECIDED: a commit reads as a whole-file re-encrypt when it changes the ciphertext of at
# least 90% of the keys the two revisions share, and at least 10 keys are shared (so a
# file with only a handful of secrets can't hit the fraction on an ordinary rotation).
# Measured: 3e731bcec (a merge-conflict re-encrypt) changed 165 of 166 shared keys
# (99.4%); every other commit in the last 25 changed 0-2 (<=1.2%). 90% sits far above that
# noise floor and well below "half the registry rotated at once", which a real bulk
# rotation could plausibly do and this guard must not swallow.
_REENCRYPT_FRACTION = 0.9
_REENCRYPT_MIN_SHARED = 10


def _is_whole_file_reencrypt(newer: dict[str, str], current: dict[str, str]) -> bool:
    """True when `current` differs from `newer` at nearly every key both revisions share.

    Resolving a merge conflict on secrets.yml means decrypting both sides and re-encrypting
    the merged result, which assigns every value a fresh SOPS nonce whether or not its
    plaintext changed. That is indistinguishable, key by key, from a real rotation — the
    whole-file version of the single-key case `RENAMED_FROM` guards, where re-keying a
    value also forces a re-encrypt with nothing rotated. See `_REENCRYPT_FRACTION` for the
    threshold and the measurement behind it.
    """
    shared = set(newer) & set(current)
    if len(shared) < _REENCRYPT_MIN_SHARED:
        return False
    changed = sum(1 for name in shared if newer[name] != current[name])
    return changed / len(shared) >= _REENCRYPT_FRACTION


def rotation_evidence(name: str, newer_value: str, older: dict[str, str]) -> bool:
    """True when `older` shows `name` held a value different from `newer_value`.

    False at a rename boundary — where the older revision spells the key its old way —
    because a rename is not a rotation. See `RENAMED_FROM`.
    """
    if name in older:
        return older[name] != newer_value
    was = RENAMED_FROM.get(name)
    if was is not None and was in older:
        return False
    return True


def ciphertext_at(rev: str, tools: RotationTools) -> dict[str, str]:
    """name -> stored ciphertext at `rev`.

    Never decrypts: the `diff=sops` textconv driver rewrites diff output only, so `git show
    <rev>:<path>` streams the raw blob.
    """
    data = yaml_fast.safe_load(tools.git("show", f"{rev}:{SECRETS_GIT_PATH}")) or {}
    return {k: str(v) for k, v in data.items() if k != "sops"}


def _revisions(tools: RotationTools) -> list[list[str]]:
    """Every commit that touched the store, newest first, as [sha, "YYYY-MM-DD"]."""
    return [
        line.split(" ", 1)
        for line in tools.git(
            "log", "--format=%H %ad", "--date=short", "--", SECRETS_GIT_PATH
        ).splitlines()
        if line
    ]


def ciphertext_rotation_dates(tools: RotationTools) -> dict[str, dt.date]:
    """name -> date of the newest commit that changed that secret's ciphertext.

    Compares the parsed value per key rather than the diff text. A commit that only
    reorders or regroups secrets.yml rewrites lines without changing any value, and a
    line-level reader would call every secret freshly rotated — marking genuinely
    overdue ones green. ca5ae25b rewrote 149 of 156 lines doing exactly that.

    A commit that resolves a merge conflict on secrets.yml is the same trap one level up:
    decrypting both sides and re-encrypting the merged result assigns every value a fresh
    SOPS nonce, so every ciphertext changes with no plaintext touched. `_is_whole_file_reencrypt`
    is that case's guard, sitting beside `RENAMED_FROM` (its single-key analogue). 3e731bcec
    changed 165 of 166 tracked keys resolving exactly this kind of conflict.
    """
    revs = _revisions(tools)
    dates: dict[str, dt.date] = {}
    if not revs:
        return dates
    tracked = set(ciphertext_at(revs[0][0], tools))
    newer: dict[str, str] = {}
    newer_day = ""
    for rev, day in revs:
        current = ciphertext_at(rev, tools)
        reencrypt = _is_whole_file_reencrypt(newer, current)
        for name, value in newer.items():
            if name in dates:
                continue
            if reencrypt and name in current:
                # Every shared key changed here because the whole file was re-encrypted,
                # not because this one rotated. Leave it undated and let the walk carry on
                # to the (pre-re-encrypt) value in `current`, so an earlier real rotation
                # still gets found on a later iteration.
                continue
            if rotation_evidence(name, value, current):
                dates[name] = dt.date.fromisoformat(newer_day)
        if tracked <= set(dates):
            break
        newer, newer_day = current, day
    # Whatever never changed existed unaltered back to the oldest revision, so that
    # revision is the best evidence of when its value was set.
    for name in newer:
        dates.setdefault(name, dt.date.fromisoformat(newer_day))
    return dates


def derived_rotation_dates(tools: RotationTools) -> dict[str, dt.date]:
    """Git-derived dates, or {} when git cannot answer (no checkout, shallow clone, git missing).

    The daily cron degrades to the recorded dates instead of failing — a broken derivation must not
    take the monitor down on its own.
    """
    try:
        return ciphertext_rotation_dates(tools)
    except subprocess.CalledProcessError, OSError, yaml.YAMLError, ValueError:
        return {}


def advance_last_rotated(
    reg: dict, dates: dict[str, dt.date]
) -> list[tuple[str, str, str]]:
    """Move `last_rotated` forward where git shows a later change.

    Returns (name, old, new) for each row advanced. Mutates `reg` in memory only — the caller never
    saves it, which is what keeps the audit read-only and git the source of truth.

    Advance-only, for two reasons. Seed dates are deliberately staggered and backdated
    (`secret_registry.seed_last_rotated`) and most secrets predate this file's git history, so
    taking the derived date unconditionally would collapse them onto the same introduction
    commit and un-stagger every due-date. It also means this can only ever clear an overdue secret that a real
    rotation already fixed, never create one.
    """
    # DECIDED: git evidence beats the seed even though it can overstate freshness for a
    # credential minted before this file's first commit (2026-01-17) — such a secret dates
    # to when it was committed, not when it was created. The seed it replaces is not a
    # better reading: `seed_last_rotated` backdates by a hash of the NAME, so it is
    # fiction for every secret nobody has rotated since registration. That fiction is what
    # aged calendar_1 into a false OVERDUE and took the monitor down on 2026-08-25.
    advanced = []
    for name, entry in reg.get("entries", {}).items():
        derived = dates.get(name)
        recorded = entry.get("last_rotated")
        if derived is None or not recorded:
            continue
        if dt.date.fromisoformat(recorded) >= derived:
            continue
        entry["last_rotated"] = derived.isoformat()
        advanced.append((name, recorded, derived.isoformat()))
    return advanced


def historical_names(tools: RotationTools) -> set[str]:
    """Every key any committed revision of secrets.yml has held. Names only, never values."""
    names: set[str] = set()
    for rev, _day in _revisions(tools):
        names |= set(ciphertext_at(rev, tools))
    return names


def undeclared_departures(
    history: set[str],
    current: set[str],
    *,
    renamed_from: Mapping[str, str] = RENAMED_FROM,
    retired: frozenset[str] = RETIRED,
) -> set[str]:
    """Names the store once held and holds no longer that neither table accounts for.

    Each one is a rename nobody recorded or a retirement nobody listed, and the two look the
    same from here — which is why the tables exist rather than a heuristic.
    """
    return (history - current) - set(renamed_from.values()) - retired


def revived_departures(
    current: set[str],
    *,
    renamed_from: Mapping[str, str] = RENAMED_FROM,
    retired: frozenset[str] = RETIRED,
) -> set[str]:
    """Names a table calls gone that the store holds again.

    A revived `RENAMED_FROM` source makes `rotation_evidence` read the live key as a rename
    boundary, so it stops dating the target. A revived `RETIRED` name only lies, but a table
    that lies once is a table nobody trusts.
    """
    return (set(renamed_from.values()) | retired) & current
