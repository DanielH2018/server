"""The cluster pin checks' `_is_clean` / `_is_flagged` pairs, one per refusal of H1.1f.

Most pairs judge synthetic pins through `problems`. The key-move case and the exit codes run
`check` against a scratch git repository, so the `git grep` that finds each pin is exercised
too, and one test reads the real tree's two pins.

Run: uv run pytest scripts/validate/tests/test_validate_cluster_pins.py
"""

from pathlib import Path

import pytest
from lib import proc_testing

from validate import cluster_pins

K3S = "ansible/roles/setup/k3s/defaults/main.yml"
LONGHORN = "ansible/roles/k8s/longhorn/defaults/main.yml"
METALLB = cluster_pins.Pin(K3S, "v0.16.0")


def pins(longhorn: str, metallb: cluster_pins.Pin = METALLB, path: str = K3S) -> dict:
    return {
        "k3s_longhorn_version": cluster_pins.Pin(path, longhorn),
        "k3s_metallb_version": metallb,
    }


def judge(old: str, new: str, paths=(K3S,)) -> list[str]:
    return cluster_pins.problems(pins(old), pins(new), paths)


def test_a_longhorn_patch_bump_is_clean():
    assert judge("v1.12.1", "v1.12.2") == []


def test_a_longhorn_decrease_is_flagged():
    flagged = judge("v1.12.1", "v1.12.0")
    assert len(flagged) == 1
    assert "decreases from v1.12.1 to v1.12.0" in flagged[0]


def test_a_metallb_decrease_is_flagged():
    old = pins("v1.12.1")
    new = pins("v1.12.1", metallb=cluster_pins.Pin(K3S, "v0.15.3"))
    flagged = cluster_pins.problems(old, new, [K3S])
    assert len(flagged) == 1
    assert "k3s_metallb_version decreases" in flagged[0]


def test_a_longhorn_bump_to_the_next_minor_is_clean():
    assert judge("v1.12.1", "v1.13.0") == []


def test_a_longhorn_bump_that_skips_a_minor_is_flagged():
    flagged = judge("v1.12.1", "v1.14.0")
    assert len(flagged) == 1
    assert "skips a minor release" in flagged[0]


def test_a_longhorn_major_bump_is_flagged():
    assert len(judge("v1.13.0", "v2.0.0")) == 1


def test_a_metallb_bump_across_two_minors_is_clean():
    """Only Longhorn moves one minor at a time."""
    old = pins("v1.12.1")
    new = pins("v1.12.1", metallb=cluster_pins.Pin(K3S, "v0.18.0"))
    assert cluster_pins.problems(old, new, [K3S]) == []


def test_a_bump_beside_its_role_files_and_docs_is_clean():
    paths = [K3S, "ansible/roles/setup/k3s/files/upstream/longhorn.yaml", "docs/x.md"]
    assert judge("v1.12.1", "v1.12.2", paths) == []


def test_a_bump_that_also_edits_another_role_is_flagged():
    paths = [K3S, "ansible/roles/k8s/authelia/defaults/main.yml"]
    flagged = judge("v1.12.1", "v1.12.2", paths)
    assert len(flagged) == 1
    assert flagged[0].startswith("ansible/roles/k8s/authelia/defaults/main.yml: ")


def test_two_pins_bumped_in_one_pr_is_flagged():
    old = pins("v1.12.1")
    new = pins("v1.12.2", metallb=cluster_pins.Pin(K3S, "v0.16.1"))
    flagged = cluster_pins.problems(old, new, [K3S])
    assert len(flagged) == 1
    assert "both move in one PR" in flagged[0]


def test_a_pin_moved_to_another_role_at_the_same_version_is_clean():
    """No value changes, so neither the decrease nor the alone check applies."""
    old = pins("v1.12.1")
    new = pins("v1.12.1", path=LONGHORN)
    paths = [K3S, LONGHORN, "ansible/roles/setup/k3s/tasks/longhorn.yml"]
    assert cluster_pins.problems(old, new, paths) == []


def test_a_pin_removed_at_the_head_is_flagged():
    new = {"k3s_longhorn_version": None, "k3s_metallb_version": METALLB}
    assert len(cluster_pins.problems(pins("v1.12.1"), new, [K3S])) == 1


def test_a_value_that_is_not_a_version_is_unjudged():
    with pytest.raises(cluster_pins.Unjudged):
        judge("v1.12.1", "latest")


def test_the_real_tree_holds_both_pins_in_one_role_file_each():
    found = cluster_pins.pins_at("HEAD")
    assert set(found) == {"k3s_longhorn_version", "k3s_metallb_version"}
    for key, pin in found.items():
        assert pin is not None, f"no role YAML under ansible/roles sets {key}"
        cluster_pins.version(pin)


def _git(repo: Path, *args: str) -> str:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    return proc_testing.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        env=env,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None]) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "step")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "master")
    return tmp_path


def test_a_key_moved_across_roles_in_git_is_clean(repo, capsys):
    base = _commit(
        repo,
        {
            K3S: "k3s_longhorn_version: v1.12.1\nk3s_metallb_version: v0.16.0\n",
            "ansible/roles/setup/k3s/tasks/longhorn.yml": "- name: apply\n",
        },
    )
    head = _commit(
        repo,
        {
            K3S: "k3s_metallb_version: v0.16.0\n",
            LONGHORN: 'k3s_longhorn_version: "v1.12.1"\n',
            "ansible/roles/setup/k3s/tasks/longhorn.yml": None,
        },
    )
    assert cluster_pins.check(base, head, repo) == cluster_pins.CLEAN
    assert "moves no pin the wrong way" in capsys.readouterr().out


def test_a_decrease_in_git_is_flagged(repo, capsys):
    base = _commit(repo, {K3S: "k3s_longhorn_version: v1.12.1\n"})
    head = _commit(repo, {K3S: "k3s_longhorn_version: v1.12.0\n"})
    assert cluster_pins.check(base, head, repo) == cluster_pins.REFUSED
    assert f"[FAIL] {K3S}: k3s_longhorn_version decreases" in capsys.readouterr().err


def test_a_key_set_in_two_role_files_is_unjudged(repo):
    base = _commit(repo, {K3S: "k3s_longhorn_version: v1.12.1\n"})
    head = _commit(repo, {LONGHORN: "k3s_longhorn_version: v1.12.1\n"})
    assert cluster_pins.check(base, head, repo) == cluster_pins.UNJUDGED


def test_a_pin_read_but_not_assigned_outside_its_role_file_is_clean(repo):
    """A Jinja read, a `==` comparison and a comment set nothing, so none is an override."""
    sha = _commit(
        repo,
        {
            K3S: "k3s_longhorn_version: v1.12.1\n",
            "ansible/roles/setup/k3s/tasks/longhorn.yml": (
                "- name: apply\n"
                "  url: https://x/{{ k3s_longhorn_version }}/longhorn.yaml\n"
                "  when: k3s_longhorn_version == 'v1.12.1'\n"
                "  # k3s_longhorn_version: bump one minor at a time\n"
                "  url: x  # k3s_longhorn_version: v1.11.0\n"
                '  url: "x"  # k3s_longhorn_version: v1.11.0\n'
                "  url: x's  # k3s_longhorn_version: v1.11.0\n"
            ),
        },
    )
    assert cluster_pins.overrides(sha, repo) == []


def test_the_real_tree_overrides_no_pin():
    assert cluster_pins.overrides("HEAD") == []


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (
            "ansible/k3s-bringup.yml",
            "- hosts: k3s\n  vars:\n    k3s_longhorn_version: v1.11.0\n",
        ),
        (
            "ansible/roles/setup/k3s/tasks/main.yml",
            "- include_role:\n    name: x\n  vars:\n    k3s_longhorn_version: v1.11.0\n",
        ),
        (
            "ansible/inventory/hosts.ini",
            "[k3s]\ndaniel-box k3s_longhorn_version=v1.11.0\n",
        ),
        ("ansible/inventory/hosts.ini", "[all:vars]\nk3s_longhorn_version = v1.11.0\n"),
        ("ansible/inventory/group_vars/all", "k3s_longhorn_version: v1.11.0\n"),
        (
            "ansible/roles/setup/k3s/tasks/x.yml",
            "- set_fact:\n    k3s_longhorn_version: v1.11.0\n",
        ),
        ("ansible/site.yml", "- hosts: all\n  vars: {k3s_longhorn_version: v1.11.0}\n"),
        (
            "ansible/site.yml",
            '- hosts: all\n  vars: {note: "#1", k3s_longhorn_version: v1.11.0}\n',
        ),
        (
            "ansible/site.yml",
            '- hosts: all\n  vars: {note: "see #1", k3s_longhorn_version: v1.11.0}\n',
        ),
        (
            "ansible/inventory/hosts.ini",
            '[k3s]\ndaniel-box note="a #b" k3s_longhorn_version=v1.11.0\n',
        ),
        (
            "ansible/site.yml",
            "- hosts: all\n  vars: {note: 'it''s #1', k3s_longhorn_version: v1.11.0}\n",
        ),
        (
            "ansible/inventory/hosts.ini",
            "[k3s]\ndaniel-box ansible_user=x;y k3s_longhorn_version=v1.11.0\n",
        ),
    ],
    ids=[
        "play-vars",
        "include-role-vars",
        "ini-host-var",
        "ini-all-vars",
        "extensionless",
        "set-fact",
        "flow",
        "hash-in-a-value",
        "spaced-hash-in-a-quoted-yaml-value",
        "spaced-hash-in-a-quoted-ini-value",
        "spaced-hash-after-a-yaml-quote-escape",
        "semicolon-in-a-value",
    ],
)
def test_an_override_the_column_zero_grep_missed_is_flagged(repo, capsys, path, text):
    base = _commit(repo, {K3S: "k3s_longhorn_version: v1.12.1\n"})
    head = _commit(repo, {path: text})
    assert cluster_pins.check(base, head, repo) == cluster_pins.REFUSED
    assert (
        f"[FAIL] {path}: k3s_longhorn_version is set outside" in capsys.readouterr().err
    )


def test_an_inventory_override_of_a_pin_is_flagged(repo, capsys):
    base = _commit(repo, {K3S: "k3s_longhorn_version: v1.12.1\n"})
    group_vars = "ansible/inventory/group_vars/all.yml"
    head = _commit(repo, {group_vars: "k3s_longhorn_version: v1.11.0\n"})
    assert cluster_pins.check(base, head, repo) == cluster_pins.REFUSED
    assert f"[FAIL] {group_vars}: k3s_longhorn_version is set outside" in (
        capsys.readouterr().err
    )


# ── k3s_version: a decrease needs the acknowledgement key (H6.3c) ───────────────────────────
OLD_K3S, LOWER_K3S = "v1.37.1+k3s1", "v1.37.0+k3s1"


def test_a_k3s_decrease_without_the_acknowledgement_is_flagged():
    flagged = cluster_pins.k3s_problems(OLD_K3S, LOWER_K3S, None)
    assert len(flagged) == 1
    assert "decreases from v1.37.1+k3s1 to v1.37.0+k3s1" in flagged[0]


def test_a_k3s_decrease_acknowledged_at_the_new_pin_is_clean():
    assert cluster_pins.k3s_problems(OLD_K3S, LOWER_K3S, LOWER_K3S) == []


def test_a_k3s_decrease_acknowledged_at_another_version_is_flagged():
    assert len(cluster_pins.k3s_problems(OLD_K3S, LOWER_K3S, "v1.36.0+k3s1")) == 1


def test_a_k3s_suffix_decrease_is_a_decrease():
    assert len(cluster_pins.k3s_problems("v1.37.1+k3s2", OLD_K3S, None)) == 1


def test_a_k3s_raise_with_the_key_absent_is_clean():
    assert cluster_pins.k3s_problems(OLD_K3S, "v1.37.2+k3s1", None) == []


def test_a_k3s_raise_that_leaves_the_key_set_is_flagged():
    flagged = cluster_pins.k3s_problems(OLD_K3S, "v1.37.2+k3s1", OLD_K3S)
    assert len(flagged) == 1
    assert "Remove the key" in flagged[0]


def test_an_unchanged_k3s_pin_is_clean_whatever_the_key():
    assert cluster_pins.k3s_problems(OLD_K3S, OLD_K3S, OLD_K3S) == []


def test_a_k3s_pin_that_is_not_a_version_is_unjudged():
    with pytest.raises(cluster_pins.Unjudged):
        cluster_pins.k3s_problems(OLD_K3S, "latest", None)


def test_the_real_tree_holds_a_k3s_pin_the_check_can_read():
    """The subject the check reads exists at HEAD, so a moved file cannot silently skip it."""
    pin, ack = cluster_pins.k3s_values_at("HEAD")
    assert cluster_pins.K3S_VERSION.fullmatch(pin or "")
    assert ack is None or ack == pin


def test_a_k3s_pin_present_at_the_base_and_gone_at_the_head_is_flagged():
    flagged = cluster_pins.k3s_problems(OLD_K3S, None, None)
    assert len(flagged) == 1
    assert "is gone at the head" in flagged[0]


def test_a_k3s_pin_absent_on_both_sides_is_clean():
    assert cluster_pins.k3s_problems(None, None, None) == []
