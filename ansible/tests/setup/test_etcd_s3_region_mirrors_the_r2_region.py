"""The etcd snapshot's S3 region and the Longhorn R2 region are a mirrored pair across two roles (H1.7b).

`longhorn-backup` defines `k3s_longhorn_r2_region` for the R2 target's URL. The off-box etcd
snapshot script is a host file of `setup/k3s`, which may not read a key a Flux-born role defines
(the `no-flux-born-key-on-the-host-plane` census row), so it reads the twin `k3s_etcd_s3_region`.
Both name the same R2 bucket's region. One that drifts leaves a snapshot upload and a Longhorn
backup talking to different regions of the same account.

Run: uv run pytest ansible/tests/setup/test_etcd_s3_region_mirrors_the_r2_region.py
"""

from _shell_render import rendered_shell_text
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K8S_ROLES

BACKUP_DEFAULTS = K8S_ROLES / "longhorn-backup" / "defaults" / "main.yml"


def unequal(k3s: dict, backup: dict) -> list[str]:
    """One line when the pair differs or a key of it is missing, else []."""
    if "k3s_etcd_s3_region" not in k3s or "k3s_longhorn_r2_region" not in backup:
        return [
            "k3s_etcd_s3_region / k3s_longhorn_r2_region: a key of the pair is missing"
        ]
    if k3s["k3s_etcd_s3_region"] != backup["k3s_longhorn_r2_region"]:
        return [
            f"k3s_etcd_s3_region={k3s['k3s_etcd_s3_region']!r} but "
            f"k3s_longhorn_r2_region={backup['k3s_longhorn_r2_region']!r}"
        ]
    return []


def test_the_pair_is_equal():
    k3s = yaml_fast.safe_load(K3S_DEFAULTS.read_text())
    backup = yaml_fast.safe_load(BACKUP_DEFAULTS.read_text())
    assert unequal(k3s, backup) == []


def test_a_changed_twin_is_flagged():
    assert unequal({"k3s_etcd_s3_region": "auto"}, {"k3s_longhorn_r2_region": "eu"})
    assert (
        unequal({"k3s_etcd_s3_region": "auto"}, {"k3s_longhorn_r2_region": "auto"})
        == []
    )


def test_a_missing_twin_is_flagged():
    assert unequal({}, {"k3s_longhorn_r2_region": "auto"})


def test_the_off_box_script_passes_the_r2_regions_value():
    """The rendered script carries the Longhorn key's value, so the repoint changed no byte."""
    backup = yaml_fast.safe_load(BACKUP_DEFAULTS.read_text())
    rendered = rendered_shell_text("setup", "k3s", "etcd-snapshot-offbox.sh.j2")
    assert f'--s3-region "{backup["k3s_longhorn_r2_region"]}"' in rendered
