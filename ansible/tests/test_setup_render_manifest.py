"""The stale-render check only sees templates stamped into the render manifest.

manifest-prune-check.sh's third arm compares each template's source checksum against the one
stamped at render time, so a `template:`-rendered artifact that is NOT stamped is invisible to it
— the same silent-omission shape the arm exists to close (found 2026-08-23, when
/usr/local/bin/longhorn-backup-health.sh had been two commits stale for two days behind a
"deployed scripts match the repo" heartbeat).

Two coverage bugs in that mechanism were closed on 2026-08-23b and this file guards both:

  * **Scope** (review H1). The manifest was one file of bare filenames joined onto a hardcoded
    `roles/setup/k3s/templates`, so it could not see another role's artifacts at all. Nine on
    daniel-box were watched by nothing, including secret-rotate.sh — a weekly state-changing cron
    that commits and pushes. Entries now carry a repo-relative path, and four other roles stamp
    their own.

  * **Granularity** (review M6). That one file was rewritten wholesale by every partial run, and
    it was written BEFORE any `template:` task. So `--tags manifest-prune` — the natural command
    for redeploying the drift check itself — restamped all eight k3s scripts while rendering one,
    and the seven it never touched read as fresh. Stamps are now per tag family and run last.

A shell template added to the k3s role without being stamped is a coverage regression nothing
else would report, so it fails here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_ROLE = _REPO / "ansible/roles/setup/k3s"
_TEMPLATES = _ROLE / "templates"
_HEALTH_CRONS = _ROLE / "tasks/health-crons.yml"
_MANIFEST_DIR = "/var/lib/homelab/setup-render-manifest.d"

# Shell templates that are NOT rendered onto this host as standalone scripts, each with the
# reason. Anything here is exempt from the manifest; everything else must be in it.
#
# Empty, and that is the correct state. It held "kuma-push-lib.sh.j2" until 2026-08-23b review
# L8, which is not in this directory and never was — the shared push library lives in
# roles/setup/initial_setup/files/ as a plain .sh, so it is `copy:`-deployed and outside this
# glob entirely. A dead exemption is worse than none: it reads as precedent for exempting the
# next thing, and it silently widens what the check tolerates. test_every_exemption_is_real
# below is what keeps that from recurring.
_NOT_STANDALONE_SCRIPTS: set[str] = set()

# Group name -> the tag family whose `template:` tasks render its members. The stamp task for a
# group must carry exactly this tag, or the group is restamped by runs that did not render it
# (too broad) or never restamped at all (too narrow).
_GROUP_TAGS = {
    "k3s-backup-health": "backup-health",
    "k3s-disk-health": "disk-health",
    "k3s-manifest-prune": "manifest-prune",
    "k3s-etcd-snapshot": "etcd-snapshot",
}


def _groups() -> list[dict]:
    defaults = yaml.safe_load((_ROLE / "defaults/main.yml").read_text())
    return defaults["k3s_render_stamp_groups"]


def _declared_paths() -> set[str]:
    return {tpl for group in _groups() for tpl in group["templates"]}


def _declared_names() -> set[str]:
    return {Path(tpl).name for tpl in _declared_paths()}


def test_every_rendered_shell_template_is_in_the_manifest():
    on_disk = {p.name for p in _TEMPLATES.glob("*.sh.j2")} - _NOT_STANDALONE_SCRIPTS
    missing = on_disk - _declared_names()
    assert not missing, (
        "these shell templates are rendered onto the host but are not checksummed into "
        f"{_MANIFEST_DIR}, so a stale render of them is undetectable: "
        + ", ".join(sorted(missing))
    )


def test_the_manifest_names_no_template_that_stopped_existing():
    stale = {tpl for tpl in _declared_paths() if not (_REPO / tpl).is_file()}
    assert not stale, (
        "k3s_render_stamp_groups names templates that no longer exist, which would make the "
        "check page forever: " + ", ".join(sorted(stale))
    )


def test_declared_paths_are_repo_relative_and_resolve():
    """The whole point of the H1 fix: an entry carries its own path so the check joins nothing
    onto a hardcoded role directory. A bare filename here would silently stop resolving."""
    for tpl in sorted(_declared_paths()):
        assert tpl.startswith("ansible/roles/"), (
            f"{tpl!r} is not a repo-relative path. The check resolves entries against the repo "
            f"root, so a bare filename resolves to nothing and reads as 'template gone'."
        )


def test_no_template_is_stamped_by_two_groups():
    """A template in two groups is restamped by either family's run, which re-opens M6 for it."""
    seen: dict[str, str] = {}
    for group in _groups():
        for tpl in group["templates"]:
            assert tpl not in seen, (
                f"{tpl} is stamped by both {seen[tpl]} and {group['name']}; either run would "
                f"declare it current, which is the partial-run false green M6 closed."
            )
            seen[tpl] = group["name"]


def test_each_stamp_task_carries_exactly_its_groups_tag_family():
    """The M6 guard proper. The old single stamp carried [backup-health, disk-health,
    manifest-prune] and omitted etcd-snapshot, while checksumming all eight scripts — so three
    families restamped the etcd script they never rendered, and `--tags etcd-snapshot` skipped
    the stamp entirely. Membership and tags have to agree, and only a test can hold them there.
    """
    # Parsed as YAML, not matched with a regex: the tasks ARE structured data, and a pattern
    # spanning task boundaries is both fragile and — as written the first time — quadratic.
    tasks = yaml.safe_load(_HEALTH_CRONS.read_text())
    by_group = {
        task["vars"]["stamp_render_name"]: task
        for task in tasks
        if isinstance(task, dict) and "stamp_render_name" in (task.get("vars") or {})
    }
    for name, expected_tag in _GROUP_TAGS.items():
        assert name in by_group, (
            f"no stamp task in health-crons.yml sets stamp_render_name: {name}"
        )
        actual = set(by_group[name].get("tags") or [])
        assert actual == {expected_tag}, (
            f"the stamp task for {name} carries {sorted(actual)}, expected [{expected_tag}]. "
            f"Broader tags restamp templates this run did not render; narrower tags leave the "
            f"group unstamped by the run that did."
        )


def test_every_group_has_a_declared_tag_family():
    """Guards the mapping above against a new group being added with no tag expectation, which
    would let it into the defaults without any of the checks in this file applying to it."""
    declared = {group["name"] for group in _groups()}
    unmapped = declared - set(_GROUP_TAGS)
    assert not unmapped, (
        f"k3s_render_stamp_groups has {sorted(unmapped)} with no entry in _GROUP_TAGS. Add the "
        f"tag family it is rendered under, so the stamp/tag agreement is actually asserted."
    )


def test_the_check_reads_the_manifest_it_is_given():
    """Pins the writer and the reader to the same path — they are written in different files (a
    task and a shell template), which is exactly how a rename silently disarms the arm."""
    stamp_task = (
        _REPO / "ansible/roles/setup/common/tasks/stamp_render.yml"
    ).read_text()
    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    assert _MANIFEST_DIR in stamp_task, (
        "stamp_render.yml no longer writes the render manifest"
    )
    assert _MANIFEST_DIR in script, (
        "manifest-prune-check.sh.j2 no longer reads the render manifest"
    )


def test_other_setup_roles_stamp_their_own_artifacts():
    """H1's actual finding: nine rendered artifacts outside the k3s role were watched by nothing.
    Each owning role now includes the shared stamp, and this fails if one stops.

    daniel-pi's optimize_pi scripts are deliberately absent — that host runs no
    manifest-prune-check, and stamping them here would claim coverage this host cannot provide.
    """
    expected = {
        "ansible/roles/setup/gitops_deploy/tasks/main.yml",
        "ansible/roles/setup/renovate_notify/tasks/main.yml",
        "ansible/roles/setup/fake_remux/tasks/main.yml",
        "ansible/roles/setup/initial_setup/tasks/crons.yml",
    }
    for rel in sorted(expected):
        text = (_REPO / rel).read_text()
        assert "stamp_render.yml" in text and "stamp_render_name:" in text, (
            f"{rel} no longer stamps its rendered artifacts, so a stale render of them is "
            f"invisible to manifest-prune-check — the H1 gap, re-opened."
        )


def test_an_absent_manifest_is_not_reported_as_drift():
    """A host that predates the mechanism has no manifest; that is unproven coverage, not a
    failure, and paging for it would train the operator to ignore this monitor."""
    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    assert "render manifest absent" in script
    assert "MANIFEST_ENTRIES" in script, (
        "the arm no longer counts stamped entries, so a directory of empty fragments reads as "
        "'everything matches' instead of 'nothing is armed' (2026-08-23b review L2)."
    )


def test_an_empty_fragment_cannot_disarm_the_arm():
    """L2. The guard was `[[ -r … ]]`: a zero-byte manifest is readable, so it took the present
    branch, contributed no comparisons, and the check reported a confident green while watching
    nothing at all."""
    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    assert re.search(r'\[\[\s+-s\s+"\$FRAGMENT"', script), (
        "the fragment loop must test -s, not -r: an empty file is readable and silently "
        "contributes nothing, which disarms this arm behind a green heartbeat."
    )


def test_the_two_halves_hash_the_same_bytes():
    """The stamp and the check must agree on what "the file" is, byte for byte.

    Ansible's file lookup strips a trailing newline by default, so `lookup('file', x) |
    hash('sha256')` hashes the file minus its final byte, while manifest-prune-check.sh compares
    against `sha256sum x`, which does not. Every stamped entry would have read stale from the
    first armed run — eight simultaneous alerts on a perfectly healthy host.

    Nothing caught it in review because the arm has never executed: it shipped 2026-08-23, the
    host has not run initial_setup.yml since, and with no manifest present the check takes its
    absent branch. This asserts the two spellings that make the digests agree, and the assertion
    below proves the arithmetic rather than trusting the flag's name.
    """
    stamp_task = (
        _REPO / "ansible/roles/setup/common/tasks/stamp_render.yml"
    ).read_text()
    assert "rstrip=False" in stamp_task, (
        "the render stamp must pass rstrip=False to lookup('file', ...). Without it Ansible "
        "drops the trailing newline before hashing and no entry can ever match sha256sum."
    )

    script = (_TEMPLATES / "manifest-prune-check.sh.j2").read_text()
    assert "sha256sum" in script, "the check no longer recomputes with sha256sum"

    # Prove the two digests actually differ on a real template, so this test fails loudly if a
    # future Ansible makes rstrip=False the default and someone drops the flag as redundant.
    import hashlib

    sample = _TEMPLATES / "manifest-prune-check.sh.j2"
    raw = sample.read_bytes()
    assert raw.endswith(b"\n"), (
        f"{sample} no longer ends in a newline; pick another sample"
    )
    whole = hashlib.sha256(raw).hexdigest()
    stripped = hashlib.sha256(raw[:-1]).hexdigest()
    assert whole != stripped, (
        "sha256 of a file and of the file minus its last byte must differ"
    )


def test_every_exemption_is_real():
    """An exemption naming a template that does not exist excuses nothing and misleads everyone.

    `kuma-push-lib.sh.j2` sat in _NOT_STANDALONE_SCRIPTS while living in another role's files
    directory as a plain .sh — so it was never in this glob, the exemption never fired, and it
    read as precedent for exempting the next script that came along (2026-08-23b review L8).
    Same shape as the stale-exemption guard in the k3s join-port symmetry test.
    """
    on_disk = {p.name for p in _TEMPLATES.glob("*.sh.j2")}
    stale = _NOT_STANDALONE_SCRIPTS - on_disk
    assert not stale, (
        f"_NOT_STANDALONE_SCRIPTS exempts {sorted(stale)}, which is not in {_TEMPLATES}. Remove "
        f"the entry — an exemption for a file that does not exist quietly widens what this "
        f"check tolerates and reads as precedent for the next one."
    )
