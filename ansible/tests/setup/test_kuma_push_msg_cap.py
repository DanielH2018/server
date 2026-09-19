"""`kuma_push` caps `msg` before it reaches curl (kuma-push-lib.sh, #2013).

The bridge's `PUSH_MSG_MAX` covers the cluster checks; this library is the boundary for every
host cron. The stub curl records the `msg=` it was handed, so the assertion is on what would
have gone over the wire, not on a return code.
"""

import subprocess

from _helpers import ANSIBLE

LIB = ANSIBLE / "roles/setup/initial_setup/files/kuma-push-lib.sh"
MSG_MAX = 900


def _pushed_msg(tmp_path, msg):
    msg_file = tmp_path / "msg"
    script = f"""
    source {LIB}
    curl() {{
      while [ $# -gt 0 ]; do
        if [ "$1" = --data-urlencode ]; then
          case "$2" in msg=*) printf '%s' "${{2#msg=}}" > "{msg_file}" ;; esac
        fi
        shift
      done
      printf '200 application/json'
    }}
    logger() {{ :; }}
    kuma_push down "$1" https://push.example/secret-token kuma.local 10.0.0.1 test-tag
    """
    subprocess.run(["bash", "-c", script, "_", msg], check=True, capture_output=True)
    return msg_file.read_text()


def test_a_msg_under_the_cap_passes_verbatim(tmp_path):
    msg = "x" * MSG_MAX
    assert _pushed_msg(tmp_path, msg) == msg


def test_a_msg_over_the_cap_is_cut_with_a_count_marker(tmp_path):
    sent = _pushed_msg(tmp_path, "a" * 3000)
    assert len(sent) == MSG_MAX
    assert sent.endswith(" …(+%d chars)" % (3000 - sent.index(" …")))
