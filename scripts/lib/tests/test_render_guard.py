"""The inventory readers every generator and validator shares.

``load_yaml`` replaced three per-script copies on 2026-09-01. Each of those coerced a
non-mapping file to ``{}``; the shared one must keep doing so, or a host_vars file whose
top level is a list reaches a ``.get`` several calls later.
"""

import os
import subprocess
from pathlib import Path

from render_guard import (
    HOST_VARS,
    HOST_VARS_IN_TREE,
    REPO,
    host_files,
    load_yaml,
    service_tags_at_or_none,
)
from repo_paths import ANSIBLE, INVENTORY, ROLES

# One commit declaring a service, and one adding a second. `deploy.sh --at <sha>` validates
# its tags against the commit it renders, so the answer must move with the ref.
_WITHOUT = "containers_list:\n  - name: sonarr\n    platform: k8s\n"
_WITH = _WITHOUT + "  - name: newsvc\n    platform: k8s\n"


def _tags_repo(tmp_path: Path, *texts: str) -> list[str]:
    """Commit each `texts` entry as the host_vars file in `tmp_path`; the shas, in order.

    Every ``GIT_*`` variable is scrubbed: under a prek hook an inherited ``GIT_DIR`` beats
    ``cwd``, and these commits would land in the real repository.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "GIT_AUTHOR_NAME": "t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def run(*args: str) -> str:
        return subprocess.run(
            args, cwd=tmp_path, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("git", "init", "-q", "-b", "master")
    host_vars = tmp_path / HOST_VARS_IN_TREE
    host_vars.mkdir(parents=True)
    shas = []
    for n, text in enumerate(texts):
        (host_vars / "daniel-box.yml").write_text(text)
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", f"c{n}", "--no-gpg-sign")
        shas.append(run("git", "rev-parse", "HEAD"))
    return shas


def test_service_tags_at_a_ref_sees_what_that_commit_declares(tmp_path):
    """CLEAN half: a role and its containers_list entry added together are declared at it."""
    shas = _tags_repo(tmp_path, _WITHOUT, _WITH)
    assert service_tags_at_or_none(shas[1], tmp_path) == {"sonarr", "newsvc"}


def test_the_same_tag_is_absent_at_the_commit_before_it(tmp_path):
    """The answer has to MOVE with the ref, or reading at one proves nothing."""
    shas = _tags_repo(tmp_path, _WITHOUT, _WITH)
    assert service_tags_at_or_none(shas[0], tmp_path) == {"sonarr"}


def test_an_unreadable_ref_is_none_rather_than_an_empty_set(tmp_path):
    """REJECTING half: `set()` says no service is declared anywhere, which refuses every tag.

    None is what sends the caller back to the tree it can read (issue #1331).
    """
    _tags_repo(tmp_path, _WITHOUT)
    assert service_tags_at_or_none("deadbeefdeadbeefdeadbeef", tmp_path) is None


def test_load_yaml_returns_a_mapping(tmp_path):
    path = tmp_path / "a.yml"
    path.write_text("key: value\n")
    assert load_yaml(path) == {"key": "value"}


def test_load_yaml_returns_empty_for_a_missing_file(tmp_path):
    assert load_yaml(tmp_path / "absent.yml") == {}


def test_load_yaml_returns_empty_for_an_empty_file(tmp_path):
    path = tmp_path / "empty.yml"
    path.write_text("")
    assert load_yaml(path) == {}


def test_load_yaml_returns_empty_for_a_non_mapping(tmp_path):
    path = tmp_path / "list.yml"
    path.write_text("- one\n- two\n")
    assert load_yaml(path) == {}


def test_host_files_skips_the_example_template(tmp_path):
    (tmp_path / "_example.yml").write_text("server_ip: 1.1.1.1\n")
    (tmp_path / "daniel-box.yml").write_text("server_ip: 2.2.2.2\n")
    assert [p.name for p in host_files(tmp_path)] == ["daniel-box.yml"]


def test_anchors_resolve_to_the_real_tree():
    """A wrong ``parents[N]`` reads as an empty inventory, not an error."""
    assert (REPO / "pyproject.toml").is_file()
    assert (ANSIBLE / "deploy.yml").is_file()
    assert (INVENTORY / "hosts.ini").is_file()
    assert (HOST_VARS / "daniel-box.yml").is_file()
    assert (ROLES / "k8s").is_dir()


def test_render_guard_re_exports_the_same_anchor_objects():
    from repo_paths import REPO as PATHS_REPO

    assert REPO == PATHS_REPO
    assert isinstance(REPO, Path)


def test_every_public_name_is_listed_in_all():
    """`__all__` drifted behind three helpers this module gained (issue #1776).

    Derived from the module rather than compared to a copied list: a hand-written expected
    list is the same drift one indirection away. Sorted, because the list is maintained in
    that order and an append at the end reads as correct until someone bisects it.
    """
    import types

    import render_guard

    public = {
        name
        for name, value in vars(render_guard).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", "render_guard") == "render_guard"
    }
    assert public - set(render_guard.__all__) == set(), sorted(
        public - set(render_guard.__all__)
    )
    assert render_guard.__all__ == sorted(render_guard.__all__)
    for name in render_guard.__all__:
        assert hasattr(render_guard, name), name
