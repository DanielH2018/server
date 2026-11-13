#!/usr/bin/env python3
"""`role_callers` — which k8s roles `lookup()` another role, read from the live tree.

The consumer is `scripts/deploy_tools/land_lib/land_tags.py`: a helper role whose callers were ALL
deployed is already applied, and reporting it as `needs-manual-apply` sends an operator to apply
it by hand for nothing. A census that silently returned an empty map
would suppress no note at all here, but the same map is what decides suppression — so the
non-vacuity half below names the roles it must find rather than counting them.

Run: uv run pytest scripts/lib/tests/test_k8s_role_callers.py
"""

import pytest

from lib.k8s_roles import k8s_entries, role_callers
from lib.k8s_role_callers import CannotNarrow

# The helper roles: every role under ansible/roles/k8s/ that another role reaches and that has
# no containers_list entry of its own. Named, not counted -- a rename or a moved template
# leaves a count-based assertion passing over a map that lost the member it was checking.
# cronjob-gate left the set when configarr and qbittorrent, its last callers, flipped to Flux.
_HELPERS = frozenset(
    {
        "arr-notification",
        "image-builder",
        "volume-snapshot",
    }
)


@pytest.fixture(scope="module")
def callers():
    return role_callers()


def test_every_named_helper_has_at_least_one_caller(callers):
    """Non-vacuity. A helper with no caller derived means a task form the walk misses."""
    missing = sorted(h for h in _HELPERS if not callers.get(h))
    assert not missing, (
        f"no caller derived for {missing} -- the lookup or defaults form changed?"
    )


def test_the_defaults_form_resolves(callers):
    assert {"code-server", "n8n"} <= callers["image-builder"]
    assert "freshrss" in callers["volume-snapshot"]


def test_a_defaults_key_is_an_edge_and_a_mention_in_a_comment_is_not(tmp_path):
    roles = tmp_path / "ansible" / "roles" / "k8s"
    for role, text in (
        ("builder", "image_builder_builds: []\n"),
        ("mentions", "# image_builder_builds lives elsewhere\n"),
    ):
        (roles / role / "defaults").mkdir(parents=True)
        (roles / role / "defaults" / "main.yml").write_text(text)
    assert role_callers(tmp_path) == {"image-builder": {"builder"}}


def test_the_lookup_form_resolves(callers):
    """sonarr and radarr reach arr-notification only through their seed Jobs' `lookup()`s."""
    assert callers["arr-notification"] == {"radarr", "sonarr"}


def test_a_template_lookup_into_another_role_is_an_edge(tmp_path):
    """The accept half on a throwaway tree; a lookup of the reader's own file is no edge."""
    verify = tmp_path / "ansible" / "roles" / "k8s" / "reader" / "templates" / "verify"
    verify.mkdir(parents=True)
    (verify / "job.yaml.j2").write_text(
        "data: {{ lookup('file', role_file('files/x.py', role='helper')) }}\n"
        "own: {{ lookup('file', role_file('files/y.py')) }}\n"
    )
    assert role_callers(tmp_path) == {"helper": {"reader"}}


def test_a_b64file_lookup_into_another_role_is_an_edge(tmp_path):
    templates = tmp_path / "ansible" / "roles" / "k8s" / "reader" / "templates"
    templates.mkdir(parents=True)
    (templates / "x.yaml.j2").write_text(
        "x: {{ lookup('b64file', role_file('files/a.png', role='helper')) }}\n"
    )
    assert role_callers(tmp_path) == {"helper": {"reader"}}


def test_a_lookup_with_keyword_arguments_is_an_edge(tmp_path):
    """A keyword argument after the path, such as `rstrip=False`, does not hide the edge."""
    templates = tmp_path / "ansible" / "roles" / "k8s" / "reader" / "templates"
    templates.mkdir(parents=True)
    (templates / "x.yaml.j2").write_text(
        "x: \"{{ lookup('template', role_file('templates/a.j2', role='helper'),"
        ' rstrip=False) }}"\n'
    )
    assert role_callers(tmp_path) == {"helper": {"reader"}}


def test_a_role_nothing_looks_up_is_not_a_callee(callers):
    """The reject half: a walk matching any `k8s/` string would call sonarr a helper.

    A service can be a callee: crowdsec and monitor-bridge are, through other roles' lookups.
    sonarr and jellyfin are named because nothing reads their files.
    """
    assert "sonarr" not in callers
    assert "jellyfin" not in callers


def test_the_helpers_are_still_undeclared():
    """The suppression only applies to roles with no tag of their own."""
    declared = set(k8s_entries())
    assert not (_HELPERS & declared), "a helper grew a containers_list entry"
    assert "sonarr" in declared, "the reject half: an empty lookup would pass"


def test_a_named_repo_is_walked_instead_of_this_checkout(tmp_path, callers):
    """`role_callers(X)` must read X's caller graph, not this file's checkout.

    Both halves in one test, because the fixture tree is what makes the reject half
    meaningful: the derived edge is the throwaway tree's, and none of the live tree's
    helpers may appear in it.
    """
    templates = tmp_path / "ansible" / "roles" / "k8s" / "caller" / "templates"
    templates.mkdir(parents=True)
    (templates / "x.yaml.j2").write_text(
        "data: {{ lookup('file', role_file('files/x.py', role='helper')) }}\n"
    )

    derived = role_callers(tmp_path)

    assert derived == {"helper": {"caller"}}, derived
    assert callers, "the live graph is empty, so the reject half below proves nothing"
    assert not (set(derived) & set(callers)), derived


def test_a_dangling_template_symlink_refuses_by_name(tmp_path):
    templates = tmp_path / "ansible" / "roles" / "k8s" / "caller" / "templates"
    templates.mkdir(parents=True)
    (templates / "bad.yaml.j2").symlink_to(tmp_path / "gone.yaml.j2")
    with pytest.raises(CannotNarrow, match=r"bad\.yaml\.j2"):
        role_callers(tmp_path)
