"""The root inspector's socket helper and client, against a socketpair and a real unix socket (#6350).

The helper runs the real `homelab_rootq.py` through `sys.executable`, rooted under a scratch
directory by the seam the script ignores as root, so nothing here runs as root. A request that
the helper refuses itself never starts the script: those cases name a script that does not
exist, so an attempt to run one would answer exit 3 instead of exit 2.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_rootq_socket.py
"""

import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import homelab_rootq as rootq
import homelab_rootq_socket as helper
import pytest
import rootq_socket as client
from lib.git_testing import init_repo
from lib.socket_testing import listening, short_socket_path

FILES = Path(rootq.__file__).parent
NOWHERE = ("/nonexistent/homelab-rootq",)
SECRET = "network:\n  version: 2\n"


@pytest.fixture
def scratch():
    """A scratch tree under the real home, whose ancestors are root's or this user's alone."""
    home = Path(os.path.realpath(Path.home()))
    if not os.access(home, os.W_OK) or any(
        Path(p).stat().st_mode & 0o022 for p in [home, *home.parents]
    ):
        pytest.skip("the home or a parent of it is group- or world-writable")
    old = os.umask(0o022)
    root = Path(tempfile.mkdtemp(prefix="rootq-sock-test-", dir=home))
    os.chmod(root, 0o755)
    (root / "etc" / "netplan").mkdir(parents=True)
    (root / "etc" / "netplan" / "a.yaml").write_text(SECRET)
    yield root
    os.umask(old)
    shutil.rmtree(root, ignore_errors=True)


def real_script() -> tuple[str, ...]:
    return (sys.executable, str(FILES / "homelab_rootq.py"))


def seam(root: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOMELAB_ROOTQ_ROOT": str(root),
        "HOMELAB_ROOTQ_OWNER_UID": str(os.getuid()),
    }


def exchange(
    request: bytes, script=NOWHERE, env=None, close_write=True, timeout_s=5
) -> dict:
    """Send REQUEST to the helper over a socketpair, and return its decoded answer."""
    mine, theirs = socket.socketpair()

    def answer() -> None:
        # main() closes the connection after serve; the test closes the write side to match.
        helper.serve(theirs, script, env, timeout_s)
        theirs.shutdown(socket.SHUT_WR)

    with mine, theirs:
        server = threading.Thread(target=answer)
        server.start()
        mine.sendall(request)
        if close_write:
            mine.shutdown(socket.SHUT_WR)
        raw = b""
        while chunk := mine.recv(65536):
            raw += chunk
        server.join()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    return json.loads(raw)


def asked(scratch: Path, line: str) -> dict:
    return exchange(line.encode() + b"\n", real_script(), seam(scratch))


# ── Must refuse ───────────────────────────────────────────────────────────────────────────


def test_a_path_outside_the_allowlist_is_refused_with_rootqs_own_text(scratch) -> None:
    for line in ("cat /etc/shadow", "stat /etc/shadow", "sha256 /etc/shadow"):
        answer = asked(scratch, line)
        assert answer["exit"] == 2, line
        assert answer["stdout"] == ""
        assert answer["stderr"] == "refused: /etc/shadow is on the deny list" + (
            " and not on the allowlist\n" if line.startswith("stat") else "\n"
        )
    unlisted = asked(scratch, "sha256 /etc/passwd")
    assert unlisted["exit"] == 2
    assert "is not on the allowlist" in unlisted["stderr"]


@pytest.mark.parametrize(
    "line, reason",
    [
        ("cat /etc/netplan/../shadow", "empty, . or .. component"),
        ("stat /etc/netplan/./a.yaml", "empty, . or .. component"),
        ("stat //etc/netplan/a.yaml", "empty, . or .. component"),
        ("stat /etc/netplan/", "empty, . or .. component"),
        ("stat /", "empty, . or .. component"),
        ("stat etc/netplan/a.yaml", "PATH must be absolute"),
        ("stat /etc/netplan/a.yaml;id", "PATH must be absolute"),
        ("stat /etc/netplan/$(id)", "PATH must be absolute"),
        ("rm /etc/netplan/a.yaml", "unknown verb 'rm'"),
        ("bash -c id", "unknown verb 'bash'"),
        ("STAT /etc/netplan/a.yaml", "unknown verb 'STAT'"),
        ("nft list ruleset", "nft takes 0 argument(s), not 2"),
        ("cat", "cat takes 1 argument(s), not 0"),
        ("cat /etc/netplan/a.yaml /etc/shadow", "cat takes 1 argument(s), not 2"),
        ("matches /etc/netplan/a.yaml conf/a.yaml", "64-hex SHA-256"),
        ("matches /etc/netplan/a.yaml " + "AB" * 32, "64-hex SHA-256"),
        ("stat  /etc/netplan/a.yaml", "single spaces"),
        ("", "single spaces"),
        ("stat\t/etc/netplan/a.yaml", "printable ASCII"),
        ("stat /etc/netplan/é.yaml", "printable ASCII"),
    ],
)
def test_a_request_the_helper_refuses_never_starts_rootq(line, reason) -> None:
    answer = exchange(line.encode() + b"\n")
    assert answer["exit"] == 2, answer
    assert answer["stdout"] == ""
    assert answer["stderr"].startswith("refused: ")
    assert reason in answer["stderr"]


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"cat /" + b"a" * helper.REQUEST_MAX + b"\n",
        b"x" * helper.REQUEST_MAX + b"\n",
        b"x" * (helper.REQUEST_MAX * 3),
    ],
)
def test_an_oversize_request_is_refused_without_reading_it_all(request_bytes) -> None:
    # The last case never sends a newline and never closes: the helper stops at its bound.
    answer = exchange(request_bytes, close_write=False)
    assert answer["exit"] == 2
    assert f"over {helper.REQUEST_MAX} bytes" in answer["stderr"]


def test_the_longest_request_rootq_accepts_is_not_called_oversize() -> None:
    line = "matches /" + "a" * 4095 + " " + "0" * 64
    assert len(line) + 1 <= helper.REQUEST_MAX
    assert helper.parse(line.encode())[0] == "matches"


@pytest.mark.parametrize(
    "request_bytes, reason",
    [
        (b"stat /etc/netplan/a.yaml\nstat /etc/shadow\n", "nothing after it"),
        (b"stat /etc/netplan/a.yaml", "ended before its newline"),
        (b"", "ended before its newline"),
    ],
)
def test_a_request_that_is_not_exactly_one_line_is_refused(request_bytes, reason):
    answer = exchange(request_bytes)
    assert answer["exit"] == 2
    assert reason in answer["stderr"]


def test_a_client_that_sends_nothing_is_dropped_after_the_read_timeout():
    answer = exchange(b"", close_write=False, timeout_s=0.2)
    assert answer["exit"] == 2
    assert "no request line within" in answer["stderr"]


def test_a_client_sending_a_byte_at_a_time_gets_one_deadline_for_the_whole_line():
    mine, theirs = socket.socketpair()
    with mine, theirs:
        reader = threading.Thread(
            target=lambda: helper.serve(theirs, NOWHERE, None, 0.4)
        )
        reader.start()
        for byte in b"stat /etc/netplan/a.yaml\n":
            try:
                mine.sendall(bytes([byte]))
            except OSError:
                break
            time.sleep(0.1)
        reader.join()
        assert "no request line within" in json.loads(mine.recv(65536))["stderr"]


# ── Must run ──────────────────────────────────────────────────────────────────────────────


def test_a_listed_verb_on_a_listed_path_is_answered(scratch) -> None:
    path = f"{scratch}/etc/netplan/a.yaml"
    hashed = asked(scratch, f"sha256 {path}")
    assert hashed == {
        "exit": 0,
        "stdout": hashlib.sha256(SECRET.encode()).hexdigest() + "\n",
        "stderr": "",
    }
    printed = asked(scratch, f"cat {path}")
    assert (printed["exit"], printed["stdout"]) == (0, SECRET)


def test_matches_answers_with_rootqs_exit_status(scratch) -> None:
    path = f"{scratch}/etc/netplan/a.yaml"
    same = hashlib.sha256(SECRET.encode()).hexdigest()
    assert asked(scratch, f"matches {path} {same}") == {
        "exit": 0,
        "stdout": "match\n",
        "stderr": "",
    }
    differ = asked(scratch, f"matches {path} {'0' * 64}")
    assert (differ["exit"], differ["stdout"]) == (1, "differ\n")


def test_help_is_the_one_flag_the_helper_passes_on(scratch) -> None:
    answer = asked(scratch, "--help")
    assert answer["exit"] == 0
    assert "/etc/netplan/*.yaml" in answer["stdout"]
    assert exchange(b"--help --version\n")["exit"] == 2
    assert exchange(b"-h\n")["exit"] == 2


# ── How the helper starts rootq ───────────────────────────────────────────────────────────


def test_the_helper_starts_rootq_by_absolute_path_with_no_shell() -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    answer = json.loads(
        helper.run_rootq(["stat", "/etc/netplan/a.yaml"], runner=fake_run)
    )
    assert answer == {"exit": 0, "stdout": "ok\n", "stderr": ""}
    assert seen["argv"] == [
        "/usr/local/sbin/homelab-rootq",
        "stat",
        "/etc/netplan/a.yaml",
    ]
    assert not seen.get("shell")
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["env"] == helper.ENV
    assert seen["timeout"] == helper.RUN_TIMEOUT_S


def test_the_helpers_verbs_are_exactly_rootqs() -> None:
    assert set(helper.ARITY) == {*rootq.TIER1, *rootq.COMMANDS, "cat"}
    assert helper.PATH_SHAPE.pattern == rootq.PATH_SHAPE.pattern
    assert helper.HEX64.pattern == rootq.HEX64.pattern
    assert helper.ARITY["matches"] == 2
    assert all(helper.ARITY[verb] == 0 for verb in rootq.COMMANDS)


def test_the_root_helper_and_the_client_parse_with_the_3_12_grammar() -> None:
    # The hosts run both under Ubuntu 24.04's python3, so a 3.14-only construct would fail
    # every request there and none here.
    for name in ("homelab_rootq_socket.py", "rootq_socket.py"):
        ast.parse((FILES / name).read_text(), feature_version=(3, 12))


def test_the_helper_runs_only_from_a_socket_on_stdin(capsys) -> None:
    assert helper.main(["homelab-rootq-socket", "extra"]) == helper.USAGE_ERROR
    assert helper.main(["homelab-rootq-socket"], fd=987654) == helper.USAGE_ERROR
    assert "runs only from homelab-rootq.socket" in capsys.readouterr().err


# ── The client ────────────────────────────────────────────────────────────────────────────


def serve_once(listener: socket.socket, serve) -> threading.Thread:
    def accept() -> None:
        conn, _ = listener.accept()
        with conn:
            serve(conn)

    thread = threading.Thread(target=accept)
    thread.start()
    return thread


def test_the_client_prints_the_answer_and_exits_with_rootqs_status(scratch, capsys):
    path = f"{scratch}/etc/netplan/a.yaml"
    with short_socket_path() as sock, listening(sock) as listener:
        env = {"HOMELAB_ROOTQ_SOCKET": str(sock)}
        for argv, status in [
            (["cat", path], 0),
            (["cat", "/etc/shadow"], 2),
            (["matches", path, "0" * 64], 1),
        ]:
            thread = serve_once(
                listener, lambda c: helper.serve(c, real_script(), seam(scratch))
            )
            assert client.main(argv, env) == status
            thread.join()
    captured = capsys.readouterr()
    assert SECRET in captured.out
    assert "refused: /etc/shadow is on the deny list" in captured.err


def test_the_client_fails_closed_on_an_answer_that_is_not_whole(capsys) -> None:
    whole = json.dumps({"exit": 0, "stdout": "table", "stderr": ""}) + "\n"
    for sent in (b"", whole.encode()[:-3], whole.encode()[:-1], b'{"exit": "0"}\n'):
        with short_socket_path() as sock, listening(sock) as listener:
            thread = serve_once(
                listener, lambda c, sent=sent: (c.recv(64), c.sendall(sent))
            )
            assert client.main(["nft"], {"HOMELAB_ROOTQ_SOCKET": str(sock)}) == 70
            thread.join()
    assert "table" not in capsys.readouterr().out


def test_the_client_names_a_socket_it_cannot_reach(capsys) -> None:
    with short_socket_path() as sock:
        assert client.main(["nft"], {"HOMELAB_ROOTQ_SOCKET": str(sock)}) == 69
    assert "cannot reach" in capsys.readouterr().err


def test_matches_sends_only_the_hash_of_the_callers_repo_file(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    (repo / "conf").mkdir()
    (repo / "conf" / "a.yaml").write_text(SECRET)
    (repo / "out.yaml").symlink_to("/etc/hostname")
    monkeypatch.chdir(repo)
    digest = hashlib.sha256(SECRET.encode()).hexdigest()
    assert client.hash_repo_file("conf/a.yaml") == digest
    assert client.hash_repo_file(digest) == digest
    for bad, reason in [
        ("/etc/hostname", "relative"),
        ("../repo/conf/a.yaml", "relative"),
        ("..", "relative"),
        ("missing", "does not exist"),
        ("out.yaml", "outside the checkout"),
    ]:
        with pytest.raises(ValueError, match=reason):
            client.hash_repo_file(bad)
