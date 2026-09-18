"""release-staleness-check pushes the stale services' NAMES, not their reasons (#2013).

A refused narrowing marks the whole fleet stale, and the per-service reasons then ran to
~7,500 chars — past Discord's 1024-char embed field, so Kuma's DOWN notification was rejected
with HTTP 400 and reached nobody. The real template is rendered and run under bash with the
push library, `git` and `uv` stubbed; the assertion is on the msg handed to `kuma_push`.
"""

import subprocess

import jinja2

from _helpers import ANSIBLE

TEMPLATE = ANSIBLE / "roles/setup/k3s/templates/release-staleness-check.sh.j2"

STALE_OUTPUT = "\n".join(
    f"{svc}: changed since applied: ansible/inventory/host_vars/daniel-pi.yml "
    "[every service: node-exporter was removed from containers_list]"
    for svc in ("artifacts", "authelia", "bazarr")
)


def _run(tmp_path, probe_output, probe_rc):
    body = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(TEMPLATE.read_text())
        .render(
            domain="example.test", k3s_metallb_ingress_vip="10.0.0.240", sys_user="u"
        )
    )
    for literal in (
        "/usr/local/lib/kuma-push-lib.sh",
        "/etc/rancher/k3s/kuma-push.env",
        "/home/u/.local/bin/uv",
    ):
        assert literal in body, f"{TEMPLATE.name} no longer references {literal}"

    pushed = tmp_path / "pushed"
    lib = tmp_path / "kuma-push-lib.sh"
    lib.write_text(
        f'kuma_push() {{ printf "%s\\n%s" "$1" "$2" > "{pushed}"; }}\n'
        "git() { :; }\nlogger() { :; }\n"
    )
    env = tmp_path / "kuma-push.env"
    env.write_text("RELEASE_STALENESS_PUSH_TOKEN=stubtoken\n")
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!/usr/bin/env bash\ncat <<'EOF'\n{probe_output}\nEOF\nexit {probe_rc}\n"
    )
    uv.chmod(0o755)

    script = tmp_path / "release-staleness-check.sh"
    script.write_text(
        body.replace("/usr/local/lib/kuma-push-lib.sh", str(lib))
        .replace("/etc/rancher/k3s/kuma-push.env", str(env))
        .replace("/home/u/.local/bin/uv", str(uv))
        .replace("/home/u/server", str(tmp_path))
    )
    subprocess.run(["bash", str(script)], check=True, capture_output=True)
    status, msg = pushed.read_text().split("\n", 1)
    return status, msg


def test_a_stale_fleet_pushes_the_names_and_a_count(tmp_path):
    status, msg = _run(tmp_path, STALE_OUTPUT, 1)
    assert status == "down"
    assert msg.startswith("3 service(s) stale: artifacts, authelia, bazarr")
    assert "changed since applied" not in msg
    assert "probe.py releases --stale-only" in msg


def test_a_clean_fleet_pushes_the_probe_line_verbatim(tmp_path):
    clean = "0 service(s) stale; every known k8s service has a current record."
    assert _run(tmp_path, clean, 0) == ("up", clean)
