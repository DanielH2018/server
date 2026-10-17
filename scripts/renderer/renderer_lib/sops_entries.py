"""The `sops-manifest-entries` check (target slice R2).

`sops_manifest_entries` holds each role's `render_sops_manifests` list, the manifests a
placeholder render leaves out. A wrong entry would hide a whole Secret from the publish and
from the round trip, so every placeholder render runs it and exits 1 on a problem. Each entry
must render exactly one document, of kind `Secret`, and the secret keys that document names
must be some of `SOPS_MANIFEST_KEYS` and nothing else. The keys are read from the entry's
sentinel render, so a key reached through an alias counts.

The third clause, `namer_problems`, is an equality: the entries' templates are exactly the
templates that name a `SOPS_MANIFEST_KEYS` key. A template outside the lists that names one
fails, and so does an entry whose template names none. It reads template sources, so it scans
the whole tree on every run, like rule 5. `secret_checks.check` runs it, because rule 5's
result is there.

`copy_sops_manifests` is the other half: a published tag's directory gets a byte copy of each
manifest `cluster_vars.py write` keeps for those entries (D3.3c).

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Iterable
from pathlib import Path

import yaml

from lib import yaml_fast
from renderer.renderer_lib.bundle import (
    KUSTOMIZATION,
    kustomization_text,
    sops_manifest_problem,
)
from renderer.renderer_lib.cluster import RenderedTag, RenderError, RenderRefused
from renderer.renderer_lib.placeholders import PlaceholderRender, Sentinels
from secrets_mgmt.cluster_vars_check import ManifestEntry

SOPS_MANIFEST_ENTRIES = "sops-manifest-entries"

# The keys no placeholder may carry. Flux deletes every newline from a substitution value, so
# a multi-line value cannot pass through a placeholder at all, and Flux types a standalone
# value by its text, so an all-digit one stops being a string. R2's PRs 5 to 7b move each into
# a dedicated Secret that D3 ships as a SOPS-encrypted manifest.
SOPS_MANIFEST_KEYS = frozenset(
    {
        # qbittorrent's Mullvad account number, all digits and standalone in its field.
        "mullvad_account",
        # pi-peer-backup's SSH private key, a PEM block.
        "pi_peer_backup_ssh_key",
        # Authelia's OIDC signing key, a PEM block under `jwks`.
        "authelia_oidc_rsa_key_content",
        # Home Assistant's Google service account, a JSON document.
        "google_assistant_service_account",
    }
)


def sops_manifest_entries(
    render: PlaceholderRender,
    keys: Iterable[str],
    sops_keys: frozenset[str] = SOPS_MANIFEST_KEYS,
) -> list[str]:
    """The `sops-manifest-entries` problems of `render`, one line each; empty when clean.

    Args:
        render: A placeholder render; only its rendered tags' entries are read.
        keys: The key names of `ansible/vars/secrets.yml`.
        sops_keys: The keys an entry may name.
    """
    sentinels = Sentinels(keys)
    problems = []
    for tag, entries in sorted(render.sops_manifests.items()):
        for entry, text in sorted(entries.items()):
            where = f"{SOPS_MANIFEST_ENTRIES}: {tag}/{entry}"
            if text is None:
                problems.append(f"{where} is listed, and the role renders no such file")
                continue
            try:
                docs = [d for d in yaml_fast.safe_load_all(text) if d is not None]
            except yaml.YAMLError as exc:
                problems.append(f"{where} does not parse: {exc}")
                continue
            kinds = [d.get("kind") if isinstance(d, dict) else None for d in docs]
            if kinds != ["Secret"]:
                problems.append(
                    f"{where} renders {len(docs)} documents of kinds {kinds}; an entry "
                    "renders exactly one Secret"
                )
                continue
            named = sentinels.key_set({entry: text})
            outside = sorted(named - sops_keys)
            if outside:
                problems.append(
                    f"{where} names {outside}, which are not in SOPS_MANIFEST_KEYS"
                )
            if not named & sops_keys:
                problems.append(f"{where} names no key in SOPS_MANIFEST_KEYS")
    return problems


def namer_problems(
    namers: Iterable[tuple[str, str]], listed: Iterable[str]
) -> list[str]:
    """The third clause's problems, one line each; empty when the two sets are equal.

    Args:
        namers: `(template, key)` for each `SOPS_MANIFEST_KEYS` key a template names.
        listed: The templates of every role's `render_sops_manifests` entries.
    """
    namers, listed = set(namers), set(listed)
    naming = {template for template, _key in namers}
    problems = [
        f"{SOPS_MANIFEST_ENTRIES}: {template} names {key} and is in no role's "
        "render_sops_manifests"
        for template, key in sorted(namers)
        if template not in listed
    ]
    problems += [
        f"{SOPS_MANIFEST_ENTRIES}: {template} is a render_sops_manifests entry and names "
        "no key in SOPS_MANIFEST_KEYS"
        for template in sorted(listed - naming)
    ]
    return problems


def copy_sops_manifests(
    rendered: list[RenderedTag],
    entries: dict[str, dict[str, str | None]],
    published: list[str],
    manifests_dir: Path,
) -> list[RenderedTag]:
    """`rendered` with each published tag's SOPS manifests copied byte for byte into it.

    The copy keeps the name `cluster_vars.py write` gives it, `<tag>.<entry>.sops.yaml`,
    and the tag's `kustomization.yaml` lists it.

    Raises:
        RenderRefused: A manifest cannot be read.
        RenderError: A manifest lacks the substitute-disabled annotation.
    """
    copied = []
    for tag in rendered:
        names = sorted(entries.get(tag.tag) or {}) if tag.tag in published else []
        if not names:
            copied.append(tag)
            continue
        files = {k: v for k, v in tag.files.items() if k != KUSTOMIZATION}
        for entry in names:
            name = ManifestEntry(tag.tag, entry, "", frozenset()).name
            try:
                text = (manifests_dir / name).read_text()
            except OSError as exc:
                raise RenderRefused(
                    f"{tag.tag} cannot copy its SOPS manifest: {exc}. cluster_vars.py "
                    "write (operator-only) writes it"
                ) from exc
            problem = sops_manifest_problem(name, text)
            if problem:
                raise RenderError(f"{tag.tag}: {problem}")
            files[name] = text
        files[KUSTOMIZATION] = kustomization_text(list(files))
        copied.append(RenderedTag(tag.tag, files, tag.deferred))
    return copied
