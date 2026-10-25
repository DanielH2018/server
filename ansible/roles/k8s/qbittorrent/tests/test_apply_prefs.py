"""Tests for the qBittorrent preference diff.

THE INJECTION POINT IS `diff_prefs`, DELIBERATELY. It takes (current, desired) and
returns the delta, so every case below exercises the decision the script actually makes
without a socket. Testing the HTTP transport instead would prove only that urllib works
and would leave the comparison — the part with the trap in it — unexercised.
"""

import fnmatch
import io
from email.message import Message
import urllib.error

import apply_prefs
import pytest


def _int_pref(key: str) -> int:
    value = apply_prefs.DESIRED[key]
    assert isinstance(value, int)
    return value


def test_no_changes_when_everything_matches() -> None:
    current = dict(apply_prefs.DESIRED)
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {}


def test_reports_current_and_desired_for_a_changed_key() -> None:
    current = dict(apply_prefs.DESIRED) | {"connection_speed": 30}
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {
        "connection_speed": (30, 100)
    }


def test_absent_key_counts_as_a_change() -> None:
    current = dict(apply_prefs.DESIRED)
    del current["hashing_threads"]
    changes = apply_prefs.diff_prefs(current, apply_prefs.DESIRED)
    assert changes == {"hashing_threads": ("<absent>", 2)}


def test_extra_keys_in_current_are_ignored() -> None:
    # The live API returns ~200 preferences; this script owns only the DESIRED keys and
    # must not report the rest as drift.
    current = dict(apply_prefs.DESIRED) | {"web_ui_port": 8080, "dht": True}
    assert apply_prefs.diff_prefs(current, apply_prefs.DESIRED) == {}


@pytest.mark.parametrize(
    ("filename", "excluded"),
    [
        # The three fake-episode shapes indexers served (#6274).
        ("isooy2.exe", True),
        ("VisionQuest S01E05 Firewall 1080p WEB H264-EZTV.7z", True),
        ("[SubsPlease] Kimetsu no Yaiba - 05 (1080p) [57D14100].iso", True),
        ("Setup.EXE", True),
        # RAR volumes: nothing in the stack unpacks them, so they cannot be imported.
        ("show.s01e05.rar", True),
        ("show.s01e05.r00", True),
        ("show.s01e05.r17", True),
        # What a real release carries must still download.
        ("Show.S01E05.rmvb", False),
        ("Show.S01E05.1080p.WEB.H264-GRP.mkv", False),
        ("Show.S01E05.1080p.WEB.H264-GRP.mp4", False),
        ("[shincaps] Show - 12 (AT-X 1440x1080 MPEG2 AAC).ts", False),
        ("Show.S01E05.en.srt", False),
        ("Show.S01E05.ass", False),
        ("show.s01e05.nfo", False),
    ],
)
def test_excluded_file_names_skip_fakes_and_keep_real_releases(
    filename: str, excluded: bool
) -> None:
    # qBittorrent matches each pattern as a case-insensitive wildcard against the file
    # name; fnmatch on lowered strings is the same match for these `*.ext` patterns.
    assert apply_prefs.DESIRED["excluded_file_names_enabled"] is True
    patterns = apply_prefs.EXCLUDED_FILE_NAMES
    matched = any(fnmatch.fnmatchcase(filename.lower(), p.lower()) for p in patterns)
    assert matched is excluded


def test_every_desired_key_is_reported_when_current_is_empty() -> None:
    changes = apply_prefs.diff_prefs({}, apply_prefs.DESIRED)
    assert set(changes) == set(apply_prefs.DESIRED)


def test_desired_values_are_scalars_the_api_accepts() -> None:
    # setPreferences serialises to JSON, and qBittorrent silently ignores a key whose type
    # it does not expect — which the read-back in main() would then report as NOT APPLIED.
    # Catch a bad literal here instead. The one string is the "\n"-joined pattern list.
    for key, value in apply_prefs.DESIRED.items():
        expected = str if key == "excluded_file_names" else bool | int
        assert isinstance(value, expected), f"{key} is {type(value).__name__}"


def test_hashing_threads_stays_within_the_container_cpu_limit() -> None:
    # qbittorrent_k8s_cpu_limit is "2" in the role defaults. More hashing threads than
    # that buys nothing and invites throttling, since the CPU REQUEST is only 100m.
    assert _int_pref("hashing_threads") <= 2


def test_memory_working_set_fits_the_container_memory_limit() -> None:
    # qbittorrent_k8s_mem_limit is 2048Mi. libtorrent 2.0's mmap I/O holds dirty pages up
    # to this bound, so it has to leave headroom for the rest of the process.
    #
    # NB the bound is the only thing this asserts, and that is now the only thing worth
    # asserting: the setting is inert on Linux (see the DESIRED comment), so it cannot
    # actually exceed anything. Kept so a future edit can't raise it past the container
    # limit on the assumption that it works.
    assert _int_pref("memory_working_set_limit") <= 1536


def test_checking_memory_use_leaves_room_under_the_container_memory_limit() -> None:
    # In MiB, and additive to the resident session rather than carved out of it: the recheck
    # buffer is held on top of whatever the session already holds. Against a 2048Mi container
    # limit — with mmap page cache already growing to fill it, since memory_working_set_limit
    # bounds nothing here — a large value turns a routine post-restore recheck into the one
    # operation that can push the cgroup over. 256 keeps it to an eighth of the limit.
    assert _int_pref("checking_memory_use") <= 256


class _StubClient(apply_prefs.QbtClient):
    """Stands in for QbtClient so main()'s --dry-run exit code is tested without a socket.

    `answers` is the poll's script: each call pops the next value, and the last one repeats.
    It skips the base __init__, so it builds no opener and can open no connection.
    """

    def __init__(
        self, preferences: dict[str, object], answers: tuple[bool, ...] = (True,)
    ) -> None:
        self._preferences = preferences
        self._answers = list(answers)
        self.answer_calls = 0
        self.login_calls: list[tuple[str, str]] = []

    def answers(self) -> bool:
        self.answer_calls += 1
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]

    def login(self, username: str, password: str) -> None:
        self.login_calls.append((username, password))

    def preferences(self) -> dict[str, object]:
        return self._preferences


class _FakeClock:
    """A monotonic clock that only `sleep` advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _run_dry_run(
    monkeypatch,
    current_preferences: dict[str, object],
    *extra: str,
    client: _StubClient | None = None,
    clock: _FakeClock | None = None,
) -> int:
    monkeypatch.setenv("QBT_USERNAME", "admin")
    monkeypatch.setenv("QBT_PASSWORD", "hunter2")
    stub = client or _StubClient(current_preferences)
    clock = clock or _FakeClock()
    return apply_prefs.main(
        ["--dry-run", "--url", "http://qbittorrent.test:8080", *extra],
        client_factory=lambda base_url: stub,
        sleep=clock.sleep,
        clock=clock,
    )


# THE MUTATION: a cron branching on exit code must be able to tell "found drift" from "nothing
# to do". These two tests are the pair that catches a regression to `return 0` on --dry-run —
# flip EXIT_DRIFT back to EXIT_OK in main() and
# test_dry_run_exits_drift_when_a_preference_has_changed fails while its sibling keeps passing.
def test_dry_run_exits_ok_when_nothing_has_changed(monkeypatch) -> None:
    exit_code = _run_dry_run(monkeypatch, dict(apply_prefs.DESIRED))
    assert exit_code == apply_prefs.EXIT_OK


def test_dry_run_exits_drift_when_a_preference_has_changed(monkeypatch) -> None:
    # This also covers "--dry-run never writes": set_preferences is not on _StubClient at all,
    # so main() raises AttributeError — not EXIT_DRIFT — if the early return stops gating the
    # write. A separate test asserting the same exit code on the same input added no branch.
    current: dict[str, object] = {**apply_prefs.DESIRED, "connection_speed": 30}
    exit_code = _run_dry_run(monkeypatch, current)
    assert exit_code == apply_prefs.EXIT_DRIFT


def test_missing_credentials_exits_bad_args(monkeypatch) -> None:
    monkeypatch.delenv("QBT_USERNAME", raising=False)
    monkeypatch.delenv("QBT_PASSWORD", raising=False)
    assert apply_prefs.main(["--dry-run"]) == apply_prefs.EXIT_BAD_ARGS


# The connect retry (the H2 plan's *Who applies each grant*): a CronJob pod's first dials can
# be dropped through an open fence, so the check polls without credentials for up to 60 s, and
# then logs in exactly once, because every answered bad login counts against qBittorrent's
# web_ui_max_auth_fail_count of 5.
def test_connect_wait_polls_until_the_webui_answers_then_logs_in_once(
    monkeypatch,
) -> None:
    stub = _StubClient(dict(apply_prefs.DESIRED), answers=(False, False, True))
    clock = _FakeClock()
    exit_code = _run_dry_run(
        monkeypatch, {}, "--connect-wait", "60", client=stub, clock=clock
    )
    assert exit_code == apply_prefs.EXIT_OK
    assert stub.answer_calls == 3
    assert clock.sleeps == [5.0, 5.0]
    assert stub.login_calls == [("admin", "hunter2")]


def test_connect_wait_gives_up_at_its_bound_without_logging_in(monkeypatch) -> None:
    stub = _StubClient(dict(apply_prefs.DESIRED), answers=(False,))
    clock = _FakeClock()
    exit_code = _run_dry_run(
        monkeypatch, {}, "--connect-wait", "60", client=stub, clock=clock
    )
    assert exit_code == apply_prefs.EXIT_UNREACHABLE
    assert clock.now == 60.0
    assert stub.login_calls == []


def test_a_login_that_got_an_answer_is_never_retried(monkeypatch) -> None:
    class _RejectingClient(_StubClient):
        def login(self, username: str, password: str) -> None:
            super().login(username, password)
            raise SystemExit("qBittorrent login failed: Fails.")

    stub = _RejectingClient(dict(apply_prefs.DESIRED))
    with pytest.raises(SystemExit, match="Fails"):
        _run_dry_run(monkeypatch, {}, "--connect-wait", "60", client=stub)
    assert len(stub.login_calls) == 1


def test_without_connect_wait_main_does_not_poll(monkeypatch) -> None:
    stub = _StubClient(dict(apply_prefs.DESIRED), answers=(False,))
    assert _run_dry_run(monkeypatch, {}, client=stub) == apply_prefs.EXIT_OK
    assert stub.answer_calls == 0


def test_an_http_error_status_counts_as_an_answer() -> None:
    """A 403 to the unauthenticated poll proves the connection carried, so polling stops."""

    class _Forbidden(apply_prefs.QbtClient):
        def _get(self, path: str) -> str:
            raise urllib.error.HTTPError(
                path, 403, "Forbidden", Message(), io.BytesIO()
            )

    class _Refused(apply_prefs.QbtClient):
        def _get(self, path: str) -> str:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    assert _Forbidden("http://qbittorrent.test:8080").answers() is True
    assert _Refused("http://qbittorrent.test:8080").answers() is False
