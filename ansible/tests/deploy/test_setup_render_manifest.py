"""The stale-render check only sees templates stamped into the render manifest.

manifest-prune-check.sh's third arm compares each template's source checksum against the one
stamped at render time, so a `template:`-rendered artifact that is NOT stamped is invisible to it
— the same silent-omission shape the arm exists to close.

This file guards two coverage properties of that mechanism:

  * **Scope.** An entry carries a repo-relative path, so the manifest sees every role's
    artifacts, not only those under `roles/setup/k3s/templates`. Four other roles stamp their
    own.

  * **Granularity.** Stamps are per tag family and run last. One file rewritten wholesale by
    every partial run, or written BEFORE any `template:` task, would make `--tags
    manifest-prune` — the natural command for redeploying the drift check itself — restamp all
    the k3s scripts while rendering one, so the ones it never touched read as fresh.

A shell template added to the k3s role without being stamped is a coverage regression nothing
else would report, so it fails here instead.
"""

import re
from pathlib import Path

from lib import yaml_fast
from _ansible_collections import INSTALL_HINT, collection_resolves
from _helpers import REPO
from _shell_render import rendered_names_for, rendered_shell_text
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE

_REPO = REPO
_TEMPLATES = K3S_ROLE / "templates"
_HEALTH_CRONS = K3S_ROLE / "tasks/health-crons.yml"
_MANIFEST_DIR = "/var/lib/homelab/setup-render-manifest.d"
# Arms 2 and 3 live in a library that both consumers source: manifest-prune-check.sh.j2 is
# installed only on k3s server hosts, and daniel-server renders the whole UPS shutdown chain.
# The guards below assert against the file that literally holds the loops — a guard pointed at a wrapper asserts nothing
# about the code that runs.
# _ARMS_LIB is plain .sh, no Jinja. _ARM_CONSUMERS are (plane, role, name) *.sh.j2 triples,
# read from the render below.
_ARMS_LIB = _REPO / "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
_ARM_CONSUMERS = (
    ("setup", "k3s", "manifest-prune-check.sh.j2"),
    ("setup", "initial_setup", "setup-drift-check.sh.j2"),
)

# Shell templates that are NOT rendered onto this host as standalone scripts, each with the
# reason. Anything here is exempt from the manifest; everything else must be in it.
#
# Empty, and that is the correct state. A dead exemption is worse than none: it reads as precedent for exempting the
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
    "k3s-release-staleness": "release-staleness",
    "k3s-etcd-snapshot": "etcd-snapshot",
}


def _groups() -> list[dict]:
    defaults = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    return defaults["k3s_render_stamp_groups"]


def _declared_paths() -> set[str]:
    return {tpl for group in _groups() for tpl in group["templates"]}


def _declared_names() -> set[str]:
    return {Path(tpl).name for tpl in _declared_paths()}


def test_every_rendered_shell_template_is_in_the_manifest():
    on_disk = rendered_names_for("setup", "k3s") - _NOT_STANDALONE_SCRIPTS
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
    """An entry carries its own path.

    The check joins nothing onto a hardcoded role directory. A bare filename here would silently
    stop resolving.
    """
    for tpl in sorted(_declared_paths()):
        assert tpl.startswith("ansible/roles/"), (
            f"{tpl!r} is not a repo-relative path. The check resolves entries against the repo "
            f"root, so a bare filename resolves to nothing and reads as 'template gone'."
        )


def test_no_template_is_stamped_by_two_groups():
    """A template in two groups is restamped by either family's run, which restamps its members from a run that did not render them."""
    seen: dict[str, str] = {}
    for group in _groups():
        for tpl in group["templates"]:
            assert tpl not in seen, (
                f"{tpl} is stamped by both {seen[tpl]} and {group['name']}; either run would "
                f"declare it current, which is the partial-run false green M6 closed."
            )
            seen[tpl] = group["name"]


def test_each_stamp_task_carries_exactly_its_groups_tag_family():
    """The per-family stamp guard.

    A single stamp carrying several families' tags while checksumming every script would let
    each family restamp scripts it never rendered, and `--tags etcd-snapshot` skip the stamp
    entirely. Membership and tags have to agree, and only a test can hold them there.
    """
    # Parsed as YAML, not matched with a regex: the tasks ARE structured data, and a pattern
    # spanning task boundaries is both fragile and quadratic.
    tasks = yaml_fast.safe_load(_HEALTH_CRONS.read_text())
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
    assert _MANIFEST_DIR in stamp_task, (
        "stamp_render.yml no longer writes the render manifest"
    )
    assert _MANIFEST_DIR in _ARMS_LIB.read_text(), (
        "setup-drift-lib.sh no longer reads the render manifest"
    )


def test_both_readers_source_the_shared_arms():
    """The indirection guard.

    Arms 2 and 3 live in one library so daniel-box's reader and daniel-server's cannot diverge — but
    every assertion in this file now points at that library, so a consumer that quietly re-inlined
    the loops would satisfy none of them and break nothing. This is what fails in that case.
    """
    for plane, role, name in _ARM_CONSUMERS:
        text = rendered_shell_text(plane, role, name)
        assert "source /usr/local/lib/setup-drift-lib.sh" in text, (
            f"{name} no longer sources the shared drift arms, so the guards in this file "
            f"assert nothing about the code it runs (2026-08-29 review M-10)."
        )
        assert "setup_drift_scan" in text, (
            f"{name} sources the library but never calls setup_drift_scan, so both arms "
            f"are absent behind a script that still pushes a verdict."
        )


def test_the_k3s_readers_note_rewording_still_matches_the_library():
    """A bash `${var/from/to}` that matches nothing is a silent no-op.

    manifest-prune-check.sh re-words the library's two unarmed notes to name the playbooks a k3s
    server actually runs. If the library's phrasing changes, the substitution stops matching and
    daniel-box's message reverts to the generic wording with nothing failing — the textual
    coupling this repo escalates to a check rather than leaving as a comment.
    """
    lib = _ARMS_LIB.read_text()  # plain .sh, no Jinja (see the comment at its import)
    reader = rendered_shell_text("setup", "k3s", "manifest-prune-check.sh.j2")
    patterns = re.findall(r"\$\{(?:DEPLOYED|MANIFEST)_NOTE/([^/]+)/", reader)
    assert patterns, (
        "manifest-prune-check.sh.j2 no longer re-words the library's notes. If that was "
        "deliberate, delete this test; if the substitution was renamed, update the regex."
    )
    for needle in patterns:
        assert needle in lib, (
            f"manifest-prune-check.sh.j2 substitutes {needle!r} out of the library's unarmed "
            f"note, but setup-drift-lib.sh no longer contains that phrasing — the substitution "
            f"is a no-op and daniel-box silently shows the generic wording."
        )


def test_other_setup_roles_stamp_their_own_artifacts():
    """Rendered artifacts outside the k3s role must be watched.

    Each owning role includes the shared stamp, and this fails if one stops.

    daniel-pi's optimize_pi scripts are deliberately absent — that host runs no
    manifest-prune-check, and stamping them here would claim coverage this host cannot provide.
    """
    expected = {
        "ansible/roles/setup/gitops_deploy/tasks/service.yml",
        "ansible/roles/setup/renovate_notify/tasks/service.yml",
        "ansible/roles/setup/fake_remux/tasks/main.yml",
        "ansible/roles/setup/initial_setup/tasks/crons.yml",
        # THIS LIST IS AN ENUMERATION: it cannot see the next new role. Deriving it means
        # asserting that every `src:`-referenced .j2 under roles/setup/ is stamped, which would
        # demand many new stamp entries across roles whose artifacts nothing has decided to
        # watch (staged k8s manifests, netplan, fail2ban, the two daniel-pi scripts that are
        # deliberately exempt). That is a bigger change than this test, so it is a known limit,
        # not a silent omission.
        "ansible/roles/setup/claude_code/tasks/main.yml",
    }
    for rel in sorted(expected):
        text = (_REPO / rel).read_text()
        assert "stamp_render.yml" in text and "stamp_render_name:" in text, (
            f"{rel} no longer stamps its rendered artifacts, so a stale render of them is "
            f"invisible to manifest-prune-check — the H1 gap, re-opened."
        )


_DEPLOYED_DIR = "/var/lib/homelab/setup-deployed-manifest.d"


def test_the_deployed_code_arm_derives_its_pairs_from_fragments():
    """A hardcoded path list could only prove the code its author had in mind.

    Other `copy:`-deployed files would go unwatched while the same script reports "deployed
    code matches the repo".

    A fragment directory makes it per-host by construction, the same shape the stale-script arm
    already uses: a pair exists only where the role that deploys it ran.
    """
    script = _ARMS_LIB.read_text()
    assert _DEPLOYED_DIR in script, (
        "setup-drift-lib.sh no longer reads the deployed manifest directory."
    )
    assert "deployed_entries" in script, (
        "the arm no longer counts declared pairs, so an empty or absent fragment directory "
        "reads as 'everything matches' instead of 'nothing is armed' — the L2 shape, one arm "
        "over."
    )
    assert not re.search(r"^\s*_?check_deployed /", script, re.MULTILINE), (
        "a hardcoded check_deployed pair is back. Declare it in the owning role via "
        "stamp_deployed.yml instead, or this arm narrows to whatever the next author "
        "remembered (2026-08-24 review M-5)."
    )


def test_a_deleted_source_is_drift_not_an_exemption():
    """The `[[ -r "$src" ]] || return 0` guard changes meaning with the fragment form.

    Hardcoded, it was merely misleading: the repo source exists on every host, so it never
    gated anything. Fragment-derived, a pair exists ONLY because a role deployed it here — so
    an unreadable source means the file was deleted from the repo while the artifact is still
    live. That is drift, and arm 3 reports the same case as "template gone from the repo".
    Returning 0 swallows it, and the entry still counts toward DEPLOYED_ENTRIES: armed, and
    checking nothing.
    """
    script = _ARMS_LIB.read_text()
    fn = re.search(r"^_check_deployed\(\) \{.*?^\}", script, re.MULTILINE | re.DOTALL)
    assert fn, "_check_deployed() is gone or no longer a plain function."
    body = fn.group(0)
    assert "return 0" not in body.split('if [[ ! -r "$src" ]]')[0], (
        "check_deployed still short-circuits on an unreadable source. A source missing from "
        "the repo is drift under the fragment form, not 'a file this repo does not ship here'."
    )
    assert "source gone from the repo" in body, (
        "check_deployed no longer distinguishes a deleted source from a byte mismatch, so the "
        "operator cannot tell 'redeploy this' from 'this template was retired'."
    )


def test_every_role_that_deploys_code_declares_its_pairs():
    """The companion enumeration: each role that `copy:`-deploys executable code records it.

    Kept as a list rather than derived because `copy:` is also how this repo writes small config
    files (/etc/issue, journald drop-ins, apt conf) — those are not code that can run stale, and
    a derivation would demand pairs for all of them.
    """
    expected = {
        "ansible/roles/setup/gitops_deploy/tasks/code.yml",
        "ansible/roles/setup/renovate_notify/tasks/code.yml",
        "ansible/roles/setup/fake_remux/tasks/main.yml",
        "ansible/roles/setup/initial_setup/tasks/crons.yml",
        "ansible/roles/setup/k3s/tasks/health-crons.yml",
    }
    for rel in sorted(expected):
        text = (_REPO / rel).read_text()
        assert "stamp_deployed.yml" in text and "stamp_deployed_name:" in text, (
            f"{rel} no longer declares the code it deploys, so a stale copy of it is invisible "
            f"to manifest-prune-check's deployed-code arm (2026-08-24 review M-5)."
        )


def test_an_absent_manifest_is_not_reported_as_drift():
    """A host that predates the mechanism has no manifest; that is unproven coverage, not a
    failure, and paging for it would train the operator to ignore this monitor."""
    script = _ARMS_LIB.read_text()
    assert "render manifest absent" in script
    assert "manifest_entries" in script, (
        "the arm no longer counts stamped entries, so a directory of empty fragments reads as "
        "'everything matches' instead of 'nothing is armed' (2026-08-23b review L2)."
    )


def test_an_empty_fragment_cannot_disarm_the_arm():
    """A `[[ -r … ]]` guard lets a zero-byte manifest, which is readable, take the present branch,
    contribute no comparisons, and report a confident green while watching nothing at all.
    """
    script = _ARMS_LIB.read_text()
    assert re.search(r'\[\[\s+-s\s+"\$fragment"', script), (
        "the fragment loop must test -s, not -r: an empty file is readable and silently "
        "contributes nothing, which disarms this arm behind a green heartbeat."
    )


def test_the_two_halves_hash_the_same_bytes():
    """The stamp and the check must agree on what "the file" is, byte for byte.

    Ansible's file lookup strips a trailing newline by default, so `lookup('file', x) |
    hash('sha256')` hashes the file minus its final byte, while manifest-prune-check.sh compares
    against `sha256sum x`, which does not. Every stamped entry would read stale from the
    first armed run — simultaneous alerts on a perfectly healthy host.

    This asserts the two spellings that make the digests agree, and the assertion
    below proves the arithmetic rather than trusting the flag's name.
    """
    stamp_task = (
        _REPO / "ansible/roles/setup/common/tasks/stamp_render.yml"
    ).read_text()
    assert "rstrip=False" in stamp_task, (
        "the render stamp must pass rstrip=False to lookup('file', ...). Without it Ansible "
        "drops the trailing newline before hashing and no entry can ever match sha256sum."
    )

    script = _ARMS_LIB.read_text()
    assert "sha256sum" in script, "the check no longer recomputes with sha256sum"

    # Prove the two digests actually differ on a real template, so this test fails loudly if a
    # future Ansible makes rstrip=False the default and someone drops the flag as redundant.
    import hashlib

    # A genuine byte-level subject, not a render stand-in -- #3178 doesn't apply here.
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

    An exemption for `kuma-push-lib.sh.j2` would never fire: that library lives in another
    role's files directory as a plain .sh, outside this glob, and the exemption would read as
    precedent for exempting the next script that came along. Same shape as the stale-exemption guard in the k3s join-port symmetry test.
    """
    on_disk = rendered_names_for("setup", "k3s")
    stale = _NOT_STANDALONE_SCRIPTS - on_disk
    assert not stale, (
        f"_NOT_STANDALONE_SCRIPTS exempts {sorted(stale)}, which is not in {_TEMPLATES}. Remove "
        f"the entry — an exemption for a file that does not exist quietly widens what this "
        f"check tolerates and reads as precedent for the next one."
    )


def test_the_tag_scoping_actually_selects_one_stamp_per_family():
    """The behavioural half of the per-family stamp guard, and the reason it is here rather than
    left to the YAML check above: reading the tag off the task proves the tag is written, not
    that Ansible selects on it.

    `--tags <family>` must reach exactly that family's stamp task and no other; a stamp
    restamped by a run that did not render its templates is the defect.

    `--list-tasks` executes nothing and needs no cluster, secrets or become password. The k3s role
    is reached from k3s-bringup.yml, NOT initial_setup.yml — the four other roles' stamps live in
    the latter and are covered by test_other_setup_roles_stamp_their_own_artifacts.
    """
    import os
    import shutil
    import subprocess
    import sys

    import pytest

    playbook_bin = shutil.which("ansible-playbook")
    if playbook_bin is None:
        pytest.skip("ansible-playbook not on PATH")

    # --list-tasks discovers no interpreter, so the pin only keeps this spawn in step with the
    # longhorn tests' real plays.
    env = dict(os.environ)
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    # k3s-bringup.yml names community.general.ufw, so without it every tag fails to parse (#4226).
    if not collection_resolves("community.general", playbook_bin, _REPO, env):
        pytest.skip(f"community.general does not resolve here: {INSTALL_HINT}")
    playbook = _REPO / "ansible/k3s-bringup.yml"
    for _name, tag in sorted(_GROUP_TAGS.items()):
        result = subprocess.run(
            ["ansible-playbook", str(playbook), "--tags", tag, "--list-tasks"],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, (
            f"--list-tasks failed for --tags {tag}: {result.stderr[-800:]}"
        )
        selected = [
            line.split(":", 1)[1].split("\t")[0].strip()
            for line in result.stdout.splitlines()
            if "Stamp the" in line
        ]
        assert len(selected) == 1, (
            f"--tags {tag} selects {len(selected)} stamp tasks ({selected}); exactly one group "
            f"is rendered by that family, so exactly one may be restamped by it."
        )
        # It must be the task that WRITES the fragment, not a wrapper that pulls it in. That
        # distinction is the whole test: with `include_tasks` the wrapper is selected and its
        # children are not, so `--tags disk-health` prints `included: .../stamp_render.yml`,
        # reports changed=0, and writes no fragment. A test that matches the wrapper's own name
        # passes throughout.
        #
        # `import_tasks` is static, so --list-tasks prints the imported task's name before
        # `stamp_render_name` is resolved. Every group therefore shows the same literal, and the
        # group it belongs to is established by the count plus _GROUP_TAGS above, not by parsing
        # a name out of this line.
        wanted = "Stamp the rendered sources for {{ stamp_render_name }}"
        assert selected[0] == wanted, (
            f"--tags {tag} selects {selected[0]!r}, expected the copy: task inside "
            f"stamp_render.yml ({wanted!r}). A wrapper name here means the stamp went back to "
            f"include_tasks, and a tag-scoped run writes no fragment at all."
        )


_STAMP_SITES = (
    "ansible/roles/setup/k3s/tasks/health-crons.yml",
    "ansible/roles/setup/gitops_deploy/tasks/service.yml",
    "ansible/roles/setup/renovate_notify/tasks/service.yml",
    "ansible/roles/setup/fake_remux/tasks/main.yml",
    "ansible/roles/setup/initial_setup/tasks/crons.yml",
)
_STAMP_REF = '"{{ role_path }}/../common/tasks/stamp_render.yml"'


def test_every_stamp_site_imports_rather_than_includes():
    """`include_tasks` silently disarms every tag-scoped stamp.

    A DYNAMIC include's children inherit the parent's tags, but `--tags` selection still filters
    those children on tags they do not carry. So under `--tags disk-health` the wrapper is
    selected, Ansible prints `included: .../stamp_render.yml`, the `copy:` inside it is
    filtered out, and the run reports changed=0 with no fragment on disk. `import_tasks` is
    static — the tags are attached to the imported tasks at parse time, so they are selected too.

    The four other roles do not show it. Their stamps sit under a ROLE-level tag
    (`gitops_deploy`, `renovate_notify`, `fake_remux`), which does reach a dynamic include's
    children.
    """
    for rel in _STAMP_SITES:
        text = (_REPO / rel).read_text()
        assert f"include_tasks: {_STAMP_REF}" not in text, (
            f"{rel} pulls in stamp_render.yml with include_tasks. Use import_tasks: a dynamic "
            f"include's children are filtered out by --tags, so no fragment is ever written."
        )
        assert f"import_tasks: {_STAMP_REF}" in text, (
            f"{rel} no longer imports the shared render stamp."
        )
