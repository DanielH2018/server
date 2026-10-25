"""Every `.gitleaks.toml` allowlist path matches a whole repo-relative path, never a substring.

gitleaks searches an allowlist `paths` regex rather than matching it against the whole path, so
an unanchored entry for `secrets.yml` also exempts `secrets.yml.bak` and a copy under another
directory, and a plaintext secret committed there goes unscanned. The
SOPS-ciphertext entries are the ones that matter, because each exempts a file from every
rule; each regex starts with `^` and ends with `$`.

Run: uv run pytest ansible/tests/repo/test_gitleaks_allowlist_paths_are_anchored.py
"""

import re
import tomllib

import pytest
from _helpers import REPO

CONFIG = tomllib.loads((REPO / ".gitleaks.toml").read_text())


def unanchored(paths: list[str]) -> list[str]:
    """The regexes in `paths` that do not both begin with `^` and end with `$`."""
    return [p for p in paths if not (p.startswith("^") and p.endswith("$"))]


def _paths() -> list[str]:
    return [p for a in CONFIG["allowlists"] for p in a.get("paths", [])]


def test_an_anchored_path_list_is_clean():
    assert unanchored([r"^ansible/vars/secrets\.yml$"]) == []


@pytest.mark.parametrize("path", [r"ansible/vars/secrets\.yml", r"^a", r"a$"])
def test_an_unanchored_path_is_flagged(path):
    assert unanchored([path]) == [path]


def test_an_anchored_regex_rejects_a_suffixed_or_prefixed_path():
    regex = re.compile(r"^ansible/vars/cluster-vars\.sops\.env$")
    assert regex.search("ansible/vars/cluster-vars.sops.env")
    assert not regex.search("ansible/vars/cluster-vars.sops.env.bak")
    assert not regex.search("x/ansible/vars/cluster-vars.sops.env")


def test_every_allowlist_path_in_gitleaks_toml_is_anchored():
    paths = _paths()
    assert any("secrets" in p for p in paths), "subject gone: the secrets.yml entry"
    assert any("cluster-vars" in p for p in paths), "subject gone: the cluster entries"
    assert unanchored(paths) == []


def test_the_per_host_sops_entry_exempts_a_host_file_and_nothing_beside_it():
    regexes = [re.compile(p) for p in _paths() if "vars/hosts" in p]
    assert regexes, "subject gone: the per-host SOPS entry"
    exempt = lambda path: any(r.search(path) for r in regexes)
    assert exempt("ansible/vars/hosts/daniel-server.sops.yaml")
    assert not exempt("ansible/vars/hosts/daniel-server.sops.yaml.bak")
    assert not exempt("ansible/vars/hosts/daniel-server.yaml")
    assert not exempt("ansible/vars/hosts/sub/daniel-server.sops.yaml")


def test_the_package_entry_exempts_a_package_root_s_host_file_and_nothing_beside_it():
    # A package root is scanned as `gitleaks dir .`, so its paths start at `usr/` (H4.1c).
    regexes = [re.compile(p) for p in _paths() if "usr/lib/homelab" in p]
    assert regexes, "subject gone: the package ciphertext entry"
    exempt = lambda path: any(r.search(path) for r in regexes)
    here = "usr/lib/homelab/secrets"
    assert exempt(f"{here}/daniel-server.sops.yaml")
    assert not exempt(f"{here}/daniel-server.sops.yaml.bak")
    assert not exempt(f"{here}/sub/daniel-server.sops.yaml")
    assert not exempt(f"etc/{here}/daniel-server.sops.yaml")
