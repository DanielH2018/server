"""disk-health.sh writes its verdict to node_exporter's textfile collector and pushes nothing to Kuma.

The plan is docs/target-architecture/monitoring/m3-textfile-timer.md, *PR 2*: the producer metric
contract's three `homelab_check_*` series, an atomic 0644 write and a boot-grace run that writes
nothing. PR 5 removed the Kuma push, so the library stub records any `kuma_push` call and every
case asserts there is none. The real template is rendered and run under bash. Its push library
and its env file are repointed at stubs, and `df`, `curl`,
`logger` and `date` are stubbed on PATH, so neither the runner's uptime, its disk nor its clock
decides a case. The assertions read the file the run leaves in `TEXTFILE_DIR`.
"""

import re

from lib.proc_testing import fake_bin, run, write_exec
from _shell_render import render_shell_script

TEMPLATE = ("setup", "k3s", "disk-health.sh.j2")
REAL_LIB = "/usr/local/lib/kuma-push-lib.sh"
HC_ENV = "/etc/healthchecks/ping.env"
PROM = "disk-health.prom"
SEP = "\x1e"
# What the stub `date +%s` prints, so the timestamp assertion reads no live clock.
EPOCH = 1760000000

# kuma_push records any call it receives; the cases assert it never runs. boot_grace_active
# answers what the case sets, never the runner's real uptime.
LIB_STUB = """\
kuma_push() {
  printf '%s\\n%s\\x1e' "$1" "$2" >> "$KUMA_PUSH_OUT"
}
boot_grace_active() { [[ "$BOOT_GRACE" == "1" ]]; }
"""


def _df(used_pct: int) -> str:
    """A `df -P /` stub in the shape the real one prints: a header, then one filesystem line."""
    return (
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        f"echo '/dev/mapper/vg-root 100000000 {used_pct}000000 50000000 {used_pct}% /'\n"
    )


def _run(tmp_path, used_pct=40, boot_grace=False, textfile_dir=None):
    """Run the rendered script once; return (textfile dir, every kuma_push call as (status, msg))."""
    body = render_shell_script(*TEMPLATE)
    for literal in (REAL_LIB, HC_ENV):
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"

    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(LIB_STUB)
    hc_env = tmp_path / "ping.env"
    hc_env.write_text("")
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    if textfile_dir is None:
        textfile_dir = tmp_path / "textfile"
        textfile_dir.mkdir(exist_ok=True)

    script = write_exec(
        tmp_path / "disk-health.sh",
        body.replace(REAL_LIB, str(lib)).replace(HC_ENV, str(hc_env)),
    )
    stubs = fake_bin(
        tmp_path / "bin",
        df=_df(used_pct),
        curl="exit 0\n",
        date=f"echo {EPOCH}\n",
        logger=f'echo "$@" >> "{tmp_path / "logged"}"\n',
    )
    env = {
        "TEXTFILE_DIR": str(textfile_dir),
        "KUMA_PUSH_OUT": str(pushed),
        "BOOT_GRACE": "1" if boot_grace else "0",
        "PATH": "/usr/bin:/bin",
    }
    run(["bash", str(script)], env=env, stub_bin=stubs, check=True)
    pushes = [tuple(p.split("\n")[:2]) for p in pushed.read_text().split(SEP) if p]
    return textfile_dir, pushes


def _series(textfile_dir) -> dict[str, str]:
    """`name{labels}` -> value for every sample line in the written file."""
    lines = (textfile_dir / PROM).read_text().splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))


def _rendered_deadline() -> str:
    match = re.search(r"^DEADLINE_S=(\d+)$", render_shell_script(*TEMPLATE), re.M)
    assert match, "disk-health.sh.j2 no longer renders a DEADLINE_S line"
    return match.group(1)


def test_an_up_run_writes_the_three_series_and_pushes_nothing(tmp_path):
    textfile_dir, pushes = _run(tmp_path, used_pct=40)

    series = _series(textfile_dir)
    assert series['homelab_check_success{check="disk-health"}'] == "1"
    stamp = int(series['homelab_check_last_run_timestamp_seconds{check="disk-health"}'])
    assert stamp == EPOCH
    assert (
        series['homelab_check_deadline_seconds{check="disk-health"}']
        == _rendered_deadline()
    )
    assert series['homelab_disk_root_used_ratio{check="disk-health"}'] == "0.40"
    assert pushes == []


def test_a_down_run_writes_success_zero(tmp_path):
    textfile_dir, pushes = _run(tmp_path, used_pct=90)
    assert _series(textfile_dir)['homelab_check_success{check="disk-health"}'] == "0"
    assert pushes == []


def test_a_boot_grace_run_leaves_the_previous_file_byte_identical(tmp_path):
    textfile_dir = tmp_path / "textfile"
    textfile_dir.mkdir()
    previous = b'homelab_check_success{check="disk-health"} 0\n'
    (textfile_dir / PROM).write_bytes(previous)

    _, pushes = _run(tmp_path, boot_grace=True, textfile_dir=textfile_dir)

    assert (textfile_dir / PROM).read_bytes() == previous
    assert pushes == []


def test_the_file_is_world_readable_and_no_temp_file_is_left(tmp_path):
    textfile_dir, _ = _run(tmp_path)
    assert sorted(p.name for p in textfile_dir.iterdir()) == [PROM]
    assert (textfile_dir / PROM).stat().st_mode & 0o777 == 0o644


def test_a_failed_write_is_logged_and_the_run_still_completes(tmp_path):
    missing = tmp_path / "no-such-dir"
    _run(tmp_path, textfile_dir=missing)
    assert not missing.exists()
    assert "textfile write to" in (tmp_path / "logged").read_text()
