"""`PINNED_LIVE_FACTS` names every tag whose templates read a pinned fact, and no other.

Later slices key two publish guards on those reader tags. `k8s_built_images` pins to an empty
list, which renders as `"unstaged"` and carries no stub token, so a reader the constant
misses would publish a stand-in that no value census can see. The tags are therefore derived
here from a word grep of the k8s templates and `ansible/templates/`, and held equal.

Run: uv run pytest scripts/renderer/tests/test_renderer_pinned_live_facts.py
"""

import re
from pathlib import Path

from lib.repo_paths import ANSIBLE, K8S_ROLES
from renderer.renderer_lib.task_inputs import PINNED_LIVE_FACTS
from secret_stubs import homelab_secret_stub

SHARED = ANSIBLE / "templates"


def _reader_files(fact: str, roles: Path, shared: Path) -> list[Path]:
    word = re.compile(rf"(?<!\w){re.escape(fact)}(?!\w)")
    files = [*roles.glob("*/templates/**/*"), *shared.glob("**/*")]
    return sorted(p for p in files if p.is_file() and word.search(p.read_text()))


def _readers(fact: str, roles: Path = K8S_ROLES, shared: Path = SHARED) -> set[str]:
    """The role directories whose templates name `fact`; a shared template counts as itself.

    A shared macro reaches every role that imports it, so a hit there names its own path and
    can never equal a tag set.
    """
    return {
        p.relative_to(roles).parts[0] if p.is_relative_to(roles) else str(p)
        for p in _reader_files(fact, roles, shared)
    }


def test_reader_tags_equal_a_word_grep_of_the_templates():
    assert len(PINNED_LIVE_FACTS) == 5
    derived = {name: _readers(name) for name in PINNED_LIVE_FACTS}
    assert derived == {
        name: set(fact.readers) for name, fact in PINNED_LIVE_FACTS.items()
    }


def test_reader_grep_finds_the_authelia_config_secret():
    must_find = K8S_ROLES / "authelia" / "templates" / "config-secret.yaml.j2"
    assert must_find in _reader_files("authelia_password_hash", K8S_ROLES, SHARED)


def test_string_stand_ins_carry_the_stub_marker():
    for name, fact in PINNED_LIVE_FACTS.items():
        if isinstance(fact.value, str):
            assert fact.value == homelab_secret_stub(name)
    assert PINNED_LIVE_FACTS["k8s_built_images"].value == []


def test_reader_under_a_listed_tag_is_clean(tmp_path):
    roles, shared = tmp_path / "roles", tmp_path / "templates"
    (roles / "pi-peer-backup" / "templates").mkdir(parents=True)
    (roles / "pi-peer-backup" / "templates" / "secret.yaml.j2").write_text(
        "line: {{ pi_peer_backup_known_hosts_line }}\n"
    )
    shared.mkdir()
    readers = _readers("pi_peer_backup_known_hosts_line", roles, shared)
    assert readers == set(PINNED_LIVE_FACTS["pi_peer_backup_known_hosts_line"].readers)


def test_reader_under_an_unlisted_tag_is_flagged(tmp_path):
    roles, shared = tmp_path / "roles", tmp_path / "templates"
    for role in ("pi-peer-backup", "littlelink"):
        (roles / role / "templates").mkdir(parents=True)
        (roles / role / "templates" / "secret.yaml.j2").write_text(
            "line: {{ pi_peer_backup_known_hosts_line }}\n"
        )
    shared.mkdir()
    readers = _readers("pi_peer_backup_known_hosts_line", roles, shared)
    assert readers != set(PINNED_LIVE_FACTS["pi_peer_backup_known_hosts_line"].readers)
    assert "littlelink" in readers
