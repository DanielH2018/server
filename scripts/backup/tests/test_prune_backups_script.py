"""What the drain command adds to `b2_drain.py`: the live list, the credentials, the sync."""

import subprocess
from pathlib import Path

import prune_backups
from lib.exit_codes import B2_DRAIN_ERRORED, B2_DRAIN_OK, B2_DRAIN_REFUSED
from lib.operator_kubectl import KubectlFailed


class Runs:
    """Records each process the command starts, and what it could see when it ran."""

    def __init__(self, drain_code: int = B2_DRAIN_OK, sync_code: int = 0):
        self.drain_code = drain_code
        self.sync_code = sync_code
        self.drain: list[str] = []
        self.drain_env: dict[str, str] = {}
        self.live_text = ""
        self.sync: list[str] = []

    def __call__(self, argv, env: dict | None = None, check=False):
        if argv[0] == "sudo":
            self.sync = argv
            return subprocess.CompletedProcess(argv, self.sync_code)
        self.drain, self.drain_env = argv, env or {}
        path = argv[argv.index("--live-volumes-file") + 1]
        self.live_text = Path(path).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, self.drain_code)


def secret(name: str) -> str:
    return f"value-of-{name}"


def live(*_args, **_kwargs) -> str:
    return "pvc-live\npvc-other\n"


def run(runs: Runs, *argv: str, kubectl=live) -> int:
    return prune_backups.main(list(argv), kubectl=kubectl, run=runs, secret=secret)


def test_a_dry_run_hands_the_drain_its_inputs_and_syncs_nothing():
    runs = Runs()
    assert run(runs, "--volumes", "pvc-a,pvc-b") == B2_DRAIN_OK
    assert runs.drain[1] == prune_backups.DRAIN
    assert runs.drain[runs.drain.index("--volumes") + 1] == "pvc-a,pvc-b"
    assert "--apply" not in runs.drain
    assert runs.live_text == "pvc-live\npvc-other\n"
    assert not runs.sync


def test_the_credentials_reach_the_drain_in_the_environment_and_never_in_argv():
    runs = Runs()
    run(runs, "--volumes", "pvc-a")
    assert runs.drain_env["B2_KEY_ID"] == "value-of-longhorn_b2_key_id"
    assert runs.drain_env["B2_APP_KEY"] == "value-of-longhorn_b2_application_key"
    assert not [arg for arg in runs.drain if "value-of" in arg]


def test_an_apply_syncs_the_b2_target():
    runs = Runs()
    assert run(runs, "--volumes", "pvc-a", "--apply") == B2_DRAIN_OK
    assert "--apply" in runs.drain
    assert runs.sync[:8] == [
        "sudo",
        "k3s",
        "kubectl",
        "-n",
        "longhorn-system",
        "patch",
        "backuptarget",
        "default",
    ]
    assert "syncRequestedAt" in runs.sync[-1]


def test_an_apply_that_refused_a_volume_still_syncs_and_reports_the_refusal():
    runs = Runs(drain_code=B2_DRAIN_REFUSED)
    assert run(runs, "--volumes", "pvc-a", "--apply") == B2_DRAIN_REFUSED
    assert runs.sync


def test_a_failed_drain_stops_before_the_sync():
    runs = Runs(drain_code=B2_DRAIN_ERRORED)
    assert run(runs, "--volumes", "pvc-a", "--apply") == B2_DRAIN_ERRORED
    assert not runs.sync


def test_a_failed_sync_is_an_error_that_prints_the_retry(capsys):
    runs = Runs(sync_code=1)
    assert run(runs, "--volumes", "pvc-a", "--apply") == B2_DRAIN_ERRORED
    assert "sudo k3s kubectl" in capsys.readouterr().err


def test_an_empty_live_list_refuses_before_the_drain_and_the_secrets(capsys):
    runs = Runs()
    code = run(runs, "--volumes", "pvc-a", "--apply", kubectl=lambda *a, **k: "\n")
    assert code == B2_DRAIN_ERRORED
    assert not runs.drain
    assert "no Longhorn volumes" in capsys.readouterr().err


def test_an_unreadable_live_list_refuses_before_the_drain(capsys):
    def failing(*_args, **_kwargs):
        raise KubectlFailed("forbidden")

    runs = Runs()
    assert run(runs, "--volumes", "pvc-a", kubectl=failing) == B2_DRAIN_ERRORED
    assert not runs.drain
    assert "forbidden" in capsys.readouterr().err


def test_the_volumes_file_and_fresh_listing_flags_pass_through():
    runs = Runs()
    run(runs, "--volumes-file", "/tmp/names", "--fresh-listing")
    assert runs.drain[runs.drain.index("--volumes-file") + 1] == "/tmp/names"
    assert "--fresh-listing" in runs.drain
    assert (
        runs.drain[runs.drain.index("--listing-file") + 1] == prune_backups.LISTING_FILE
    )
