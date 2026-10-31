"""release-staleness-check records probe.py's `--kuma` line: one line, grouped by reason.

A refused narrowing marks the whole fleet stale, and the per-service reasons then run to
~7,500 chars. The grouping is `bridge.msgfmt`'s and is tested beside it; this proves the cron asks
for that shape and records what it gets, unedited. The real template is rendered and run under
bash with the check library, `git` and `uv` stubbed; the assertion is on the run's `status=` log
line, which `probe.py alerts` reads, and on the verdict file the SessionStart banner reads.

It also holds the changed-set note: when the counted stale set changes while the check is DOWN,
the log line carries a `Stale set changed:` prefix, and the cron records the new set for the next
run to compare against. Since M6 batch B1's Cutover-2 nothing is pushed, so the Kuma-only
not-a-recovery `up` that used to precede the `down` is gone.
"""

from lib.proc_testing import run, write_exec
from _shell_render import render_shell_script, rendered_shell_text

# `reachout_verdict` from the real library, stubbed to its WAN-reachable answer — which is what
# every case here means: the git fetch failed and the link is fine, so the tile must page. The
# skip path is covered against the real function by
# ansible/tests/setup/test_kuma_push_wan_skip.py.
REACHOUT_STUB = 'reachout_verdict() { REACHOUT_STATUS=down; REACHOUT_NOTE=""; }\n'

TEMPLATE = ("setup", "k3s", "release-staleness-check.sh.j2")
# The shared check library is stubbed: the real file would probe the WAN. The textfile it writes
# is covered in test_k3s_cron_textfiles.py.
CHECK_LIB = "/usr/local/lib/homelab-check-lib.sh"

GROUPED = (
    "3 services stale — host_vars/daniel-pi.yml [every service: node-exporter was removed "
    "from containers_list] (artifacts, authelia, bazarr). Details: probe.py releases --stale-only"
)
STATE_DIR = "/var/lib/homelab/release-staleness"
SEP = "\x1e"


def _run(tmp_path, probe_output, probe_rc, names=None, prev=None):
    """Run the rendered cron once; return the run's (status, msg) verdicts, in order.

    `names` is what probe.py writes to `--names-out`, and `prev` the set a previous DOWN
    recorded. The render carries no overrides: `domain`, the ingress VIP, `sys_user`, the
    grace window and the WAN probe URLs all come from the inventory the accessor renders
    against, the same one the shellcheck gate lints -- `sys_user` resolves to "ubuntu", which
    is why the literals below and the REPO_DIR substitution name that path rather than a
    test-only stand-in.
    """
    body = render_shell_script(*TEMPLATE)
    for literal in (
        CHECK_LIB,
        "/home/ubuntu/.local/bin/uv",
        "--grace-minutes 60",
        STATE_DIR,
    ):
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if prev is not None:
        (state / "stale-set").write_text("".join(f"{n}\n" for n in prev))
    logged = tmp_path / "logged"
    logged.write_text("")
    check_lib = tmp_path / "homelab-check-lib.sh"
    check_lib.write_text(
        "write_check_textfile() { :; }\n"
        # The third argument is the message: `logger -t <tag> "status=<s> <msg>"`.
        f'logger() {{ printf "%s{SEP}" "$3" >> "{logged}"; }}\n'
        "git() { :; }\n" + REACHOUT_STUB
    )
    names_src = tmp_path / "names"
    if names is not None:
        names_src.write_text("".join(f"{n}\n" for n in names))
    uv = write_exec(
        tmp_path / "uv",
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        f'  if [[ $1 == --names-out && -f "{names_src}" ]]; then cp "{names_src}" "$2"; fi\n'
        "  shift\n"
        "done\n"
        f"cat <<'EOF'\n{probe_output}\nEOF\nexit {probe_rc}\n",
    )

    script = write_exec(
        tmp_path / "release-staleness-check.sh",
        body.replace(CHECK_LIB, str(check_lib))
        .replace("/home/ubuntu/.local/bin/uv", str(uv))
        .replace("/home/ubuntu/server", str(tmp_path))
        .replace(STATE_DIR, str(state)),
    )
    run(["bash", str(script)], check=True)
    verdicts = [p for p in logged.read_text().split(SEP) if p.startswith("status=")]
    return [tuple(v[len("status=") :].split(" ", 1)) for v in verdicts]


def _recorded(tmp_path):
    path = tmp_path / "state" / "stale-set"
    return path.read_text().split() if path.exists() else None


def _verdict(tmp_path):
    return (tmp_path / "state" / "verdict").read_text()


def test_a_stale_fleet_records_the_grouped_line_verbatim(tmp_path):
    assert _run(tmp_path, GROUPED, 1) == [("down", GROUPED)]


def test_the_cron_asks_probe_for_the_kuma_shape():
    assert "releases --stale-only --kuma" in rendered_shell_text(*TEMPLATE)


def test_a_clean_fleet_records_the_probe_line_verbatim(tmp_path):
    clean = "0 service(s) stale; every known k8s service has a current record."
    assert _run(tmp_path, clean, 0) == [("up", clean)]


def test_a_changed_stale_set_is_flagged_in_the_down_line_with_no_up_before_it(
    tmp_path,
):
    pushes = _run(
        tmp_path,
        GROUPED,
        1,
        names=["authelia", "bazarr"],
        prev=["artifacts", "authelia"],
    )
    delta = "Stale set changed: 1 newly stale (bazarr); 1 cleared (artifacts)"
    assert pushes == [("down", f"{delta}. {GROUPED}")]
    assert _recorded(tmp_path) == ["authelia", "bazarr"]


def test_an_unchanged_stale_set_is_clean_a_plain_down(tmp_path):
    same = ["artifacts", "authelia"]
    assert _run(tmp_path, GROUPED, 1, names=same, prev=same) == [("down", GROUPED)]


def test_the_first_down_is_a_plain_down_and_records_the_set(tmp_path):
    assert _run(tmp_path, GROUPED, 1, names=["authelia"]) == [("down", GROUPED)]
    assert _recorded(tmp_path) == ["authelia"]


def test_a_clean_fleet_clears_the_recorded_set(tmp_path):
    clean = "0 service(s) stale; every known k8s service has a current record."
    _run(tmp_path, clean, 0, names=[], prev=["authelia"])
    assert _recorded(tmp_path) is None


def test_a_broken_check_keeps_the_recorded_set(tmp_path):
    pushes = _run(tmp_path, "Traceback", 2, names=[], prev=["authelia"])
    assert [s for s, _ in pushes] == ["down"]
    assert _recorded(tmp_path) == ["authelia"]


def test_the_first_verdict_after_a_broken_check_is_flagged_as_a_change(tmp_path):
    # The check is already DOWN from the broken run, so a plain DOWN would hide the new set.
    _run(tmp_path, "Traceback", 2, names=[])
    assert _recorded(tmp_path) == []
    pushes = _run(tmp_path, GROUPED, 1, names=["authelia"])
    assert [s for s, _ in pushes] == ["down"]
    assert pushes[0][1] == f"Stale set changed: 1 newly stale (authelia). {GROUPED}"


def test_the_verdict_file_holds_the_probe_line_without_the_realert_prefix(tmp_path):
    # The SessionStart banner reads this file (#6183) and matches the probe's headline at the
    # start of the line, which the journal's `Stale set changed:` prefix would hide.
    _run(tmp_path, GROUPED, 1, names=["authelia", "bazarr"], prev=["artifacts"])
    assert _verdict(tmp_path) == f"status=down {GROUPED}\n"
    assert (tmp_path / "state" / "verdict").stat().st_mode & 0o777 == 0o644


def test_a_clean_run_replaces_a_down_verdict_with_up(tmp_path):
    clean = "0 service(s) stale; every known k8s service has a current record."
    _run(tmp_path, GROUPED, 1, names=["authelia"])
    _run(tmp_path, clean, 0, names=[], prev=["authelia"])
    assert _verdict(tmp_path) == f"status=up {clean}\n"
