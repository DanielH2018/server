"""The needrestart drop-in the reconcile component ships, evaluated by perl as needrestart does.

needrestart evals each conf.d file, then looks a service up by matching its name against every
`override_rc` key as a regex; a match whose value is 0 is never restarted. The harness does the
same over a manifest written to tmp_path, so these tests need perl, which every host and CI
runner has.
"""

import json
import shutil
import subprocess

import pytest

from lib.repo_paths import REPO

CONF = REPO / "hosts/components/reconcile/files/needrestart-homelab-units.conf"
INSTALLED_MANIFEST = "/usr/lib/homelab/manifest.json"
HARNESS = r"""
our %nrconf = (override_rc => {qr(^dbus) => 0});
do $ARGV[0]; die $@ if $@;
for my $name (@ARGV[1..$#ARGV]) {
    my $skip = grep { $name =~ /$_/ && !$nrconf{override_rc}{$_} } keys %{$nrconf{override_rc}};
    print "$name=", ($skip ? "kept" : "restarted"), "\n";
}
"""

pytestmark = pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl")


def _verdicts(tmp_path, manifest: str, *names: str) -> dict[str, str]:
    path = tmp_path / "manifest.json"
    path.write_text(manifest)
    conf = tmp_path / "homelab-units.conf"
    conf.write_text(CONF.read_text().replace(INSTALLED_MANIFEST, str(path)))
    out = subprocess.run(
        ["perl", "-e", HARNESS, str(conf), *names],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout  # fmt: skip
    return dict(line.split("=", 1) for line in out.splitlines())


MANIFEST = json.dumps(
    {
        "units": {
            "homelab-renovate-agent.service": {"policy": "manual"},
            "k3s.service": {"policy": "windowed"},
            "renovate-agent-land@.service": {},
            "coredns-host.service": {},
            "deploy-ui.service": {},
        }
    }
)


def test_a_policy_unit_and_a_template_instance_are_kept_and_a_plain_daemon_restarts(
    tmp_path,
):
    """#8854: the 2026-11-15 homelab-tools upgrade restarted every one of these but k3s."""
    assert _verdicts(
        tmp_path,
        MANIFEST,
        "homelab-renovate-agent.service",
        "k3s.service",
        "renovate-agent-land@8828.service",
        "coredns-host.service",
        "deploy-ui.service",
    ) == {
        "homelab-renovate-agent.service": "kept",
        "k3s.service": "kept",
        "renovate-agent-land@8828.service": "kept",
        "coredns-host.service": "restarted",
        "deploy-ui.service": "restarted",
    }


def test_a_name_is_matched_whole_not_as_a_prefix(tmp_path):
    verdicts = _verdicts(
        tmp_path,
        MANIFEST,
        "homelab-renovate-agent-setup.service",
        "renovate-agent-landx@1.service",
    )
    assert set(verdicts.values()) == {"restarted"}


@pytest.mark.parametrize("manifest", ["", "{not json", '{"units": []}'])
def test_a_manifest_that_will_not_read_leaves_needrestarts_list_alone(
    tmp_path, manifest
):
    assert _verdicts(tmp_path, manifest, "dbus.service", "k3s.service") == {
        "dbus.service": "kept",
        "k3s.service": "restarted",
    }
