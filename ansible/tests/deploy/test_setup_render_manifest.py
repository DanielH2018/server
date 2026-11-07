"""The stale-render check only sees templates stamped into the render manifest.

setup-drift-check.sh's third arm compares each template's source checksum against the one
stamped at render time, so a `template:`-rendered artifact that is NOT stamped is invisible to it
— the same silent-omission shape the arm exists to close.

This file guards two coverage properties of that mechanism:

  * **Scope.** An entry carries a repo-relative path, so the manifest sees every role's
    artifacts, not only those under `roles/setup/k3s/templates`. Four other roles stamp their
    own.

  * **Granularity.** Stamps are per tag family and run last. One file rewritten wholesale by
    every partial run, or written BEFORE any `template:` task, would make `--tags
    disk-health` restamp all the k3s scripts while rendering one, so the ones it never touched
    read as fresh.

A shell template added to the k3s role without being stamped is a coverage regression nothing
else would report, so it fails here instead.
"""

import re

from _helpers import REPO
from _shell_render import rendered_shell_text

_REPO = REPO
_MANIFEST_DIR = "/var/lib/homelab/setup-render-manifest.d"
# Arms 2 and 3 live in a library setup-drift-check.sh.j2 sources.
# The guards below assert against the file that literally holds the loops — a guard pointed at a wrapper asserts nothing
# about the code that runs.
# _ARMS_LIB is plain .sh, no Jinja. _ARM_CONSUMERS are (plane, role, name) *.sh.j2 triples,
# read from the render below.
_ARMS_LIB = _REPO / "ansible/roles/setup/initial_setup/files/setup-drift-lib.sh"
_ARM_CONSUMERS = (("setup", "initial_setup", "setup-drift-check.sh.j2"),)

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


def test_other_setup_roles_stamp_their_own_artifacts():
    """Rendered artifacts outside the k3s role must be watched.

    Each owning role includes the shared stamp, and this fails if one stops.

    daniel-pi's heartbeat scripts are deliberately absent — homelab-daniel-pi ships them, that
    host runs no setup-drift-check, and stamping them would claim coverage it cannot provide.
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
    }
    for rel in sorted(expected):
        text = (_REPO / rel).read_text()
        assert "stamp_render.yml" in text and "stamp_render_name:" in text, (
            f"{rel} no longer stamps its rendered artifacts, so a stale render of them is "
            f"invisible to setup-drift-check — the H1 gap, re-opened."
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
    }
    for rel in sorted(expected):
        text = (_REPO / rel).read_text()
        assert "stamp_deployed.yml" in text and "stamp_deployed_name:" in text, (
            f"{rel} no longer declares the code it deploys, so a stale copy of it is invisible "
            f"to setup-drift-check's deployed-code arm (2026-08-24 review M-5)."
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
    hash('sha256')` hashes the file minus its final byte, while setup-drift-check.sh compares
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
    sample = _REPO / "ansible/roles/setup/fake_remux/templates/fake-remux-health.sh.j2"
    raw = sample.read_bytes()
    assert raw.endswith(b"\n"), (
        f"{sample} no longer ends in a newline; pick another sample"
    )
    whole = hashlib.sha256(raw).hexdigest()
    stripped = hashlib.sha256(raw[:-1]).hexdigest()
    assert whole != stripped, (
        "sha256 of a file and of the file minus its last byte must differ"
    )


_STAMP_SITES = (
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
