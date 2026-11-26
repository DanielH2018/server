"""Guards on how the weekly AIDE check judges a change to the user and group databases.

/etc/passwd, /etc/group, /etc/shadow and /etc/gshadow are in PERSIST and no package owns them, so
every service user a host package adds used to page. The check keeps root-only copies of the four
files from the run the baseline advanced to. A change is explained only when every line that
differs from that copy is a declared account's, as the host package's users arm declares it in
/usr/lib/homelab/manifest.json. The case table below is the rule; each row is one report entry
and the files as they stand after it.

Run: uv run pytest hosts/tests/host_checks/test_aide_check_accounts.py
"""

import pytest
from _aide_fake_host import (
    ACCOUNT_FILES,
    HOST_MANIFEST,
    RECONCILE_RECORD,
)
from _aide_check_run import (
    CHANGED_CONTENT,
    CHANGED_MODE,
    CLEAN_REPORT,
    _report,
    _run_check,
)

PASSWD, GROUP, SHADOW, GSHADOW = ACCOUNT_FILES
# The users arm's declaration in the fake host's manifest: claude exists, svc does not yet.
SVC_PASSWD = "svc:x:906:906:Svc:/var/lib/svc:/usr/sbin/nologin\n"
SVC_GROUP = "svc:x:906:\n"
SVC_SHADOW = "svc:!*:20400::::::\n"
SVC_GSHADOW = "svc:!::\n"
# oldsvc left the users list, so the arm expired it and its record in applied.json names it.
OLDSVC_SHADOW = "oldsvc:!*:20000::::::"
OLDSVC_EXPIRED = "oldsvc:!*:20000:::::1:"
# A password hash the fake shadow file holds; no logged line may carry it.
HASH = "$6$NEWSECRETHASH"


def _with(path: str, *replace: tuple[str, str], add: str = "") -> dict[str, str]:
    """The fake host's `path` with each (old, new) substring replaced and `add` appended."""
    text = ACCOUNT_FILES[path]
    for old, new in replace:
        assert old in text, (path, old)
        text = text.replace(old, new)
    return {path: text + add}


def _entries(*paths: str) -> str:
    return _report(*(f"{CHANGED_CONTENT}: {p}" for p in paths))


# Each row: the entries AIDE reports, and the `_run_check` setup that makes the files match.
ACCEPTED = {
    "a declared user added to all four files": (
        (PASSWD, GROUP, SHADOW, GSHADOW),
        {
            "rewrite": {
                **_with(PASSWD, add=SVC_PASSWD),
                **_with(GROUP, add=SVC_GROUP),
                **_with(SHADOW, add=SVC_SHADOW),
                **_with(GSHADOW, add=SVC_GSHADOW),
            }
        },
    ),
    "a declared user moved to its declared uid": (
        (PASSWD,),
        {"snapshot": _with(PASSWD, ("claude:x:900:", "claude:x:1001:"))},
    ),
    "a declared user moved with no comment, as live claude has none": (
        (PASSWD,),
        {
            "rewrite": _with(PASSWD, (":Claude agent:", "::")),
            "snapshot": _with(
                PASSWD, ("claude:x:900:985:Claude agent:", "claude:x:1001:985::")
            ),
        },
    ),
    "an expired user's expiry date set to 1, as usermod --expiredate 1 writes it": (
        (SHADOW,),
        {"rewrite": _with(SHADOW, (OLDSVC_SHADOW, OLDSVC_EXPIRED))},
    ),
    "a declared user's group moved to its declared gid, with its passwd gid": (
        (PASSWD, GROUP),
        {
            "snapshot": {
                **_with(PASSWD, ("claude:x:900:985:", "claude:x:900:1001:")),
                **_with(GROUP, ("claude:x:985:", "claude:x:1001:")),
            }
        },
    ),
}

REJECTED = {
    "no snapshot from the last accepted run": (
        PASSWD,
        {"rewrite": _with(PASSWD, add=SVC_PASSWD), "snapshot": {PASSWD: None}},
        "no copy of it from the run the baseline advanced to, so its change cannot be judged",
    ),
    "an unreadable users declaration": (
        PASSWD,
        {"rewrite": {**_with(PASSWD, add=SVC_PASSWD), HOST_MANIFEST: "{"}},
        "the host package's users declaration is unreadable",
    ),
    "a declared user with the wrong uid": (
        PASSWD,
        {"rewrite": _with(PASSWD, add=SVC_PASSWD.replace(":906:906:", ":916:906:"))},
        "the line of declared user svc does not match its declaration",
    ),
    "a declared user with another shell": (
        PASSWD,
        {
            "rewrite": _with(
                PASSWD, ("/var/lib/claude:/bin/bash", "/var/lib/claude:/bin/sh")
            )
        },
        "the line of declared user claude does not match its declaration",
    ),
    "an undeclared uid below 1000": (
        PASSWD,
        {"rewrite": _with(PASSWD, add="backdoor:x:950:950::/:/bin/bash\n")},
        "changes the line of an account the host package does not declare: backdoor",
    ),
    "an undeclared uid 0": (
        PASSWD,
        {"rewrite": _with(PASSWD, add="toor:x:0:0::/root:/bin/bash\n")},
        "changes the line of an account the host package does not declare: toor",
    ),
    "a declared account removed": (
        PASSWD,
        {
            "rewrite": _with(
                PASSWD,
                ("claude:x:900:985:Claude agent:/var/lib/claude:/bin/bash\n", ""),
            )
        },
        "removes the line of an account: claude",
    ),
    "a name twice": (
        PASSWD,
        {
            "rewrite": _with(
                PASSWD, add="claude:x:900:985:Claude agent:/var/lib/claude:/bin/bash\n"
            )
        },
        "names an account twice: claude",
    ),
    "a change with no line that differs from the snapshot": (
        PASSWD,
        {},
        "AIDE reports a change, but no line differs from the copy",
    ),
    "a declared user's gid pointing at another group": (
        PASSWD,
        {"rewrite": _with(PASSWD, ("claude:x:900:985:", "claude:x:900:990:"))},
        "declared user claude's gid is not its declared gid",
    ),
    "an undeclared group gaining a declared member": (
        GROUP,
        {
            "rewrite": _with(
                GROUP, ("ssh-users:x:990:ubuntu", "ssh-users:x:990:ubuntu,claude")
            )
        },
        "changes the line of an account the host package does not declare: ssh-users",
    ),
    "a declared group with gid 0": (
        GROUP,
        {"rewrite": _with(GROUP, ("claude:x:985:", "claude:x:0:"))},
        "the gid of declared group claude is not its declared gid",
    ),
    "a declared group whose gid collides with another group": (
        GROUP,
        {"rewrite": _with(GROUP, ("claude:x:985:", "claude:x:990:"))},
        "the gid of declared group claude is not its declared gid",
    ),
    "a declared user and its group moved together to an undeclared, unshared gid": (
        PASSWD,
        {
            "rewrite": {
                **_with(PASSWD, ("claude:x:900:985:", "claude:x:900:987:")),
                **_with(GROUP, ("claude:x:985:", "claude:x:987:")),
            }
        },
        "declared user claude's gid is not its declared gid",
    ),
    "a declared group at its declared gid while another group holds it too": (
        GROUP,
        {
            "rewrite": _with(
                GROUP, ("claude:x:985:", "claude:x:985:svc"), ("990", "985")
            ),
            "snapshot": _with(GROUP, ("990", "985")),
        },
        "the declared gid of group claude is another group's too",
    ),
    "a declared group with an undeclared member": (
        GROUP,
        {"rewrite": _with(GROUP, ("claude:x:985:", "claude:x:985:ubuntu"))},
        "declared group claude has a member the host package does not declare",
    ),
    "an undeclared account's password changed": (
        SHADOW,
        {"rewrite": _with(SHADOW, ("$6$SECRETHASH", HASH))},
        "changes the line of an account the host package does not declare: ubuntu",
    ),
    "a declared account given a password": (
        SHADOW,
        {"rewrite": _with(SHADOW, ("claude:!*:", f"claude:{HASH}:"))},
        "declared account claude has a password",
    ),
    "a declared group gaining a gshadow administrator": (
        GSHADOW,
        {"rewrite": _with(GSHADOW, ("claude:!::", "claude:!:ubuntu:"))},
        "declared group claude has an administrator or a member in gshadow",
    ),
    "a declared group gaining a gshadow member": (
        GSHADOW,
        {"rewrite": _with(GSHADOW, ("claude:!::", "claude:!::ubuntu"))},
        "declared group claude has an administrator or a member in gshadow",
    ),
    "an expired user's expiry with its password changed too": (
        SHADOW,
        {"rewrite": _with(SHADOW, (OLDSVC_SHADOW, OLDSVC_EXPIRED.replace("!*", HASH)))},
        "changes the line of an account the host package does not declare: oldsvc",
    ),
    "an expired user's expiry date set to another day": (
        SHADOW,
        {
            "rewrite": _with(
                SHADOW, (OLDSVC_SHADOW, OLDSVC_EXPIRED.replace(":1:", ":2:"))
            )
        },
        "changes the line of an account the host package does not declare: oldsvc",
    ),
    "an expiry on an account the arm's record does not list": (
        SHADOW,
        {
            "rewrite": _with(
                SHADOW, ("root:!*:20000:0:99999:7:::", "root:!*:20000:0:99999:7::1:")
            )
        },
        "changes the line of an account the host package does not declare: root",
    ),
    "an expiry with no readable reconcile record": (
        SHADOW,
        {
            "rewrite": {
                **_with(SHADOW, (OLDSVC_SHADOW, OLDSVC_EXPIRED)),
                RECONCILE_RECORD: "{",
            }
        },
        "changes the line of an account the host package does not declare: oldsvc",
    ),
    "a shadow line with no colon, all of it a hash": (
        SHADOW,
        {"rewrite": _with(SHADOW, add=f"{HASH}\n")},
        "changes the line of an account the host package does not declare: "
        "one whose name is not a valid account name",
    ),
}


def _assert_no_hash(logged: list[str]) -> None:
    leaked = [line for line in logged if "SECRETHASH" in line]
    assert not leaked, leaked


@pytest.mark.parametrize(("paths", "setup"), ACCEPTED.values(), ids=ACCEPTED.keys())
def test_an_account_change_the_users_arm_declares_is_clean(tmp_path, paths, setup):
    _, pushed, logged, _ = _run_check(tmp_path, _entries(*paths), rc=4, **setup)
    assert pushed[0] == "up", pushed
    _assert_no_hash(logged)


@pytest.mark.parametrize(
    ("path", "setup", "reason"), REJECTED.values(), ids=REJECTED.keys()
)
def test_an_account_change_the_users_arm_does_not_declare_is_flagged(
    tmp_path, path, setup, reason
):
    _, pushed, logged, _ = _run_check(tmp_path, _entries(path), rc=4, **setup)
    assert pushed[0] == "down", pushed
    assert f"aide-check: unexplained: {path} ({reason})" in logged, logged
    _assert_no_hash(logged)


def test_a_mode_change_to_an_account_file_still_pages(tmp_path):
    report = _report(f"{CHANGED_MODE}: {SHADOW}")
    _, pushed, _, _ = _run_check(tmp_path, report, rc=4)
    assert pushed[0] == "down", pushed
    assert f"{SHADOW} (owner, group or permissions changed)" in pushed[1], pushed[1]


def _snapshot(tmp_path, name: str) -> str:
    return (tmp_path / "aide-db" / "accounts" / name).read_text()


def test_an_advance_keeps_copies_of_the_files_as_the_scan_began(tmp_path):
    rewrite = _with(PASSWD, add=SVC_PASSWD)
    _run_check(tmp_path, CLEAN_REPORT, rc=0, rewrite=rewrite)
    accounts = tmp_path / "aide-db" / "accounts"
    assert _snapshot(tmp_path, "passwd") == rewrite[PASSWD]
    assert sorted(p.name for p in accounts.iterdir()) == [
        "group",
        "gshadow",
        "passwd",
        "shadow",
    ]
    assert {p.stat().st_mode & 0o777 for p in accounts.iterdir()} == {0o600}
    assert accounts.stat().st_mode & 0o777 == 0o700


def test_an_edit_during_the_scan_is_left_out_of_the_copies(tmp_path):
    """The copy is taken before the scan, so the edit is in the next diff, where it is judged."""
    _run_check(
        tmp_path,
        CLEAN_REPORT,
        rc=0,
        during_scan={PASSWD: "backdoor:x:950:950::/:/bin/bash\n"},
    )
    assert _snapshot(tmp_path, "passwd") == ACCOUNT_FILES[PASSWD]


def test_a_down_run_keeps_the_last_accepted_copies(tmp_path):
    _run_check(
        tmp_path,
        _entries(PASSWD),
        rc=4,
        rewrite=_with(PASSWD, add="backdoor:x:950:950::/:/bin/bash\n"),
    )
    assert _snapshot(tmp_path, "passwd") == ACCOUNT_FILES[PASSWD]


def test_accept_installs_the_reviewed_runs_copies(tmp_path):
    pending = _with(PASSWD, add="backdoor:x:950:950::/:/bin/bash\n")[PASSWD]
    _run_check(
        tmp_path,
        CLEAN_REPORT,
        rc=0,
        args=("--accept",),
        pending=True,
        pending_snapshot={PASSWD: pending},
    )
    assert _snapshot(tmp_path, "passwd") == pending
