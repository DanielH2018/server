"""disk-health.sh writes its verdict to node_exporter's textfile collector, before any push.

The plan is docs/target-architecture/monitoring/m3-textfile-timer.md, *PR 2*: the producer metric
contract's three `homelab_check_*` series, an atomic 0644 write, a missing Kuma token that skips
only the pushes, and a boot-grace run that writes nothing. The real template is rendered and run
under bash. Its push library and its two env files are repointed at stubs, and `df`, `curl` and
`logger` are stubbed on PATH, so neither the runner's uptime nor its disk decides a case. The
assertions read the file the run leaves in `TEXTFILE_DIR`.
"""

import re
import time

from lib.proc_testing import fake_bin, run, write_exec
from _shell_render import render_shell_script

TEMPLATE = ("setup", "k3s", "disk-health.sh.j2")
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
KUMA_ENV = "/etc/rancher/k3s/kuma-push.env"
HC_ENV = "/etc/healthchecks/ping.env"
PROM = "disk-health.prom"
SEP = "\x1e"

# kuma_push records its status and message, and whether the textfile already existed when the
# push ran. boot_grace_active answers what the case sets, never the runner's real uptime.
LIB_STUB = """\
kuma_push() {
  local written=no
  [[ -f "$TEXTFILE_DIR/disk-health.prom" ]] && written=yes
  printf '%s\\n%s\\n%s\\x1e' "$1" "$2" "$written" >> "$KUMA_PUSH_OUT"
}
boot_grace_active() { [[ "$BOOT_GRACE" == "1" ]]; }
"""


def _df(used_pct: int) -> str:
    """A `df -P /` stub in the shape the real one prints: a header, then one filesystem line."""
    return (
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        f"echo '/dev/mapper/vg-root 100000000 {used_pct}000000 50000000 {used_pct}% /'\n"
    )


def _run(tmp_path, used_pct=40, token="stubtoken", boot_grace=False, textfile_dir=None):
    """Run the rendered script once; return (textfile dir, every push as (status, msg, written))."""
    body = render_shell_script(*TEMPLATE)
    for literal in (REAL_LIB, KUMA_ENV, HC_ENV):
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(LIB_STUB)
    kuma_env = tmp_path / "kuma-push.env"
    kuma_env.write_text(f"DANIEL_BOX_DISK_PUSH_TOKEN={token}\n" if token else "")
    hc_env = tmp_path / "ping.env"
    hc_env.write_text("")
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    if textfile_dir is None:
        textfile_dir = tmp_path / "textfile"
        textfile_dir.mkdir(exist_ok=True)

    script = write_exec(
        tmp_path / "disk-health.sh",
        body.replace(REAL_LIB, str(lib))
        .replace(KUMA_ENV, str(kuma_env))
        .replace(HC_ENV, str(hc_env)),
    )
    stubs = fake_bin(
        tmp_path / "bin",
        df=_df(used_pct),
        curl="exit 0\n",
        logger=f'echo "$@" >> "{tmp_path / "logged"}"\n',
    )
    env = {
        "TEXTFILE_DIR": str(textfile_dir),
        "KUMA_PUSH_OUT": str(pushed),
        "BOOT_GRACE": "1" if boot_grace else "0",
        "PATH": "/usr/bin:/bin",
    }
    run(["bash", str(script)], env=env, stub_bin=stubs, check=True)
    pushes = [tuple(p.split("\n")[:3]) for p in pushed.read_text().split(SEP) if p]
    return textfile_dir, pushes


def _series(textfile_dir) -> dict[str, str]:
    """`name{labels}` -> value for every sample line in the written file."""
    lines = (textfile_dir / PROM).read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


def _rendered_deadline() -> str:
    match = re.search(r"^DEADLINE_S=(\d+)$", render_shell_script(*TEMPLATE), re.M)
    assert match, "disk-health.sh.j2 no longer renders a DEADLINE_S line"
    return match.group(1)


def test_an_up_run_writes_the_three_series_before_the_kuma_push(tmp_path):
    before = int(time.time())
    textfile_dir, pushes = _run(tmp_path, used_pct=40)
    after = int(time.time())

    series = _series(textfile_dir)
    assert series['homelab_check_success{check="disk-health"}'] == "1"
    stamp = int(series['homelab_check_last_run_timestamp_seconds{check="disk-health"}'])
    assert before <= stamp <= after
    assert (
        series['homelab_check_deadline_seconds{check="disk-health"}']
        == _rendered_deadline()
    )
    assert series['homelab_disk_root_used_ratio{check="disk-health"}'] == "0.40"
    assert [(status, written) for status, _, written in pushes] == [("up", "yes")]


def test_a_down_run_writes_success_zero(tmp_path):
    textfile_dir, pushes = _run(tmp_path, used_pct=90)
    assert _series(textfile_dir)['homelab_check_success{check="disk-health"}'] == "0"
    assert [status for status, _, _ in pushes] == ["down"]


def test_a_run_without_a_token_still_writes_the_file_and_pushes_nothing(tmp_path):
    textfile_dir, pushes = _run(tmp_path, token="")
    assert _series(textfile_dir)['homelab_check_success{check="disk-health"}'] == "1"
    assert pushes == []


def test_a_boot_grace_run_leaves_the_previous_file_byte_identical(tmp_path):
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    previous = b'homelab_check_success{check="disk-health"} 0\n'
    (textfile_dir / PROM).write_bytes(previous)

    _, pushes = _run(tmp_path, boot_grace=True, textfile_dir=textfile_dir)

    assert (textfile_dir / PROM).read_bytes() == previous
    assert [(s, m) for s, m, _ in pushes] == [("up", "skipped — host still booting")]


def test_the_file_is_world_readable_and_no_temp_file_is_left(tmp_path):
    textfile_dir, _ = _run(tmp_path)
    assert sorted(p.name for p in textfile_dir.iterdir()) == [PROM]
    assert (textfile_dir / PROM).stat().st_mode & 0o777 == 0o644


def test_a_failed_write_is_logged_and_the_push_still_goes(tmp_path):
    missing = tmp_path / "no-such-dir"
    _, pushes = _run(tmp_path, textfile_dir=missing)
    assert not missing.exists()
    assert "textfile write to" in (tmp_path / "logged").read_text()
    assert [status for status, _, _ in pushes] == ["up"]
