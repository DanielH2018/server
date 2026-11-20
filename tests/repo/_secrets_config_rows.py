"""Census rows over the two secrets-scanning configs: `.gitleaks.toml` and `secrets/.sops.yaml`.

`test_census_rows_roles.py` runs them. A row parses its file in its offence, and its red half
is a small document that breaks the one property the row holds.
"""

import re
import tomllib

import yaml
from _helpers import REPO
from _row_table import Census, Subject
from lib import yaml_fast

# ── .gitleaks.toml ─────────────────────────────────────────────────────────────────────

GITLEAKS = ".gitleaks.toml"
_ANCHORED = (r"^secrets/secrets\.yml$", r"^secrets/cluster-vars\.sops\.env$")
_HOST_FILE = "secrets/hosts/daniel-server.sops.yaml"
_PACKAGE_DIR = "usr/lib/homelab/secrets"


def _toml(*paths: str) -> Subject:
    return Subject(
        GITLEAKS,
        "[[allowlists]]\npaths = [" + ", ".join(f"'''{p}'''" for p in paths) + "]\n",
    )


def _allowlist_paths(subject: Subject) -> list[str]:
    return [
        p
        for allowlist in tomllib.loads(subject.text).get("allowlists", [])
        for p in allowlist.get("paths", [])
    ]


def _unanchored_paths(subject: Subject) -> list[str]:
    paths = _allowlist_paths(subject)
    found = [
        f"{p!r} lacks ^ or $"
        for p in paths
        if not (p.startswith("^") and p.endswith("$"))
    ]
    found += [
        f"no entry for {name}"
        for name in ("secrets", "cluster-vars")
        if not any(name in p for p in paths)
    ]
    return found


def _exempts_only(marker: str, exempt: tuple[str, ...], refuse: tuple[str, ...]):
    def offence(subject: Subject) -> list[str]:
        regexes = [re.compile(p) for p in _allowlist_paths(subject) if marker in p]
        if not regexes:
            return [f"no allowlist path names {marker}"]
        covers = lambda path: any(r.search(path) for r in regexes)
        return [f"does not exempt {p}" for p in exempt if not covers(p)] + [
            f"exempts {p}" for p in refuse if covers(p)
        ]

    return offence


# ── secrets/.sops.yaml ─────────────────────────────────────────────────────────────────

SOPS_CONFIG = "secrets/.sops.yaml"
_SECRETS, _CLUSTER_FILE = "secrets.yml", "cluster-vars.sops.env"
_CLUSTER_MANIFEST = "cluster-manifests/pi-peer-backup.sops.yaml"
_OUTSIDE_SECRETS = (
    "inventory/group_vars/all.yml",
    "../inventory/group_vars/all.yml",
    f"{REPO}/inventory/group_vars/all.yml",
    f"{REPO}/hosts/daniel-pi.sops.yaml",
    f"{REPO}/k8s/roles/traefik/defaults/main.yml",
)


def _sops(*rules: dict) -> Subject:
    return Subject(SOPS_CONFIG, yaml.safe_dump({"creation_rules": list(rules)}))


def _creation_rules(generic="a,b", cluster="a,b,c", *, generic_first=False) -> Subject:
    cluster_rules = [
        {"path_regex": r"(^|secrets/)cluster-vars\.sops\.env$", "age": cluster},
        {
            "path_regex": r"(^|secrets/)cluster-manifests/[^/]+\.sops\.yaml$",
            "encrypted_regex": "^(data|stringData)$",
            "age": cluster,
        },
    ]
    generic_rule = {
        "path_regex": r"(^|secrets/)(cluster-manifests/)?[^/]+\.ya?ml$",
        "age": generic,
    }
    if generic_first:
        return _sops(generic_rule, *cluster_rules)
    return _sops(*cluster_rules, generic_rule)


def _rule_for(subject: Subject, path: str) -> dict | None:
    """The rule SOPS picks for `path`: the first whose `path_regex` matches."""
    rules = yaml_fast.safe_load(subject.text)["creation_rules"]
    return next((rule for rule in rules if re.search(rule["path_regex"], path)), None)


def _recipients(rule: dict) -> set[str]:
    return {key.strip() for key in rule["age"].split(",")}


def _path_form_drift(subject: Subject) -> list[str]:
    found = []
    for path in (_SECRETS, _CLUSTER_FILE, _CLUSTER_MANIFEST):
        base = _rule_for(subject, path)
        if base is None:
            found.append(f"no creation rule matches {path}")
        found += [
            f"{prefix}{path} resolves to a different rule than {path}"
            for prefix in ("secrets/", f"{REPO}/secrets/")
            if _rule_for(subject, prefix + path) != base
        ]
    return found


def _rules_matching_outside_secrets(subject: Subject) -> list[str]:
    return [
        f"a rule matches {path}"
        for path in _OUTSIDE_SECRETS
        if _rule_for(subject, path)
    ]


def _cluster_rule_problems(subject: Subject) -> list[str]:
    rules = {
        path: _rule_for(subject, path)
        for path in (_SECRETS, _CLUSTER_FILE, _CLUSTER_MANIFEST)
    }
    if unmatched := [path for path, rule in rules.items() if rule is None]:
        return [f"no creation rule matches {path}" for path in unmatched]
    rules = {path: rule for path, rule in rules.items() if rule is not None}
    secrets, cluster_file = (
        _recipients(rules[_SECRETS]),
        _recipients(rules[_CLUSTER_FILE]),
    )
    found = []
    # One extra key, not a superset: a host key dropped from the generic rule alone still leaves a
    # superset, and that host would keep decrypting every cluster file.
    if not secrets <= cluster_file or len(cluster_file - secrets) != 1:
        found.append(
            f"{_CLUSTER_FILE} must go to every {_SECRETS} recipient plus exactly one cluster key"
        )
    if _recipients(rules[_CLUSTER_MANIFEST]) != cluster_file:
        found.append(
            f"{_CLUSTER_MANIFEST} resolves below the generic rule, or to other keys than {_CLUSTER_FILE}"
        )
    if rules[_CLUSTER_MANIFEST].get("encrypted_regex") != "^(data|stringData)$":
        found.append(
            f"{_CLUSTER_MANIFEST}'s rule must encrypt only data and stringData"
        )
    return found


SECRETS_CONFIG_ROWS = (
    Census(
        name="gitleaks-allowlist-paths-are-anchored",
        reason=(
            "gitleaks searches an allowlist `paths` regex rather than matching it against the "
            "whole path. An unanchored entry for `secrets.yml` also exempts `secrets.yml.bak` and a "
            "copy under another directory, and a plaintext secret committed there goes unscanned. "
            "The SOPS-ciphertext entries matter most, because each exempts a file from every rule. "
            "Each regex starts with `^` and ends with `$`. The row also names the `secrets` and "
            "`cluster-vars` entries it must find."
        ),
        files=lambda: [GITLEAKS],
        offence=_unanchored_paths,
        red=(
            *(
                _toml(*_ANCHORED, bad)
                for bad in (r"secrets/secrets\.yml", r"^a", r"a$")
            ),
            _toml(_ANCHORED[0]),
        ),
        green=(_toml(*_ANCHORED),),
    ),
    Census(
        name="gitleaks-per-host-sops-entry-exempts-one-host-file",
        reason=(
            "The per-host SOPS entry exempts a host's ciphertext file and nothing beside it: not "
            "a `.bak` copy, a file without `.sops`, or one in a subdirectory. Each is a place a "
            "plaintext secret could be committed unscanned."
        ),
        files=lambda: [GITLEAKS],
        offence=_exempts_only(
            "secrets/hosts",
            (_HOST_FILE,),
            (
                f"{_HOST_FILE}.bak",
                "secrets/hosts/daniel-server.yaml",
                "secrets/hosts/sub/daniel-server.sops.yaml",
            ),
        ),
        red=(
            _toml(r"secrets/hosts/[^/]+\.sops\.yaml"),
            _toml(r"^secrets/hosts/x\.sops\.yaml$"),
        ),
        green=(_toml(r"^secrets/hosts/[^/]+\.sops\.yaml$"),),
    ),
    Census(
        name="gitleaks-package-entry-exempts-one-package-host-file",
        reason=(
            "A package root is scanned as `gitleaks dir .`, so its paths start at `usr/` (H4.1c). "
            "The package entry exempts a root's host ciphertext file and nothing beside it: not "
            "a `.bak` copy, a file in a subdirectory, or the same path under another prefix."
        ),
        files=lambda: [GITLEAKS],
        offence=_exempts_only(
            "usr/lib/homelab",
            (f"{_PACKAGE_DIR}/daniel-server.sops.yaml",),
            (
                f"{_PACKAGE_DIR}/daniel-server.sops.yaml.bak",
                f"{_PACKAGE_DIR}/sub/daniel-server.sops.yaml",
                f"etc/{_PACKAGE_DIR}/daniel-server.sops.yaml",
            ),
        ),
        red=(
            _toml(r"usr/lib/homelab/secrets/[^/]+\.sops\.yaml"),
            _toml(r"^usr/lib/homelab/x$"),
        ),
        green=(_toml(r"^usr/lib/homelab/secrets/[^/]+\.sops\.yaml$"),),
    ),
    Census(
        name="sops-creation-rules-resolve-from-any-path-form",
        reason=(
            "SOPS matches the path relative to `secrets/`, but `write` passes a repo-root or "
            "absolute `--filename-override`. Every form must pick the same rule for `secrets.yml`, "
            "the cluster file and a cluster manifest. Rule resolution follows `parseCreationRuleForFile` "
            "in sops `config/config.go`: the first `re.search` match wins."
        ),
        files=lambda: [SOPS_CONFIG],
        offence=_path_form_drift,
        red=(
            _sops(
                {"path_regex": r"^cluster-vars\.sops\.env$", "age": "a"},
                {"path_regex": r"(^|secrets/)[^/]+\.ya?ml$", "age": "a"},
            ),
        ),
        green=(_creation_rules(),),
    ),
    Census(
        name="sops-creation-rules-match-no-yaml-file-outside-secrets",
        reason=(
            "`--config secrets/.sops.yaml` from the repo root must not encrypt a file elsewhere, "
            "so no rule's `path_regex` matches an inventory, host or role YAML file. "
            "`inventory/` is not auto-encrypted, and an encrypted copy there would hide a diff."
        ),
        files=lambda: [SOPS_CONFIG],
        offence=_rules_matching_outside_secrets,
        red=(_sops({"path_regex": r"\.ya?ml$", "age": "a"}),),
        green=(_creation_rules(),),
    ),
    Census(
        name="sops-cluster-rules-resolve-as-adr-0022-decides",
        reason=(
            "SOPS takes the first creation rule whose `path_regex` matches, and the generic rule "
            "also matches every cluster manifest. A cluster rule moved below it stops applying "
            "without an error, and the next manifest is encrypted without the cluster key. A cluster "
            "key added to the generic rule hands the cluster `secrets.yml` (ADR-0022). Neither "
            "mistake shows until the cluster fails to decrypt, or never fails at all. The row names "
            "no key, so rotating the cluster key or a host key needs no change here. It does not "
            "compare a real file's recipients against its rule: `cluster_vars.py check` (slice "
            "D3.2a) owns that."
        ),
        files=lambda: [SOPS_CONFIG],
        offence=_cluster_rule_problems,
        red=(
            _creation_rules(generic_first=True),
            _creation_rules(generic="a,b,c"),
            _creation_rules(generic="b"),
        ),
        green=(_creation_rules(),),
    ),
)
