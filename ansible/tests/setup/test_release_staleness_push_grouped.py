"""release-staleness-check pushes probe.py's `--kuma` line: one line, grouped by reason (#2013).

A refused narrowing marks the whole fleet stale, and the per-service reasons then ran to
~7,500 chars — past Discord's 1024-char embed field, so Kuma's DOWN notification was rejected
with HTTP 400 and reached nobody. The grouping is `bridge.msgfmt`'s and is tested beside it;
this proves the cron asks for that shape and pushes what it gets, unedited. The real template
is rendered and run under bash with the push library, `git` and `uv` stubbed; the assertion is
on the pushes handed to `kuma_push`.

It also holds the re-alert (#2378): Kuma notifies only on a transition, so when the counted
stale set changes while the tile is DOWN, the cron pushes a not-a-recovery `up` and then the
`down`, and records the new set for the next run to compare against.
"""

import subprocess

import jinja2

from _helpers import ANSIBLE

TEMPLATE = ANSIBLE / "roles/setup/k3s/templates/release-staleness-check.sh.j2"

GROUPED = (
    "3 services stale — host_vars/daniel-pi.yml [every service: node-exporter was removed "
    "from containers_list] (artifacts, authelia, bazarr). Details: probe.py releases --stale-only"
)
STATE_DIR = "/var/lib/homelab/release-staleness"
SEP = "\x1e"


def _run(tmp_path, probe_output, probe_rc, names=None, prev=None):
    """Run the rendered cron once; return every (status, msg) push, in order.

    `names` is what probe.py writes to `--names-out`, and `prev` the set a previous DOWN
    recorded.
    """
    body = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(TEMPLATE.read_text())
        .render(
            domain="example.test",
            k3s_metallb_ingress_vip="10.0.0.240",
            sys_user="u",
            k3s_release_staleness_grace_minutes=60,
        )
    )
    for literal in (
        "/usr/local/lib/kuma-push-lib.sh",
        "/etc/rancher/k3s/kuma-push.env",
        "/home/u/.local/bin/uv",
        "--grace-minutes 60",
        STATE_DIR,
    ):
        assert literal in body, f"{TEMPLATE.name} no longer references {literal}"

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if prev is not None:
        (state / "stale-set").write_text("".join(f"{n}\n" for n in prev))
    pushed = tmp_path / "pushed"
    pushed.write_text("")
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        "kuma_push() {\n"
        f'  printf "%s\\n%s{SEP}" "$1" "$2" >> "{pushed}"\n'
        "}\n"
        "git() { :; }\nlogger() { :; }\n"
    )
    env = tmp_path / "kuma-push.env"
    env.write_text("RELEASE_STALENESS_PUSH_TOKEN=stubtoken\n")
    names_src = tmp_path / "names"
    if names is not None:
        names_src.write_text("".join(f"{n}\n" for n in names))
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        f'  if [[ $1 == --names-out && -f "{names_src}" ]]; then cp "{names_src}" "$2"; fi\n'
        "  shift\n"
        "done\n"
        f"cat <<'EOF'\n{probe_output}\nEOF\nexit {probe_rc}\n"
    )
    uv.chmod(0o755)

    script = tmp_path / "release-staleness-check.sh"
    script.write_text(
        body.replace("/usr/local/lib/kuma-push-lib.sh", str(lib))
        .replace("/etc/rancher/k3s/kuma-push.env", str(env))
        .replace("/home/u/.local/bin/uv", str(uv))
        .replace("/home/u/server", str(tmp_path))
        .replace(STATE_DIR, str(state))
    )
    subprocess.run(["bash", str(script)], check=True, capture_output=True)
    return [tuple(p.split("\n", 1)) for p in pushed.read_text().split(SEP) if p]


def _recorded(tmp_path):
    path = tmp_path / "state" / "stale-set"
    return path.read_text().split() if path.exists() else None


def test_a_stale_fleet_pushes_the_grouped_line_verbatim(tmp_path):
    assert _run(tmp_path, GROUPED, 1) == [("down", GROUPED)]


def test_the_cron_asks_probe_for_the_kuma_shape():
    assert "releases --stale-only --kuma" in TEMPLATE.read_text()


def test_a_clean_fleet_pushes_the_probe_line_verbatim(tmp_path):
    clean = "0 service(s) stale; every known k8s service has a current record."
    assert _run(tmp_path, clean, 0) == [("up", clean)]


def test_a_changed_stale_set_is_flagged_with_a_not_a_recovery_up_then_the_down(
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
    assert pushes == [
        (
            "up",
            f"Not a recovery. {delta}. The DOWN that follows carries the current list.",
        ),
        ("down", f"{delta}. {GROUPED}"),
    ]
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
    # The tile is already DOWN from the broken run, so a plain DOWN would notify nobody.
    _run(tmp_path, "Traceback", 2, names=[])
    assert _recorded(tmp_path) == []
    pushes = _run(tmp_path, GROUPED, 1, names=["authelia"])
    assert [s for s, _ in pushes] == ["up", "down"]
    assert pushes[1][1] == f"Stale set changed: 1 newly stale (authelia). {GROUPED}"
