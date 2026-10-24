"""Reconcile's line arm, against stub `dpkg` and `dpkg-query` (H7.2d).

The clauses are H7's *Red proofs for the checks this slice adds*, line arm. The arm edits a real
file under the test root, so the assertions read the file. What a stub cannot show is dpkg
replacing a conffile; the two owning-package upgrade cases below stand for the arm's side of
that (an upgrade that keeps the local file, and one that restored the package's copy), and the
dpkg side belongs to the container job. Each red fixture is a copy of reconcile with one rule
removed (`_reconcile_arms.red`), and its scenario must fail against it.

Run: uv run pytest hosts/lib/tests/test_reconcile_lines_arm.py
"""

import pytest
from _reconcile_arms import base, red
from _reconcile_host import Node

ARM = "reconcile_lib/lines.py"
SYSCTL = "/etc/sysctl.conf"
SWAPPY = {
    "path": SYSCTL,
    "line": "vm.swappiness=10",
    "regexp": r"^\s*vm\.swappiness\s*=",
}
BLOCK = {
    "path": "/etc/zsh/zshenv",
    "block": "export HOMELAB=1\nexport PAGER=less",
    "marker": "# {mark} ANSIBLE MANAGED BLOCK",
}
WRAPPED = (
    "# BEGIN ANSIBLE MANAGED BLOCK\nexport HOMELAB=1\nexport PAGER=less\n"
    "# END ANSIBLE MANAGED BLOCK\n"
)


@pytest.fixture
def node(tmp_path) -> Node:
    return base(Node(tmp_path))


def declare(node: Node, *edits: dict) -> None:
    node.manifest["reconcilers"] = ["lines"]
    node.manifest["lines"] = {"edits": list(edits)}


def write(node: Node, path: str, text: str, mode: int | None = None) -> None:
    target = node.root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if mode is not None:
        target.chmod(mode)


def read(node: Node, path: str) -> str:
    return (node.root / path.lstrip("/")).read_text()


def own(node: Node, path: str, *, conffile: bool) -> None:
    node.state["owned"].append(path)
    node.state["conffiles"] = {"some-package": [path] if conffile else []}


# What is and is not run.


def test_a_host_declaring_no_line_runs_no_dpkg_command_at_all(node):
    result = node.run()

    assert result.returncode == 0, result.stderr
    assert node.calls("dpkg") == []
    assert "lines" not in node.applied()


# The line form.


def test_a_line_is_appended_to_an_unowned_file_and_a_second_run_changes_nothing(node):
    write(node, SYSCTL, "net.ipv4.ip_forward=1")
    declare(node, SWAPPY)

    first = node.run()
    second = node.run()

    assert first.returncode == 0 and second.returncode == 0, second.stderr
    assert read(node, SYSCTL) == "net.ipv4.ip_forward=1\nvm.swappiness=10\n"


def test_a_missing_file_is_created_with_the_declared_mode(node):
    write(node, "/etc/default/.keep", "")
    declare(node, {**SWAPPY, "path": "/etc/default/new.conf", "mode": "0640"})

    result = node.run()

    assert result.returncode == 0, result.stderr
    assert read(node, "/etc/default/new.conf") == "vm.swappiness=10\n"
    assert (node.root / "etc/default/new.conf").stat().st_mode & 0o777 == 0o640


def test_the_last_line_the_regexp_matches_is_replaced_in_place(node):
    write(node, SYSCTL, "a=1\nvm.swappiness = 60\nb=2\nvm.swappiness=30\nc=3\n")
    declare(node, SWAPPY)

    assert node.run().returncode == 0
    assert read(node, SYSCTL) == "a=1\nvm.swappiness = 60\nb=2\nvm.swappiness=10\nc=3\n"


def test_the_line_ansible_wrote_is_adopted_and_no_second_copy_is_added(node):
    write(node, SYSCTL, "a=1\nvm.swappiness=10\n")
    before = (node.root / SYSCTL.lstrip("/")).stat().st_mtime_ns
    declare(node, SWAPPY)

    assert node.run().returncode == 0
    assert read(node, SYSCTL).count("vm.swappiness") == 1
    assert (node.root / SYSCTL.lstrip("/")).stat().st_mtime_ns == before, "no rewrite"


# The block form.


def test_a_block_is_appended_with_its_markers_and_replaced_between_them(node):
    write(node, BLOCK["path"], "alias l=ls")
    declare(node, BLOCK)

    assert node.run().returncode == 0
    assert read(node, BLOCK["path"]) == "alias l=ls\n" + WRAPPED
    declare(node, {**BLOCK, "block": "export HOMELAB=2"})

    assert node.run().returncode == 0
    assert read(node, BLOCK["path"]) == (
        "alias l=ls\n# BEGIN ANSIBLE MANAGED BLOCK\nexport HOMELAB=2\n"
        "# END ANSIBLE MANAGED BLOCK\n"
    )


def test_the_block_ansible_wrote_is_adopted(node):
    write(node, BLOCK["path"], "alias l=ls\n" + WRAPPED + "tail\n")
    before = read(node, BLOCK["path"])
    declare(node, BLOCK)

    assert node.run().returncode == 0
    assert read(node, BLOCK["path"]) == before


# The conffile rule.


def test_a_line_in_a_conffile_is_kept_and_a_block_in_one_too(node):
    write(node, SYSCTL, "a=1\n")
    own(node, SYSCTL, conffile=True)
    declare(node, SWAPPY)

    assert node.run().returncode == 0
    assert "vm.swappiness=10" in read(node, SYSCTL)
    node.state["owned"].append(BLOCK["path"])
    node.state["conffiles"]["some-package"].append(BLOCK["path"])
    write(node, BLOCK["path"], "x\n")
    declare(node, SWAPPY, BLOCK)

    assert node.run().returncode == 0
    assert WRAPPED in read(node, BLOCK["path"])


def owned_non_conffile_is_refused(node: Node) -> None:
    write(node, "/etc/demo/app.conf", "keep=1\n")
    own(node, "/etc/demo/app.conf", conffile=False)
    declare(node, {**SWAPPY, "path": "/etc/demo/app.conf"})

    result = node.run()

    assert result.returncode == 1
    assert "does not list it as a conffile" in result.stderr
    assert read(node, "/etc/demo/app.conf") == "keep=1\n"
    assert node.applied()["result"]["ok"] is False


def test_an_owned_path_that_is_not_a_conffile_is_refused_and_fails_the_run(node):
    owned_non_conffile_is_refused(node)


def test_red_an_arm_that_edits_any_owned_file_would_fight_dpkg(tmp_path):
    red(
        tmp_path,
        owned_non_conffile_is_refused,
        ARM,
        "if path not in (row.split()[0] for row in listed.splitlines() if row.strip()):",
        "if False:",
    )


# The two owning-package upgrade cases (H7, *In-place edits*).


def test_an_upgrade_that_keeps_the_local_conffile_keeps_the_line_and_one_that_restored_the_packages_copy_gets_it_back(
    node,
):
    write(node, SYSCTL, "a=1\n")
    own(node, SYSCTL, conffile=True)
    declare(node, SWAPPY)
    assert node.run().returncode == 0

    assert node.run().returncode == 0, (
        "the local file was kept: the line is still there"
    )
    assert read(node, SYSCTL).count("vm.swappiness=10") == 1
    write(node, SYSCTL, "a=1\n")  # the upgrade restored the package's own copy

    assert node.run().returncode == 0
    assert read(node, SYSCTL) == "a=1\nvm.swappiness=10\n"


# Owner and mode survive a rewrite.


def rewrite_keeps_the_mode(node: Node) -> None:
    write(node, "/home/pi/authorized_keys", "ssh-ed25519 AAA old\n", mode=0o640)
    declare(
        node,
        {
            "path": "/home/pi/authorized_keys",
            "line": "ssh-ed25519 AAA new pi-peer-backup-cronjob",
            "regexp": "pi-peer-backup-cronjob",
        },
    )

    assert node.run().returncode == 0
    target = node.root / "home/pi/authorized_keys"
    assert target.stat().st_mode & 0o7777 == 0o640
    assert "new pi-peer-backup-cronjob" in target.read_text()


def test_rewriting_a_file_keeps_its_mode(node):
    rewrite_keeps_the_mode(node)


def test_red_an_arm_that_rewrites_with_a_default_mode_would_break_sshd_strictmodes(
    tmp_path,
):
    red(
        tmp_path,
        rewrite_keeps_the_mode,
        ARM,
        "os.chmod(tmp, existing.st_mode & 0o7777 if existing else int(mode, 8))",
        "os.chmod(tmp, int(mode, 8))",
    )


# Order: the edit comes before the activation that reads it, and before ufw.


def test_the_activation_reads_the_file_the_arm_just_edited(node):
    # `vm.swappiness 10` is not a sysctl line, so the activation's parse refuses it. It sees the
    # edit only if the arm ran first; the unedited file parses.
    write(node, SYSCTL, "a=1\n")
    node.manifest["activations"] = {SYSCTL: "sysctl"}
    declare(node, {**SWAPPY, "line": "vm.swappiness 10", "regexp": "vm.swappiness"})

    result = node.run()

    assert result.returncode == 1
    assert "unparsable line" in result.stderr and "vm.swappiness 10" in result.stderr
    assert node.calls("sysctl") == []


def test_a_changed_file_runs_its_sysctl_activation_once_and_an_unchanged_one_does_not(
    node,
):
    write(node, SYSCTL, "a=1\n")
    node.manifest["activations"] = {SYSCTL: "sysctl"}
    declare(node, SWAPPY)

    assert node.run().returncode == 0
    assert [c["argv"] for c in node.calls("sysctl")] == [["sysctl", "--system"]]
    assert node.run().returncode == 0
    assert node.calls("sysctl") == []


def test_the_line_arm_runs_before_the_ufw_arm(node):
    write(node, SYSCTL, "a=1\n")
    declare(node, SWAPPY)
    node.manifest["reconcilers"] = ["lines", "ufw"]
    node.manifest["ufw"] = {
        "rules": [
            {
                "id": "ssh-limit",
                "spec": "limit 22/tcp",
                "keep": True,
                "position": "last",
                "before": None,
            }
        ],
        "legacy": [],
    }

    assert node.run().returncode == 0
    order = [
        call["argv"][0] for call in node.calls() if call["argv"][0] in ("dpkg", "ufw")
    ]
    assert order.index("dpkg") < order.index("ufw")


# A declaration the arm could never keep is refused with the manifest.


def test_a_line_that_misses_its_own_regexp_fails_the_run_before_any_edit(node):
    write(node, SYSCTL, "a=1\n")
    declare(node, {**SWAPPY, "regexp": "nothing-matches-this"})

    result = node.run()

    assert result.returncode == 1
    assert read(node, SYSCTL) == "a=1\n"
    assert "lines section is malformed" in result.stderr
